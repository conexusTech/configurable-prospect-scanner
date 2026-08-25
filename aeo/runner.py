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
  SCANNER_TOP_N              — FALLBACK limit (default 50) for the output ranking cut,
                               and for `discovery.target_prospects` /
                               `contacts.max_prospects` when the skill config omits them.
                               The config wins; this is the deployment default only.
                               ⚠️ It is NOT a fallback for `discovery.max_prospects` —
                               see `_optional_config_limit`.
  SCANNER_PHASE_CONCURRENCY  — width of the per-prospect phases (default 2). The single
                               biggest lever on run duration: verification and validation
                               are one grounded call per prospect each, so a scan's
                               wall-clock is roughly 2 x prospects / this x call latency.
                               Raise it only as far as the model key's rate limit allows —
                               a 429 costs more in backoff than the parallelism wins.
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
    UNIVERSAL_TIMING_FIELDS,
    UnmappedConfigError,
    build_tool_context,
    unsupported_authored_sections,
)
from aeo.context_refs import UnresolvedRefError  # noqa: E402
from aeo.context_refs import resolve as _resolve_refs  # noqa: E402
from aeo.bootstrap import BootstrapError, bootstrap  # noqa: E402
from aeo.event_mapping import map_event  # noqa: E402
from aeo.modules.apply import apply_modules, merge_signals_into_scored  # noqa: E402
from aeo.modules.loader import load_modules  # noqa: E402
from aeo.phases.contacts import find_contacts, merge_into_scored  # noqa: E402
from aeo.phases.geo_filter import (  # noqa: E402
    STRICTNESS_METRO,
    build_target_area,
)
from aeo.phases.geo_loop import DEFAULT_MAX_ROUNDS, discover_in_area  # noqa: E402
from aeo.phases.prospect_budget import (  # noqa: E402
    ProspectBudget,
    capped_discover,
)
from aeo.phases.query_expansion import (  # noqa: E402
    DEFAULT_MAX_QUERIES_PER_SOURCE,
    MARKET_PLACEHOLDER,
    expand_queries,
    unexpanded_placeholders,
)
from aeo.phases.ai_judgment import judge_prospects
from aeo.phases.enrichment import enrich_prospects  # noqa: E402
from aeo.phases.validation import surviving_ids, validate_prospects  # noqa: E402
from aeo.phases.zip_discovery import (  # noqa: E402
    DEFAULT_MAX_ZIPS_PER_MARKET,
    discover_zips,
    zips_as_markets,
)


def resolve_context_refs(aeo_context: dict[str, Any]) -> dict[str, Any]:
    """Resolve the skill config's bindings against the org's own context (R12).

    Only `skill.config` is walked — the rest of the context IS the resolution
    source, so rewriting it would be circular.
    """
    config = (aeo_context.get("skill") or {}).get("config") or {}
    resolved = _resolve_refs(config, aeo_context)
    return {**aeo_context, "skill": {**(aeo_context.get("skill") or {}), "config": resolved}}


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
        # `completed` is a SUMMARY payload with no `data` array, so the item counter
        # reports 0 for it — which read as "the run delivered nothing" and cost two
        # investigations, one of which published a wrong mechanism before the answer
        # turned out to be this line. Terminal events say what they are instead.
        suffix = f" in {len(posts)} batches" if len(posts) > 1 else ""
        if any(t == "completed" for t, _ in posts):
            _log(f"forwarded {etype}: run summary (no item list){suffix}")
        else:
            count = sum(len(p.get("data", [])) for _, p in posts)
            _log(f"forwarded {etype}: {count} item(s){suffix}")


def _log(message: str) -> None:
    print(f"[aeo] {message}", file=sys.stderr)


#: The phases this runtime can execute, in execution order.
PHASE_ZIP_DISCOVERY = "zip_discovery"
PHASE_DISCOVERY = "discovery"
PHASE_VALIDATION = "validation"
PHASE_CONTACTS = "contacts"
PHASE_SCORING = "scoring"
ALL_PHASES = (
    PHASE_ZIP_DISCOVERY,
    PHASE_DISCOVERY,
    PHASE_VALIDATION,
    PHASE_CONTACTS,
    PHASE_SCORING,
)


