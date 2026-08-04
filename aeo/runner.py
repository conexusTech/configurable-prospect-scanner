"""Entrypoint for the `configurable-prospect-scanner` queue task.

Promoted from upstream's `examples/aeo_integration.py` (kept at
`reference/upstream-aeo_integration.py`), with three changes that matter:

1. **The context path is corrected.** Upstream fetched
   `/api/runtime/organizations/{id}/context`. aeo-backend sets **no global
   prefix** — its controller is `@Controller('runtime/organizations/:orgId')` — so
   that call 404s. The events POST was already correct, which is why the bug
   survived: one of the two paths worked.
2. **The AEO config is mapped, not assumed.** Upstream fed the fetched context
   straight to the engine, which only works if AEO returns the engine's own shape.
   It does not. `aeo.config_mapping` translates it, and refuses rather than
   defaults when the authored recipe cannot drive a real scan.
3. **Sections the engine cannot execute are reported**, not silently dropped.

Env (all injected by the queue in a real run):
  ORGANIZATION_ID   required — whose context to fetch
  SCAN_RUN_ID       required — binds every event to this run
  AEO_BACKEND_URL   required — e.g. http://localhost:3000
  CI_USER/CI_PASSWORD        — HTTP Basic for both AEO calls
  AV_SCANNER_MOCK=1          — offline provider; no model key needed (local runs)
  AV_SCANNER_PROVIDER        — gemini|claude when not mocking
  SCANNER_TOP_N              — output ranking cut (default 50)
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import av_lead_scanner as als  # noqa: E402

from aeo.config_mapping import (  # noqa: E402
    UnmappedConfigError,
    build_tool_context,
    unsupported_authored_sections,
)


def _auth_headers() -> dict[str, str]:
    user = os.environ.get("CI_USER", "")
    if not user:
        return {}
    token = base64.b64encode(
        f"{user}:{os.environ.get('CI_PASSWORD', '')}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}"}


def fetch_context(backend_url: str, org_id: str) -> dict[str, Any]:
    """GET the org's runtime context.

    No `/api` prefix — aeo-backend does not set one. Verified against
    `runtime-context.controller.ts`, not against upstream's docstring.
    """
    url = f"{backend_url.rstrip('/')}/runtime/organizations/{org_id}/context"
    req = urlrequest.Request(url, headers=_auth_headers(), method="GET")
    with urlrequest.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def post_event(
    backend_url: str, scan_run_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    url = f"{backend_url.rstrip('/')}/runtime/scans/{scan_run_id}/events"
    body = json.dumps({"type": event_type, **payload}).encode()
    headers = {"Content-Type": "application/json", **_auth_headers()}
    req = urlrequest.Request(url, data=body, headers=headers, method="POST")
    with urlrequest.urlopen(req, timeout=30) as resp:
        resp.read()


class AeoEventSink(als.Sink):
    """Forwards the engine's event stream to AEO's durable callback.

    `prospects` / `scored` / `completed` / `error` map 1:1 onto POST types.
    Per-source progress is logged only — it is high-frequency and AEO has no
    durable destination for it.
    """

    def __init__(self, backend_url: str, scan_run_id: str) -> None:
        self._backend = backend_url
        self._scan = scan_run_id

    def emit(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype in ("prospects", "scored"):
            items = event.get("items", [])
            post_event(self._backend, self._scan, etype, {"items": items})
            _log(f"forwarded {etype}: {len(items)}")
        elif etype == "completed":
            post_event(
                self._backend, self._scan, "completed", {"summary": event.get("summary", {})}
            )
            _log("forwarded completed")
        elif etype == "error":
            post_event(
                self._backend, self._scan, "error", {"message": event.get("message", "")}
            )
            _log(f"forwarded error: {event.get('message')}")
        elif etype in ("phase_start", "phase_complete"):
            _log(f"progress {etype} phase={event.get('phase')} count={event.get('count', '')}")


def _log(message: str) -> None:
    print(f"[aeo] {message}", file=sys.stderr)


def main() -> int:
    org_id = os.environ.get("ORGANIZATION_ID")
    scan_run_id = os.environ.get("SCAN_RUN_ID")
    backend_url = os.environ.get("AEO_BACKEND_URL")

    missing = [
        name
        for name, value in (
            ("ORGANIZATION_ID", org_id),
            ("SCAN_RUN_ID", scan_run_id),
            ("AEO_BACKEND_URL", backend_url),
        )
        if not value
    ]
    if missing:
        _log(f"missing required env: {', '.join(missing)}")
        return 2

    assert org_id and scan_run_id and backend_url  # narrowed by the check above
    sink = AeoEventSink(backend_url, scan_run_id)

    try:
        aeo_context = fetch_context(backend_url, org_id)
    except (HTTPError, URLError, ValueError) as exc:
        # Report before dying: a scan run left `running` with no explanation is
        # reconciled hours later by the gateway's stale-scan sweep, which reports
        # "runner lost" and hides the real cause.
        _try_report(backend_url, scan_run_id, f"fetch context failed: {exc}")
        return 1

    # Tell the operator up front about anything they authored that this engine
    # cannot run — before the scan, not by its absence from the output.
    for section in unsupported_authored_sections(aeo_context):
        _log(
            f"WARNING: the config's `{section}` section will NOT run — this engine "
            f"implements discovery and scoring only."
        )

    try:
        tool_context = build_tool_context(
            aeo_context, top_n=int(os.environ.get("SCANNER_TOP_N", "50"))
        )
    except UnmappedConfigError as exc:
        # A config problem, not a crash: it names every fault at once so the
        # operator repairs in one pass. Forwarded so it lands on the scan run
        # rather than only in container logs nobody will read.
        sink.emit({"type": "error", "message": str(exc)})
        _log(str(exc))
        return 1

    # The engine's own deterministic check, kept as a second gate: ours validates
    # that AEO's config maps, this validates that the mapping produced something
    # the engine can actually run.
    problems = als.validate_context(tool_context, need_sources=True)
    if problems:
        message = "engine rejected the mapped context: " + "; ".join(problems)
        sink.emit({"type": "error", "message": message})
        _log(message)
        return 1

    provider_name = (
        "mock" if os.environ.get("AV_SCANNER_MOCK") else os.environ.get("AV_SCANNER_PROVIDER", "gemini")
    )
    provider = als._pick_provider(provider_name, mock=False, dry_run=False)
    provider_config = als._provider_config(tool_context, provider_name)
    today = als._resolve_today(None)

    try:
        prospects = als.discover(
            tool_context,
            scan_run_id=scan_run_id,
            provider=provider,
            emit=sink.emit,
            provider_config=provider_config,
        )
        scored = als.score_prospects(prospects, tool_context, today=today)
        sink.emit({"type": "scored", "phase": "score", "items": scored})
        sink.emit(
            {
                "type": "completed",
                "summary": {
                    "total_prospects": len(prospects),
                    "total_scored": len(scored),
                    "provider": provider_name,
                },
            }
        )
    except Exception as exc:  # noqa: BLE001 — report, then terminate non-zero
        sink.emit({"type": "error", "message": str(exc)})
        _log(f"run failed: {exc}")
        return 1

    return 0


def _try_report(backend_url: str, scan_run_id: str, message: str) -> None:
    try:
        post_event(backend_url, scan_run_id, "error", {"message": message})
    except Exception:  # noqa: BLE001 — best effort; the log below is the fallback
        pass
    _log(message)


if __name__ == "__main__":
    sys.exit(main())
