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
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import av_lead_scanner as als  # noqa: E402

from aeo.config_mapping import (  # noqa: E402
    UnmappedConfigError,
    build_tool_context,
    unsupported_authored_sections,
)
from aeo.event_mapping import map_event  # noqa: E402


def _auth_headers() -> dict[str, str]:
    user = os.environ.get("CI_USER", "")
    if not user:
        return {}
    token = base64.b64encode(
        f"{user}:{os.environ.get('CI_PASSWORD', '')}".encode()
    ).decode()
    return {"Authorization": f"Basic {token}"}


def fetch_context(backend_url: str, org_id: str, skill_slug: str) -> dict[str, Any]:
    """GET the org's runtime context for a specific skill.

    Two corrections to upstream's version, both verified against
    `runtime-context.controller.ts` rather than its docstring:

    - **No `/api` prefix** — aeo-backend sets no global prefix.
    - **`?skill=<slug>` is REQUIRED.** One endpoint serves every skill, so the
      slug is what selects whose `config` comes back; omitting it is a 400. The
      queue injects it from the scan job's `skill.slug`.
    """
    query = urlencode({"skill": skill_slug})
    url = f"{backend_url.rstrip('/')}/runtime/organizations/{org_id}/context?{query}"
    req = urlrequest.Request(url, headers=_auth_headers(), method="GET")
    with urlrequest.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def post_event(
    backend_url: str,
    scan_run_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    tenant_id: str,
    organization_id: str,
) -> None:
    """POST one scan event.

    ⚠️ **`tenant_id` and `organization_id` are REQUIRED on every event**, not just
    data events — `ScanEventBaseDto` declares both `@IsUUID()`, and the writes are
    RLS-scoped to the envelope's `tenant_id`. Upstream's integration example sent
    neither (`{"type": event_type, **payload}`), so it would have 400'd on every
    single callback; it cannot ever have completed a run. Found by actually running
    it against a local gateway, not by reading it.
    """
    url = f"{backend_url.rstrip('/')}/runtime/scans/{scan_run_id}/events"
    body = json.dumps(
        {
            "type": event_type,
            "tenant_id": tenant_id,
            "organization_id": organization_id,
            **payload,
        }
    ).encode()
    headers = {"Content-Type": "application/json", **_auth_headers()}
    req = urlrequest.Request(url, data=body, headers=headers, method="POST")
    with urlrequest.urlopen(req, timeout=30) as resp:
        resp.read()


class AeoEventSink(als.Sink):
    """Forwards the engine's event stream to AEO's durable callback.

    Every event goes through `aeo.event_mapping` rather than being posted as-is.
    A pass-through fails: AEO wants `phase`/`phase_name` on each ITEM while the
    engine puts them on the EVENT, and AEO caps an event at 1000 items while the
    engine emits one event for a whole discovery sweep. Both failures are total —
    a 400 loses the entire sweep, not the offending part.
    """

    def __init__(
        self,
        backend_url: str,
        scan_run_id: str,
        tenant_id: str,
        organization_id: str,
    ) -> None:
        self._backend = backend_url
        self._scan = scan_run_id
        self._tenant = tenant_id
        self._org = organization_id

    def emit_safe(self, event: dict[str, Any]) -> None:
        """Emit without ever raising — for the terminal error path only.

        A callback failure while *reporting* a failure must not replace the real
        diagnosis with a urllib traceback. That is exactly what happened on the
        first live run: a refusal message naming two config faults was lost behind
        `HTTPError: 400`, and the operator would have seen the wrong problem.
        """
        try:
            self.emit(event)
        except Exception as exc:  # noqa: BLE001 — last-resort path
            _log(f"could not forward {event.get('type')!r} event: {exc}")

    def emit(self, event: dict[str, Any]) -> None:
        etype = event.get("type")

        # Progress events are legitimately high-frequency and have no durable AEO
        # destination — logged, never posted.
        if etype in ("phase_start", "phase_complete"):
            _log(f"progress {etype} phase={event.get('phase')} count={event.get('count', '')}")
            return

        posts = map_event(event)
        if not posts:
            _log(f"no AEO destination for event type {etype!r} — not forwarded")
            return

        for aeo_type, payload in posts:
            post_event(
                self._backend,
                self._scan,
                aeo_type,
                payload,
                tenant_id=self._tenant,
                organization_id=self._org,
            )
        count = sum(len(p.get("data", [])) for _, p in posts)
        suffix = f" in {len(posts)} batches" if len(posts) > 1 else ""
        _log(f"forwarded {etype}: {count} item(s){suffix}")


def _log(message: str) -> None:
    print(f"[aeo] {message}", file=sys.stderr)


def main() -> int:
    org_id = os.environ.get("ORGANIZATION_ID")
    tenant_id = os.environ.get("TENANT_ID")
    scan_run_id = os.environ.get("SCAN_RUN_ID")
    backend_url = os.environ.get("AEO_BACKEND_URL")
    skill_slug = os.environ.get("SKILL_SLUG")

    missing = [
        name
        for name, value in (
            ("ORGANIZATION_ID", org_id),
            # Required because every event envelope carries it and the writes are
            # RLS-scoped to it. The queue injects it from the scan job payload.
            ("TENANT_ID", tenant_id),
            ("SCAN_RUN_ID", scan_run_id),
            ("AEO_BACKEND_URL", backend_url),
            ("SKILL_SLUG", skill_slug),
        )
        if not value
    ]
    if missing:
        _log(f"missing required env: {', '.join(missing)}")
        return 2

    # Narrowed by the check above.
    assert org_id and tenant_id and scan_run_id and backend_url and skill_slug
    sink = AeoEventSink(backend_url, scan_run_id, tenant_id, org_id)

    try:
        aeo_context = fetch_context(backend_url, org_id, skill_slug)
    except (HTTPError, URLError, ValueError) as exc:
        # Report before dying: a scan run left `running` with no explanation is
        # reconciled hours later by the gateway's stale-scan sweep, which reports
        # "runner lost" and hides the real cause.
        sink.emit_safe({"type": "error", "message": f"fetch context failed: {exc}"})
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
        sink.emit_safe({"type": "error", "message": str(exc)})
        _log(str(exc))
        return 1

    # The engine's own deterministic check, kept as a second gate: ours validates
    # that AEO's config maps, this validates that the mapping produced something
    # the engine can actually run.
    problems = als.validate_context(tool_context, need_sources=True)
    if problems:
        message = "engine rejected the mapped context: " + "; ".join(problems)
        sink.emit_safe({"type": "error", "message": message})
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
        sink.emit_safe({"type": "error", "message": str(exc)})
        _log(f"run failed: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