def selected_phases(raw: str | None) -> list[str]:
    """Resolve the R6 phase subset from `PHASES`. Absent means all phases.

    ⚠️ **The subset arrives as env, NOT in the config — by design, in two places.**
    aeo-backend deliberately strips `execution_phases`/`phases` from the runtime
    context ("the scanner runtime has its own built-in phases, and the builder agent
    authors *sections*, not phases"), and the engine selects work by subcommand
    rather than from config. So a reduced-phase draft test run (R6) can only be
    expressed on the dispatch payload, which the queue injects here.

    Unknown names are ignored rather than fatal: a caller asking for a phase this
    runtime does not have should get the phases it does have, not a failed scan.
    Discovery is always included — every later phase consumes its output.
    """
    if not raw or not raw.strip():
        return list(ALL_PHASES)
    asked = {p.strip().lower() for p in raw.split(",") if p.strip()}
    unknown = asked - set(ALL_PHASES)
    if unknown:
        _log(f"ignoring unknown phase(s): {', '.join(sorted(unknown))}")
    chosen = [p for p in ALL_PHASES if p in asked]
    if PHASE_DISCOVERY not in chosen:
        chosen.insert(0, PHASE_DISCOVERY)
    return chosen


#: Fallback when the skill config does not state a limit. `SCANNER_TOP_N` remains the
#: deployment-level default so behaviour is unchanged for configs that say nothing.
_ENV_LIMIT_FALLBACK = "SCANNER_TOP_N"


def _raw_positive_int(config: dict[str, Any], section: str, key: str) -> int | None:
    """`config[section][key]` as a positive int, or `None` if absent/unusable.

    The shared half of the two limit readers below. They differ only in what an
    absent value MEANS, which is the part worth having two names for.
    """
    block = config.get(section)
    raw = block.get(key) if isinstance(block, dict) else None
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _config_limit(config: dict[str, Any], section: str, key: str, default: int = 50) -> int:
    """A run limit, **from the skill config first, the environment only as a fallback.**

    ⚠️ **An environment variable is static configuration too.** `SCANNER_TOP_N` alone used
    to drive three unrelated things — how many in-area prospects discovery keeps looking
    for, how many prospects get contact enrichment, and the output ranking cut — so one
    deployment value silently overrode what the skill had decided. Set to 1 in a real
    environment it capped a 15-market scan to a single round of discovery and enriched
    exactly one prospect of fourteen, which read as "the model found little" rather than
    "we stopped looking".
    """
    authored = _raw_positive_int(config, section, key)
    if authored is not None:
        return authored
    try:
        return max(1, int(os.environ.get(_ENV_LIMIT_FALLBACK, str(default))))
    except (TypeError, ValueError):
        return default


#: Prospect ceiling applied to TEST runs only, and only ever downward.
#:
#: A test exists to prove the pipeline and the config, not to produce volume, and the
#: cost is almost entirely per-prospect: address verification is one grounded call each
#: at ~44s, so the ceiling is what sets the tail. Measured on run `29a75f94`
#: (Resource Floor Care, 150 prospects): 7m03s discovery + 13m52s verification =
#: 20m55s. The same run capped here spends ~1m30s verifying.
#:
#: ⚠️ That run is `test = t`, and it PREDATES this cap — which is why it produced 150.
#: An earlier version of this note called it a "production run"; it is a run in
#: production *data*, not a real (uncapped) one, and aeo-agent-service mirrored the
#: wrong label into their source before it was caught. Reproducing `150 in 20m55s`
#: with a test run today yields **15**, so anyone re-deriving the budget from this
#: number must use a run with `test = f`.
#:
#: ⚠️ It does NOT shorten discovery — the ceiling is applied *after* discovery has found
#: its candidates (that run discovered 377 and kept 150), so a capped test still pays
#: the full search cost. Shortening that means fewer markets or queries, which changes
#: what the test actually exercises.
TEST_RUN_MAX_PROSPECTS = 15


