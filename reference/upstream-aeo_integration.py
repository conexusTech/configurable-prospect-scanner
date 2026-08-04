#!/usr/bin/env python3
"""SAMPLE AEO integration wrapper for the av-lead-scanner tool (Scenario 2).

This is a *reference example*, not part of the core tool. It shows how a
developer embeds av-lead-scanner inside an AEO skill that runs as a k8s Job:
the skill fetches its organization context from aeo-backend, drives the tool,
and forwards each tool event to aeo-backend's durable scan-events endpoint.

The core tool (`av_lead_scanner.py`) stays generic — it knows nothing about
AEO. All AEO-specific behavior lives here, in a custom Sink that maps the
tool's event stream onto `POST /runtime/scans/{scan_run_id}/events`.

Flow (mirrors the deploy brief — build once, run as a Job):

  1. Require ORGANIZATION_ID + SCAN_RUN_ID (env, injected by the queue).
  2. GET  {AEO_BACKEND_URL}/api/runtime/organizations/{ORGANIZATION_ID}/context
     → the SAME shape as examples/organization.json.
  3. Validate the context meets the tool's minimum requirements. (Optionally
     via an LLM inference call — see `validate_context_for_run`.)
  4. discover → forward each `prospects` event  as type=prospects
  5. score    → forward the  `scored`    event  as type=scored
  6. completed→ forward                          as type=completed
  On ANY error → forward type=error and terminate non-zero.

Env:
  ORGANIZATION_ID   (required)  org whose context to fetch
  SCAN_RUN_ID       (required)  binds the events to this scan run
  AEO_BACKEND_URL   (required)  e.g. http://aeo-backend
  CI_USER / CI_PASSWORD         HTTP Basic for the events callback (from the
                                AEO_CI_SECRET_ID secret in a real deploy)
  GEMINI_API_KEY    (optional)  if set, real discovery; else pass --mock-style
                                by setting AV_SCANNER_MOCK=1
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

# Import the tool from the sibling skill directory (…/skill/av_lead_scanner.py).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import av_lead_scanner as als  # noqa: E402


# ── AEO backend client ───────────────────────────────────────────────────

def _basic_auth_header() -> dict[str, str]:
    user = os.environ.get("CI_USER", "")
    pw = os.environ.get("CI_PASSWORD", "")
    if not user:
        return {}
    import base64
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def fetch_context(backend_url: str, org_id: str) -> dict[str, Any]:
    """GET the organization context from aeo-backend."""
    url = f"{backend_url.rstrip('/')}/api/runtime/organizations/{org_id}/context"
    req = urlrequest.Request(url, headers=_basic_auth_header(), method="GET")
    with urlrequest.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def post_event(backend_url: str, scan_run_id: str, event_type: str, payload: dict[str, Any]) -> None:
    """POST one scan event: /runtime/scans/{scan_run_id}/events."""
    url = f"{backend_url.rstrip('/')}/runtime/scans/{scan_run_id}/events"
    body = json.dumps({"type": event_type, **payload}).encode()
    headers = {"Content-Type": "application/json", **_basic_auth_header()}
    req = urlrequest.Request(url, data=body, headers=headers, method="POST")
    with urlrequest.urlopen(req, timeout=30) as resp:
        resp.read()


# ── Sink: map tool events → aeo-backend event POSTs ─────────────────────────

class AeoEventSink(als.Sink):
    """Forwards the tool's event stream to aeo-backend.

    The tool emits generic events; this sink translates them to the AEO
    durable-callback contract. Per-source progress (`phase_start` /
    `phase_complete`) is logged only. The deduped `prospects` event, the
    `scored` event, `completed`, and `error` map 1:1 to POST event types.
    """

    def __init__(self, backend_url: str, scan_run_id: str):
        self._backend = backend_url
        self._scan = scan_run_id

    def emit(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "prospects":
            post_event(self._backend, self._scan, "prospects", {"items": event.get("items", [])})
            print(f"[aeo] forwarded prospects: {len(event.get('items', []))}", file=sys.stderr)
        elif etype == "scored":
            post_event(self._backend, self._scan, "scored", {"items": event.get("items", [])})
            print(f"[aeo] forwarded scored: {len(event.get('items', []))}", file=sys.stderr)
        elif etype == "completed":
            post_event(self._backend, self._scan, "completed", {"summary": event.get("summary", {})})
            print("[aeo] forwarded completed", file=sys.stderr)
        elif etype == "error":
            post_event(self._backend, self._scan, "error", {"message": event.get("message", "")})
            print(f"[aeo] forwarded error: {event.get('message')}", file=sys.stderr)
        elif etype in ("phase_start", "phase_complete"):
            print(f"[aeo] progress {etype} phase={event.get('phase')} count={event.get('count', '')}", file=sys.stderr)


# ── Optional: LLM-based context validation ─────────────────────────────────

def validate_context_for_run(ctx: dict[str, Any]) -> list[str]:
    """Return a list of problems (empty == OK).

    Scenario 2 calls for an LLM inference call to judge whether the fetched
    context meets the tool's minimum requirements. We default to the tool's
    own deterministic `validate_context` so this sample runs with no LLM
    dependency. To use an LLM instead, replace the body with an inference call
    that reads SKILL.md + the context and returns a pass/fail + reasons.
    """
    return als.validate_context(ctx, need_sources=True)


# ── Main ────────────────────────────────────────────────────────────────

def main() -> int:
    org_id = os.environ.get("ORGANIZATION_ID")
    scan_run_id = os.environ.get("SCAN_RUN_ID")
    backend_url = os.environ.get("AEO_BACKEND_URL")

    if not org_id:
        print("ORGANIZATION_ID not set — nothing to do", file=sys.stderr)
        return 2
    if not scan_run_id or not backend_url:
        print("SCAN_RUN_ID and AEO_BACKEND_URL are required", file=sys.stderr)
        return 2

    sink = AeoEventSink(backend_url, scan_run_id)

    try:
        ctx = fetch_context(backend_url, org_id)
    except (HTTPError, URLError, ValueError) as exc:
        # Can't fetch context → can't emit against a scan we may not have set up
        # cleanly; still try to report the error, then terminate.
        try:
            post_event(backend_url, scan_run_id, "error", {"message": f"fetch context failed: {exc}"})
        except Exception:  # noqa: BLE001
            pass
        print(f"fetch context failed: {exc}", file=sys.stderr)
        return 1

    problems = validate_context_for_run(ctx)
    if problems:
        msg = "context validation failed: " + "; ".join(problems)
        sink.emit({"type": "error", "message": msg})
        return 1

    # Provider: AV_SCANNER_MOCK forces the offline provider; otherwise
    # AV_SCANNER_PROVIDER (gemini|claude) selects the grounded-search backend.
    provider_name = "mock" if os.environ.get("AV_SCANNER_MOCK") else os.environ.get("AV_SCANNER_PROVIDER", "gemini")
    provider = als._pick_provider(provider_name, mock=False, dry_run=False)
    pconf = als._provider_config(ctx, provider_name)
    today = als._resolve_today(None)

    try:
        prospects = als.discover(ctx, scan_run_id=scan_run_id, provider=provider,
                                 emit=sink.emit, provider_config=pconf)
        scored = als.score_prospects(prospects, ctx, today=today)
        sink.emit({"type": "scored", "phase": "score", "items": scored})
        sink.emit({"type": "completed", "summary": {
            "total_prospects": len(prospects), "total_scored": len(scored),
        }})
    except Exception as exc:  # noqa: BLE001 — report + terminate
        sink.emit({"type": "error", "message": str(exc)})
        print(f"run failed: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
