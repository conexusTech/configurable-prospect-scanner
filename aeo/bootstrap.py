"""Self-bootstrap when launched as a conqrse-queue task.

**The queue does not pass the job.** It injects exactly one environment variable —
`TASK_RECORD_ID` — and the payload stays in the portal's own database: *"Only the
reference is injected"* (`k8s-job.executor.ts`). aeo-backend's enqueue site says the
same from the other end: *"the opaque business envelope the scan task reads back by
reference (GET /tasks/:id)"*.

So a container that expects `ORGANIZATION_ID` / `SCAN_RUN_ID` / `TENANT_ID` in its
environment gets none of them under the queue. It has to fetch them. This module is
that fetch, and it exists because assuming otherwise is exactly the kind of thing
that only surfaces when you actually run through the queue rather than beside it.

Everything else the catalog entry declares — credentials, the backend URL — arrives
via `envFrom` (Secrets/ConfigMaps), which the executor wires normally. Only the
*business* payload is by-reference.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib import request as urlrequest

#: Injected by the k8s executor. Its presence IS "we are running under the queue".
TASK_RECORD_ID_ENV = "TASK_RECORD_ID"

#: Where to read the payload back from. Inside the cluster this is the portal's
#: ClusterIP service; running the portal on a laptop it is the host address.
QUEUE_API_URL_ENV = "QUEUE_API_URL"


class BootstrapError(RuntimeError):
    """The task reference could not be resolved into a runnable job."""


def in_queue_mode() -> bool:
    return bool(os.environ.get(TASK_RECORD_ID_ENV))


def fetch_task_payload(queue_url: str, task_record_id: str) -> dict[str, Any]:
    """GET the task record and return its payload.

    Intake is documented as internal with no app auth (a network boundary, not a
    credential one), so this sends none.
    """
    url = f"{queue_url.rstrip('/')}/api/tasks/{task_record_id}"
    try:
        with urlrequest.urlopen(url, timeout=30) as resp:
            record = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — surfaced as BootstrapError below
        raise BootstrapError(f"could not read task {task_record_id} from {url}: {exc}") from exc

    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise BootstrapError(
            f"task {task_record_id} has no object `payload` — got "
            f"{type(payload).__name__}"
        )
    return payload


def job_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    """Map aeo-backend's scan payload onto the values the runner needs.

    Field names taken from the gateway's enqueue site, not from memory — note it is
    **`organization_id`**, not `org_id`, which is the sort of detail that costs a run.

    Payload shape:
        {scan_run_id, tenant_id, organization_id, parameters, test,
         phases?, code_url, skill: {name, slug, type}}

    `phases` is present only for reduced-phase runs (the Strategy Tester / a draft
    test run), and absent means "all phases" — the same convention `PHASES` uses.
    """
    skill = payload.get("skill")
    slug = skill.get("slug") if isinstance(skill, dict) else None

    resolved = {
        "ORGANIZATION_ID": payload.get("organization_id"),
        "TENANT_ID": payload.get("tenant_id"),
        "SCAN_RUN_ID": payload.get("scan_run_id"),
        "SKILL_SLUG": slug,
    }
    missing = [k for k, v in resolved.items() if not v]
    if missing:
        raise BootstrapError(
            f"scan payload is missing {', '.join(missing)} — cannot run. "
            f"Payload keys present: {', '.join(sorted(payload)) or '(none)'}"
        )

    phases = payload.get("phases")
    if isinstance(phases, list) and phases:
        resolved["PHASES"] = ",".join(str(p) for p in phases)

    return {k: str(v) for k, v in resolved.items() if v}


def bootstrap() -> dict[str, str]:
    """Resolve the job from the queue when in queue mode. `{}` otherwise.

    Values are returned rather than written to `os.environ` by the caller's choice —
    the runner decides precedence, and a function that silently mutates the process
    environment is harder to reason about than one that hands back a dict.
    """
    task_record_id = os.environ.get(TASK_RECORD_ID_ENV)
    if not task_record_id:
        return {}

    queue_url = os.environ.get(QUEUE_API_URL_ENV)
    if not queue_url:
        raise BootstrapError(
            f"{TASK_RECORD_ID_ENV} is set (queue mode) but {QUEUE_API_URL_ENV} is not "
            f"— the payload is delivered by reference and cannot be fetched without it. "
            f"Declare it on the catalog entry's envFrom/ConfigMap."
        )

    return job_from_payload(fetch_task_payload(queue_url, task_record_id))