def _resolve_prospect_ceiling(
    config: dict[str, Any], is_test: bool
) -> tuple[int | None, str]:
    """The run's prospect ceiling, plus a phrase explaining which rule produced it.

    Returns `(limit, reason)`, where `None` means unbounded.

    **A real run returns exactly what the skill configured, untouched.** This function
    must not be able to lower a production ceiling; only `is_test` opens that door, and
    only via `min`. A skill deliberately configured below the cap keeps its own number
    rather than being raised to it, and an unbounded config *does* get the cap — an
    unbounded test run is the case this exists to prevent.
    """
    configured = _optional_config_limit(config, "discovery", "max_prospects")
    if not is_test:
        return configured, "skill config"
    if configured is None:
        return (
            TEST_RUN_MAX_PROSPECTS,
            f"test run, skill declared no ceiling, using {TEST_RUN_MAX_PROSPECTS}",
        )
    if configured <= TEST_RUN_MAX_PROSPECTS:
        return (
            configured,
            f"test run, skill's {configured} already at or below the {TEST_RUN_MAX_PROSPECTS} test cap",
        )
    return (
        TEST_RUN_MAX_PROSPECTS,
        f"test run, capped down from the skill's {configured}",
    )


def _optional_config_limit(
    config: dict[str, Any], section: str, key: str
) -> int | None:
    """A positive integer limit from the skill config, or `None` when it is absent.

    ⚠️ **Deliberately does NOT fall back to `SCANNER_TOP_N` the way `_config_limit`
    does.** A ceiling is a decision, and that variable is set to **1** in production —
    so borrowing it as a default would cap every run at a single prospect while
    looking like a sensible fallback, and the symptom (one prospect, no error) is
    indistinguishable from a market with nothing in it. An absent ceiling means "no
    ceiling".
    """
    return _raw_positive_int(config, section, key)



def _adjustment_bounds(tool_context: dict[str, Any]) -> tuple[float, float]:
    """The `ai_adjustment` range the engine will honour, read from the same config.

    Taken from the config rather than hardcoded so a judge cannot be handed a range
    wider than the axis it feeds: the engine clamps to `ai_adjustment.min/max` when it
    scores, so a model told +-40 would have its answer silently truncated and the
    reasoning would then explain a number nobody stored.
    """
    cfg = ((tool_context.get("scoring") or {}).get("ai_adjustment")) or {}
    try:
        lo = float(cfg.get("min", -15))
        hi = float(cfg.get("max", 15))
    except (TypeError, ValueError):
        lo, hi = -15.0, 15.0
    return (min(lo, hi), max(lo, hi))


def _pin_completeness_fields(tool_context: dict[str, Any]) -> None:
    """Keep the universal timing pair out of the completeness denominator.

    `fields` does double duty: it is the merge vocabulary AND, when a skill declares no
    `scoring.completeness.fields`, the engine derives the denominator from it
    (`filled / len(fields)`). Appending `event_date`/`event_type` to every source would
    therefore dock every prospect discovered from a source that legitimately has no
    dated event — a property directory, a firm register — for a field we added on their
    behalf and they were right to leave blank.

    Set explicitly rather than by editing the engine: `score_prospects` derives the list
    only when the context does not carry one, so writing it here wins without touching
    the vendored file. A skill that authored its own `completeness.fields` is untouched.

    ⚠️ The pair stays in `sources[*].fields` — that is the merge vocabulary, and dropping
    it there is precisely the `industry` defect of `acafb67`: a key asked for and then
    omitted from the vocabulary arrives and is silently discarded.
    """
    scoring = tool_context.setdefault("scoring", {})
    completeness = scoring.setdefault("completeness", {})
    if completeness.get("fields"):
        return
    canonical = list(als.canonical_fields_from_sources(tool_context.get("sources")))
    scored = [f for f in canonical if f not in UNIVERSAL_TIMING_FIELDS]
    # `or canonical` guards the degenerate case of a source whose ONLY fields are the
    # pair we appended: an empty denominator scores 0 for everyone, silently.
    completeness["fields"] = scored or canonical


def _resolve_signal_fields(
    pipeline_vocab: dict[str, Any], prospects: list[dict[str, Any]]
) -> list[str]:
    """Which keys on a discovered row carry a dated event, for THIS run's data.

    Union of two sources, in this order:

    1. Whatever the skill declared (`pipeline.signal_fields`). An operator's explicit
       naming always survives.
    2. Every key actually present on the run's `discovery_data.by_source` rows whose
       *shape* reads as timing — resolved by `als.timing_fields_from_authored`, the
       engine's own `_TIMING_NAME_HINTS` vocabulary (`timeline|date|completion|due|
       schedule|when`). Deliberately the engine's function and not a second copy: the
       date ladder already answers "which field is a date" this way, and two readers of
       one concept is precisely the drift this phase was built to remove.

    🔑 **Nothing here names a vertical, an org or a place.** `permit_date`,
    `bid_due_date`, `trigger_date` and `estimated_timeline` all resolve on their shape,
    so a skill authored next year works without an edit. The list this replaces was the
    literal pair `["trigger_date", "transaction_date"]` — one skill's field names, which
    scored 0/15 on a second org that had 8 usable dates and 0/497 on a third.

    A false positive costs nothing: the value is handed to a model as text beside its
    event type, and a non-date simply reads as one more attribute. A false NEGATIVE is
    also survivable — the free-text loop prints every string field regardless — so this
    resolution controls labelling, not visibility.
    """
    declared = [str(f) for f in (pipeline_vocab.get("signal_fields") or []) if str(f)]
    present: list[str] = []
    for p in prospects:
        discovery = p.get("discovery_data")
        by_source = discovery.get("by_source") if isinstance(discovery, dict) else None
        for row in (by_source or {}).values():
            if not isinstance(row, dict):
                continue
            for key in row:
                if key not in present:
                    present.append(str(key))
    out = list(declared)
    for name in als.timing_fields_from_authored(present):
        if name not in out:
            out.append(name)
    return out


def _top_ranked(
    prospects: list[dict[str, Any]], scored: list[dict[str, Any]], top_n: int
) -> list[dict[str, Any]]:
    """The `top_n` prospects by scored rank, as full prospect records.

    Contact search needs the prospect's website and discovery detail, which the
    scored projection does not carry — hence the join back rather than using
    `scored` directly.
    """
    ranked_ids = [s.get("prospect_id") for s in scored[: max(top_n, 0)]]
    by_id = {p.get("id"): p for p in prospects}
    return [by_id[i] for i in ranked_ids if i in by_id]


def main() -> int:
    # Under conqrse-queue only TASK_RECORD_ID is injected — the business payload
    # stays in the portal's DB and must be read back by reference. In queue mode the
    # payload WINS over any ambient env: the task record is the authoritative
    # statement of what this run is, and a stale env var silently scanning the wrong
    # org is a worse failure than a missing one.
    try:
        from_queue = bootstrap()
    except BootstrapError as exc:
        # No scan run id is knowable yet, so there is nothing to report the failure
        # against — the queue's own reconciler will mark the Job failed and its logs
        # are where this lands.
        _log(f"bootstrap failed: {exc}")
        return 2
    if from_queue:
        _log(f"queue mode: resolved {', '.join(sorted(from_queue))} from the task payload")

    def _resolve(name: str) -> str | None:
        return from_queue.get(name) or os.environ.get(name)

    org_id = _resolve("ORGANIZATION_ID")
    tenant_id = _resolve("TENANT_ID")
    scan_run_id = _resolve("SCAN_RUN_ID")
    backend_url = os.environ.get("AEO_BACKEND_URL")
    skill_slug = _resolve("SKILL_SLUG")
    # Strictly opt-in: only the exact string the gateway maps counts as a test. Absent,
    # "false", "0", or a typo all read as a REAL run — the only thing this flag does is
    # lower the prospect ceiling, so defaulting it the other way would quietly cap
    # production scans.
    is_test = (_resolve("SCAN_IS_TEST") or "").strip().lower() == "true"

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

    # R12: resolve every `{"context_ref": …}` binding against this org's context
    # BEFORE anything reads the config. A binding left in place reaches a phase as a
    # dict where a list belongs, so the phase searches for nothing and reports
    # success. This is the step that makes one skill serve many orgs.
    try:
        aeo_context = resolve_context_refs(aeo_context)
    except UnresolvedRefError as exc:
        sink.emit_safe({"type": "error", "message": str(exc)})
        _log(str(exc))
        return 1

    phases = selected_phases(_resolve("PHASES"))
    _log(f"phases: {', '.join(phases)}")

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

    _pin_completeness_fields(tool_context)

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

    config = (aeo_context.get("skill") or {}).get("config") or {}

    # Declared up front: geographic enforcement reads this whether or not Phase 0 ran.
    zip_rows: list[dict[str, Any]] = []

    # R11 — load reviewed custom modules. Returns [] at launch (D5), so this is a
    # no-op today; it runs anyway so the gate is exercised by every real scan rather
    # than only by its unit tests.
    #
    # Both blockers that kept loading wired but APPLYING unwired are closed
    # (thread #20, 2026-08-10):
    #   1. `custom_modules` now has a home in the ratified config — the root is
    #      still `additionalProperties: false`, but the key is declared, with the
    #      shape taken from this file's own `REQUIRED_SPEC_FIELDS` so the schema
    #      and the gate cannot drift.
    #   2. Signals persist in `prospects.custom_fields`, riding the `scored`
    #      event. Nothing new had to be invented: `scored` is already the run's
    #      second per-prospect write (contact patches ride it too), and AEO
    #      merges with `custom_fields || custom_signals`, so a replayed callback
    #      is idempotent.
    custom_modules = load_modules(
        config.get("custom_modules"),
        base_dir=Path(__file__).resolve().parent.parent / "modules_data",
        on_reject=lambda name, reason: _log(f"custom module {name}: {reason}"),
    )
    if custom_modules:
        _log(f"loaded {len(custom_modules)} custom module(s)")

    try:
        # ── zip discovery (Phase 0) ───────────────────────────────────────
        # Runs before discovery because its output can widen the search geography.
        if PHASE_ZIP_DISCOVERY in phases:
            targeting = (config.get("geography") or {}).get("targeting") or {}
            zip_rows[:] = discover_zips(
                aeo_context.get("geography") or {},
                provider=provider,
                provider_config=provider_config,
                parse_json_array=als.parse_json_array,
                max_per_market=int(
                    targeting.get("max_zips_per_market", DEFAULT_MAX_ZIPS_PER_MARKET)
                ),
                emit=sink.emit,
            )
            if zip_rows:
                sink.emit({"type": "zip_codes", "items": zip_rows})

            # Recording the fan-out is cheap; SEARCHING it is not — a market
            # expanding to 15 zips multiplies every discovery query by 15. So acting
            # on the zips is a separate, explicit opt-in.
            if targeting.get("use_zip_discovery"):
                cap = int(targeting.get("max_search_zips", 10))
                markets = zips_as_markets(zip_rows, cap)
                if markets:
                    tool_context["organization"]["markets"] = markets
                    _log(f"zip discovery yielded {len(markets)} market(s) to search")
            elif zip_rows:
                _log(
                    f"recorded {len(zip_rows)} zip(s) but NOT searching them "
                    f"(geography.targeting.use_zip_discovery is not set)"
                )

        # Geography as a loop condition, not an instruction. Prompting proved
        # unreliable — identical prompt/image/config gave in-area results standalone
        # and out-of-area results in-container — so candidates are verified against
        # the target area and discovery re-runs (excluding what it already found)
        # until enough of them are genuinely in it. See aeo/phases/geo_loop.py.
        markets = tool_context["organization"].get("markets") or []
        area = build_target_area(zip_rows, markets)
        strictness = str(
            (config.get("geography") or {})
            .get("targeting", {})
            .get("geo_strictness", STRICTNESS_METRO)
        )

        # THE ROOT CAUSE OF THE DRIFT, fixed here. The engine hands `queries` to the
        # model verbatim and never substitutes `{market}` — it does not read
        # `organization.markets` during discovery at all. So an authored query reached
        # the model with the placeholder intact, carrying no geography, and the model
        # answered with the state's most prominent firms. Expanding it is ours to do
        # because the markets are not known until zip discovery has run.
        tool_context["sources"] = expand_queries(
            tool_context["sources"],
            markets,
            max_per_source=int(
                (config.get("geography") or {})
                .get("targeting", {})
                .get("max_queries_per_source", DEFAULT_MAX_QUERIES_PER_SOURCE)
            ),
        )
        stragglers = unexpanded_placeholders(tool_context["sources"])
        if stragglers:
            # A query reaching the model with `{market}` in it returns confident,
            # well-formed, geographically random results — the most expensive failure
            # this repo has seen. Say so loudly rather than searching anyway.
            _log(
                f"WARNING: {len(stragglers)} query/queries still contain "
                f"{MARKET_PLACEHOLDER} and will carry NO geography: {stragglers[0]}"
            )
        else:
            total = sum(len(s.get("queries") or []) for s in tool_context["sources"].values())
            _log(f"expanded to {total} geo-anchored query/queries across {len(markets)} market(s)")

        # The run's prospect ceiling. Cumulative across rounds, applied before any
        # prospect becomes durable — see aeo/phases/prospect_budget.py for why it
        # cannot be a truncation of `discover`'s return value.
        ceiling, ceiling_reason = _resolve_prospect_ceiling(config, is_test)
        budget = ProspectBudget(ceiling)
        if budget.unbounded:
            _log(
                "no prospect ceiling (discovery.max_prospects unset) — every "
                "discovered prospect will be verified, validated and scored"
            )
        else:
            _log(
                f"prospect ceiling: {budget.remaining} for this run ({ceiling_reason})"
            )

        # `emit` is threaded through rather than closed over, because the ceiling has
        # to intercept the engine's `prospects` event before it reaches the sink.
        _discover = capped_discover(
            lambda ctx, emit: als.discover(
                ctx,
                scan_run_id=scan_run_id,
                provider=provider,
                emit=emit,
                provider_config=provider_config,
            ),
            budget=budget,
            emit=sink.emit,
            log=_log,
        )

        geo_rejects: list[dict[str, Any]] = []
        if area.is_empty:
            # Enforcement off: an empty area would reject everything, which is worse
            # than not enforcing. One plain pass.
            _log("geography unknown — verify loop skipped (would reject everything)")
            prospects = _discover(tool_context)
        else:
            targeting = (config.get("geography") or {}).get("targeting") or {}
            prospects, geo_rejects = discover_in_area(
                tool_context=tool_context,
                area=area,
                # How many in-area prospects to keep discovering for — the skill's
                # call, not the deployment's.
                target_count=_config_limit(config, "discovery", "target_prospects"),
                discover=_discover,
                provider=provider,
                provider_config=provider_config,
                parse_json_array=als.parse_json_array,
                strictness=strictness,
                max_rounds=int(targeting.get("max_discovery_rounds", DEFAULT_MAX_ROUNDS)),
                log=_log,
            )
            if geo_rejects:
                _log(
                    f"{len(geo_rejects)} prospect(s) rejected on VERIFIED address — "
                    f"outside {area.describe()}"
                )

        # How many prospects DISCOVERY produced, captured before validation filters
        # `prospects` down to survivors. Reported as `total_prospects` because that is
        # what the `prospects` table holds — every discovered row persists, judged or
        # not. Without this the summary counted survivors, so a run wrote 33 rows and
        # reported 14, duplicating `total_scored` and leaving the discovered count
        # reported nowhere. aeo-frontend saw the mismatch first and asked whether it was
        # "a cap, a filter, or an incomplete pass" — it was none of those, it was this.
        total_discovered = len(prospects)

        # `geo_rejects` is already populated by the verify loop above — rejections
        # there carry a VERIFIED address, which is strictly better evidence than the
        # single-pass check this replaced (that one trusted discovery's own city).

        # ── validation ────────────────────────────────────────────────────
        validations: list[dict[str, Any]] = []
        if PHASE_VALIDATION in phases and config.get("validation"):
            # Signal validation only runs on prospects that are geographically
            # valid — judging in-market signals for a firm in the wrong state is a
            # model call spent to reach a conclusion already known.
            geo_rejected_ids = {v["prospect_id"] for v in geo_rejects}
            validations = validate_prospects(
                [p for p in prospects if p.get("id") not in geo_rejected_ids],
                validation_config=config["validation"],
                provider=provider,
                provider_config=provider_config,
                parse_json_array=als.parse_json_array,
                emit=sink.emit,
            )

        # ── enrichment lanes ──────────────────────────────────────────────
        # N authored lanes, each attaching its own shape of fact to a prospect the
        # verdict above already accepted. Runs BEFORE the `validations` emission so the
        # lane rows travel inside the same `validation_data` the verdict does — one
        # write, one shape, no migration.
        lane_defs = (config.get("validation") or {}).get("lanes")
        if PHASE_VALIDATION in phases and lane_defs:
            # Only prospects that survived geography and qualification. Enriching a
            # prospect already known to be disqualified is spend on a conclusion nobody
            # will read — the same rule validation applies to its own disqualifiers.
            judged_so_far = geo_rejects + validations
            kept = surviving_ids(judged_so_far) if judged_so_far else None
            rejected = {v["prospect_id"] for v in judged_so_far}
            lane_targets = [
                p
                for p in prospects
                if p.get("id")
                and (kept is None or p["id"] not in rejected or p["id"] in kept)
            ]
            lane_results = enrich_prospects(
                lane_targets,
                lanes=lane_defs,
                provider=provider,
                provider_config=provider_config,
                parse_json_array=als.parse_json_array,
                scan_date=today.isoformat(),
                emit=sink.emit,
                log=_log,
            )
            # Merge each prospect's lane rows into its verdict, creating a verdict-shaped
            # entry for a prospect that was never judged (no `validation` section, or it
            # was skipped) so the rows still reach AEO and the scorer.
            by_id = {v["prospect_id"]: v for v in validations}
            for entry in lane_results:
                pid = entry["prospect_id"]
                target = by_id.get(pid)
                if target is None:
                    target = {"prospect_id": pid, "validation_data": {}}
                    validations.append(target)
                    by_id[pid] = target
                target.setdefault("validation_data", {}).update(entry["lanes"])
            _log(
                f"enriched {len(lane_results)} prospects across "
                f"{len(lane_defs)} lane(s)"
            )

        # The scorer reads lane rows off the prospect, so put them there. Without this
        # the rows persist to AEO and are invisible to every factor that binds to a lane.
        if validations:
            data_by_id = {
                v["prospect_id"]: v.get("validation_data") for v in validations
            }
            for p in prospects:
                incoming = data_by_id.get(p.get("id"))
                if isinstance(incoming, dict):
                    existing = p.get("validation_data")
                    p["validation_data"] = (
                        {**existing, **incoming}
                        if isinstance(existing, dict)
                        else dict(incoming)
                    )

        # One `validations` emission covering both kinds of rejection, so AEO sees a
        # single coherent verdict per prospect rather than two competing ones.
        all_verdicts = geo_rejects + validations
        if all_verdicts:
            sink.emit({"type": "validations", "items": all_verdicts})
            keep = surviving_ids(all_verdicts)
            judged = {v["prospect_id"] for v in all_verdicts}
            before = len(prospects)
            # Unjudged prospects are kept — same rule as validation's own.
            prospects = [
                p for p in prospects if p.get("id") not in judged or p.get("id") in keep
            ]
            _log(f"kept {len(prospects)}/{before} prospects after geography + validation")

        # Bracketed because the absence of a log here was read for most of a day as
        # "scoring hangs". It does not: `score_prospects` is pure arithmetic over the
        # already-collected set. The silence belonged to the per-candidate address
        # verification above (concurrency 2, one grounded call each), which on run
        # `cab8c68c` spent 81 minutes on 249 candidates and never reached this line.
        # These two lines make that distinction readable from the log alone.
        # ── AI stage judgment ─────────────────────────────────────────────
        # Before scoring, because the engine reads `_ai_judgment` and
        # `ai_score_adjustment` off the prospect dict when it builds each scored item.
        #
        # Runs on the set that survived geography + validation: judging a prospect that
        # was already rejected is a model call spent to reach a conclusion we hold.
        #
        # ⚠️ Unlike the other model phases this one is NOT gated on a config section.
        # A stage is written for every prospect on every run, and a skill cannot opt out
        # of having one — AEO's ruling is that a completed run always yields a stage. It
        # IS gated on the vocabulary, which `build_tool_context` refuses to omit.
        judgments: dict[str, dict[str, Any]] = {}
        pipeline_vocab = tool_context.get("pipeline") or {}
        if PHASE_SCORING in phases and pipeline_vocab.get("stages"):
            signal_fields = _resolve_signal_fields(pipeline_vocab, prospects)
            _log(
                f"judging {len(prospects)} prospect(s) for pipeline stage; "
                f"timing signal fields: {', '.join(signal_fields) or '(none found)'}"
            )
            judgments = judge_prospects(
                prospects,
                pipeline=pipeline_vocab,
                product_description=tool_context.get("product_description") or "",
                today=str(today),
                provider=provider,
                provider_config=provider_config,
                parse_json_array=als.parse_json_array,
                adjustment_bounds=_adjustment_bounds(tool_context),
                signal_fields=signal_fields,
                emit=sink.emit,
            )
            for p in prospects:
                judged = judgments.get(str(p.get("id")))
                if not judged:
                    continue
                # Two channels, deliberately: `_ai_judgment` carries the stage the
                # engine must prefer over its date ladder, and `ai_score_adjustment` is
                # the field the engine ALREADY read and nothing ever supplied — which
                # is why `ai_analysis` was NULL on all 131 rows of the last real run.
                p["_ai_judgment"] = judged
                p["ai_score_adjustment"] = judged.get("ai_score_adjustment", 0)
            # Counted, not assumed: an unjudged prospect keeps the date ladder, so the
            # split between judged and fallback has to be readable from the log or a
            # degraded run looks like a healthy one.
            _log(
                f"judged {len(judgments)}/{len(prospects)} prospect(s); "
                f"{len(prospects) - len(judgments)} fell back to the date ladder"
            )

        _log(f"scoring {len(prospects)} prospect(s)")
        scored = als.score_prospects(prospects, tool_context, today=today)
        _log(f"scored {len(scored)} prospect(s)")

        # The model's reasoning onto the scored item, so it reaches
        # `prospects.ai_analysis` through `SCORED_PASSTHROUGH`. The engine puts the
        # reasoning in `pipeline_detail` (which AEO has no column for); this is the
        # field that actually lands.
        for item in scored:
            judged = judgments.get(str(item.get("prospect_id")))
            if not judged:
                continue
            if judged.get("ai_analysis"):
                item["ai_analysis"] = judged["ai_analysis"]
            # The evidence beside the verdict. Copied here for the same reason as the
            # reasoning: the engine's scored item is built by the vendored file, which
            # knows nothing about this phase, so the judgment is grafted on after.
            for field in ("signal_event", "signal_date"):
                if judged.get(field):
                    item[field] = judged[field]

        # ── contacts ──────────────────────────────────────────────────────
        # After scoring, deliberately: contact search is the most expensive call
        # per prospect, so it runs on the set that survived validation and only
        # for the top `SCANNER_TOP_N` by rank. Enriching a prospect nobody will
        # look at is spend with no reader.
        if PHASE_CONTACTS in phases and config.get("contacts"):
            targets = _top_ranked(
                prospects, scored, _config_limit(config, "contacts", "max_prospects")
            )
            patches = find_contacts(
                targets,
                contacts_config=config["contacts"],
                product_description=str(tool_context.get("product_description") or ""),
                provider=provider,
                provider_config=provider_config,
                parse_json_array=als.parse_json_array,
                emit=sink.emit,
            )
            scored = merge_into_scored(scored, patches)

        # R11 — custom-module signals, computed from the surviving prospect set
        # and merged onto the scored items so they travel on the one event AEO
        # already upserts per prospect.
        #
        # ⚠️ These signals do NOT influence `score`. Scoring is the vendored
        # engine's `score_prospects`, which ran above and knows nothing about
        # them; the engine is vendored verbatim and is not ours to teach. So a
        # signal is persisted and rendered, not scored on. Saying so here
        # because the neighbouring interface docstring used to imply otherwise.
        if custom_modules:
            signals = apply_modules(
                prospects,
                custom_modules,
                context=tool_context,
                on_error=lambda name, pid, exc: _log(
                    f"custom module {name} failed on prospect {pid}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            if signals:
                scored = merge_signals_into_scored(scored, signals)
                _log(f"custom modules contributed signals for {len(signals)} prospect(s)")

        sink.emit({"type": "scored", "phase": "score", "items": scored})
        sink.emit(
            {
                "type": "completed",
                # ⚠️ All FOUR counters AEO declares, not two. `total_zips` and
                # `total_validated` were never sent, and the event mapper filters to
                # declared keys — so the gateway wrote NULL for both while the rows
                # themselves persisted fine (60 zip rows, `total_zips = null`). The
                # gap read as a gateway defect for a day; it was this dictionary.
                "summary": {
                    "total_zips": len(zip_rows),
                    "total_prospects": total_discovered,
                    "total_validated": len(validations),
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
    code = main()
    # ⚠️ `os._exit`, not `sys.exit`, and this is load-bearing.
    #
    # A per-prospect phase abandons any provider call that exceeds its timeout, but
    # Python cannot kill the thread running it — and since 3.9 `ThreadPoolExecutor`
    # registers an atexit hook that JOINS its workers. So a scan can post every
    # event, flip the run to `completed`, and then sit there until the abandoned
    # call returns on its own. Observed exactly that: all four phases done, prospects
    # and contacts persisted, and the process still alive nine minutes later.
    #
    # For a queue container that is a hang: the task looks stuck long after its work
    # is durable. Every side effect we care about is an HTTP POST that has already
    # been acknowledged by the time we get here, so there is nothing left to flush.
    sys.stderr.flush()
    sys.stdout.flush()
    os._exit(code)
