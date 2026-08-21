"""Map the engine's emitted events onto AEO's durable scan-event contract.

The mirror of `config_mapping.py`: that one translates config going *in*, this one
translates data coming *out*. Both exist because the vendored engine predates AEO
and must not be edited to suit it (UPSTREAM.md).

Verified against aeo-backend's `scan-event.dto.ts`, not against documentation —
every field name and constraint below was read off the DTO.

Three things this fixes that a naive pass-through gets wrong:

1. **`phase` / `phase_name` are per-ITEM in AEO, per-EVENT in the engine.** The
   engine emits `{"type": "prospects", "phase": "discover", "items": [...]}` with no
   phase on the items; AEO requires both on each item. Pass the event through
   unchanged and every item fails validation.
2. **AEO caps an event at 1000 items** (`@ArrayMaxSize`). The engine emits one
   `prospects` event for the whole discovery sweep, however large. Over the cap the
   entire callback 400s — losing the whole sweep, not the overflow.
3. **`pipeline_status` means two different things.** See PIPELINE_STATUS_HAZARD.
"""

from __future__ import annotations

from typing import Any, Iterator

# AEO's `@ArrayMaxSize(MAX_ITEMS_PER_EVENT)` on every data event.
MAX_ITEMS_PER_EVENT = 1000

# Human-readable names for the engine's phases. The engine's `phase` value is a
# source key during discovery ("church_architects") or literally "discover"/"score";
# AEO wants an identifier AND a display name, so anything unmapped falls back to a
# de-slugged version of the identifier rather than an empty string.
PHASE_NAMES = {
    "discover": "Discovery sweep",
    "score": "Scoring",
}

# Fields on the engine's `scored` item that AEO's ScanScoredItemDto declares
# top-level. Everything else is folded into `scoring_payload`.
#
#: ⚠️ Same hazard as PROSPECT_PASSTHROUGH below, and it bit twice. The five contact
#: fields were added 2026-08-18: `aeo/phases/contacts.py` writes `contact_title` /
#: `contact_email` / `contact_phone` / `contact_linkedin` / `contacts_data` onto every
#: prospect it enriches, AEO's `ScanScoredItemDto` declares all five and the ingest
#: UPSERTs them — but this tuple named only `contact_name`, so a run that found 17
#: contacts persisted 17 names and zero emails. `contact_name` arriving while the other
#: five did not is the fingerprint of this bug: one whitelist, one field listed.
SCORED_PASSTHROUGH = (
    "prospect_id",
    "contact_name",
    "contact_title",
    "contact_email",
    "contact_phone",
    "contact_linkedin",
    "contacts_data",
    "score",
    "rank",
    "score_factors",
    # Added 2026-08-21 with the AI judgment phase, and the THIRD time this exact
    # omission has shipped from this one tuple -- after `contact_name` alone (17
    # contacts persisted, zero emails) and after `industry`/`website`. AEO declares
    # both on `ScanScoredItemDto` and the callback UPSERTs both; without them the
    # model reasoning is computed, paid for, and dropped one line before the wire.
    "ai_analysis",
    "ai_score_adjustment",
)

# Fields on the engine's `prospects` item that map straight onto AEO columns.
#: ⚠️ **A field absent here can never reach its column, however well it was collected.**
#: `industry` and `zip_code` were added 2026-08-12: both are declared on AEO's
#: `ScanProspectItemDto`, both were being collected (12 of 36 prospects carried an
#: `industry` on the first real production run), and both were stranded in
#: `discovery_data` with the columns left NULL because this tuple did not name them.
PROSPECT_PASSTHROUGH = (
    "id",
    "company_name",
    "industry",
    "city",
    "state",
    "zip_code",
    "address",
    "website",
    "contact_name",
    "contact_title",
    "sources",
    "discovery_data",
)

#: `pipeline_status` travels inside `scoring_payload`, and that is the DESIGNED
#: channel — corrected 2026-08-04 after a live run.
#:
#: An earlier version of this file called it a name collision and warned against
#: mapping it. That was wrong, and worth recording because the wrong version was
#: persuasive: the engine's value comes from `calculate_pipeline()`, AEO has an
#: operator-driven `PATCH /prospects/:id/pipeline-status`, and the names match — so
#: "two meanings, one name" looked obvious. What the live run showed instead:
#:
#: - AEO's `scoring_payload` is documented as *"Carries the resolved
#:   `pipeline_status` … not persisted as a column — read transiently for pipeline
#:   assignment"*. It exists for this one field.
#: - Assignment is **set-once** (`COALESCE(p.pipeline_status, …)`), so a skill seeds
#:   the initial value and an operator's later edit is never overwritten.
#: - `customer` skills are trusted verbatim; `project` skills are validated against
#:   `TIMELINE_STAGES` and abstain (NULL) on an unknown key.
#: - The engine's emitted value (`"6 - Likely Awarded"`) is a **first-class key in
#:   AEO's own `TIMELINE_STAGES`**, whose stage descriptions read "months to AV
#:   decision". AEO's pipeline vocabulary was itself derived from this domain.
#:
#: So the two sides agree because they share an origin. Keep sending it here.
PIPELINE_STATUS_KEY = "pipeline_status"


def phase_name_for(phase: str) -> str:
    """Display name for a phase identifier. Never returns empty — AEO requires it."""
    if phase in PHASE_NAMES:
        return PHASE_NAMES[phase]
    return phase.replace("_", " ").replace("-", " ").strip().capitalize() or "Unknown"


def _chunk(items: list[Any]) -> Iterator[list[Any]]:
    """Split into AEO-sized batches. One event in, zero-or-more events out.

    **Yields nothing for an empty list**, deliberately. AEO declares
    `@ArrayMinSize(1)` on every data array, so posting `{"data": []}` is a 400 —
    which would turn "this sweep legitimately found no prospects" into a failed
    scan run. A zero-result scan is a real outcome, not an error.
    """
    for start in range(0, len(items), MAX_ITEMS_PER_EVENT):
        yield items[start : start + MAX_ITEMS_PER_EVENT]


def map_prospects_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Engine `prospects` event → one or more AEO `prospects` payloads."""
    phase = str(event.get("phase") or "discover")
    display = phase_name_for(phase)

    mapped = []
    for item in event.get("items") or []:
        if not isinstance(item, dict):
            continue
        out = {k: item[k] for k in PROSPECT_PASSTHROUGH if item.get(k) is not None}
        # Required by AEO, absent from the engine's items: stamped from the event.
        out["phase"] = phase
        out["phase_name"] = display
        mapped.append(out)

    return [{"data": batch} for batch in _chunk(mapped)]


def map_scored_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Engine `scored` event → one or more AEO `scored` payloads.

    Everything the DTO does not declare top-level is preserved under
    `scoring_payload` rather than dropped: the engine's vertical-shaped extras
    (`denomination`, `campaign_goal`, `project_type`, …) are real signal a
    salesperson wants, they just are not columns. Dropping them would lose data the
    scan paid a model to produce.
    """
    mapped = []
    for item in event.get("items") or []:
        if not isinstance(item, dict):
            continue
        out = {k: item[k] for k in SCORED_PASSTHROUGH if item.get(k) is not None}

        # `scoring_payload` carries `pipeline_status` and NOTHING ELSE.
        #
        # It is tempting to use it as a bucket for the engine's remaining fields
        # (`denomination`, `campaign_goal`, `project_type`, …) — an earlier version
        # of this file did. But AEO reads exactly one key out of it and discards the
        # rest ("not persisted as a column — read transiently"), so a bucket here
        # creates a convincing illusion of durability: the wire shows rich data, the
        # database keeps none of it, and nothing errors.
        #
        # Those fields are not lost by omitting them. They are already durable in
        # `discovery_data.by_source` on the `prospects` event, which IS persisted as
        # an open JSONB column — the scored copies are a flattened re-projection of
        # data AEO already stored.
        # ⚠️ ONE exception to "nothing else", added 2026-08-21: `pipeline_source`.
        # AEO cannot trust a bare stage from a customer skill, because the engine's
        # `calculate_pipeline` fallback derives it from months-to-DECISION and that
        # arithmetic is wrong for event dates — so AEO discarded it and re-derived,
        # which silently threw away every LLM verdict (measured: '4 - Active Pursuit'
        # persisted as '7 - Too Late'). This marker is what lets AEO tell a JUDGED
        # stage from the date fallback and keep the former. Unlike the engine extras
        # above it is not merely durable elsewhere — nothing else on the wire carries it.
        status = item.get(PIPELINE_STATUS_KEY)
        if isinstance(status, str) and status:
            payload = {PIPELINE_STATUS_KEY: status}
            source = item.get("pipeline_source")
            if isinstance(source, str) and source:
                payload["pipeline_source"] = source
            out["scoring_payload"] = payload

        # `prospect_id` is the only required field; an item without it cannot be
        # attached to anything, so it is dropped loudly rather than sent to 400.
        if not out.get("prospect_id"):
            continue
        mapped.append(out)

    return [{"data": batch} for batch in _chunk(mapped)]


def map_zip_codes_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Zip-discovery event → one or more AEO `zip_codes` payloads.

    Rows arrive already validated and shaped by `aeo/phases/zip_discovery.py` — only
    `zip_code` is required, so a row without one is dropped rather than sent to a 400.
    """
    mapped = [
        item
        for item in (event.get("items") or [])
        if isinstance(item, dict) and item.get("zip_code")
    ]
    return [{"data": batch} for batch in _chunk(mapped)]


def map_validations_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Validation-phase event → one or more AEO `validations` payloads.

    `ScanValidationItemDto` is just `{prospect_id, validation_data?}` — the verdict
    shape is ours to define, and it is pinned in `aeo/phases/validation.py`.
    """
    mapped = []
    for item in event.get("items") or []:
        if not isinstance(item, dict) or not item.get("prospect_id"):
            continue
        out: dict[str, Any] = {"prospect_id": item["prospect_id"]}
        if isinstance(item.get("validation_data"), dict):
            out["validation_data"] = item["validation_data"]
        mapped.append(out)
    return [{"data": batch} for batch in _chunk(mapped)]


def map_event(event: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Engine event → list of (aeo_event_type, payload) to POST, in order.

    Returns a list because one engine event can become several AEO callbacks
    (the 1000-item cap). An unmapped or progress-only event returns `[]`, which
    the caller should log rather than treat as an error — `phase_start` and
    `phase_complete` are legitimately high-frequency and have no AEO destination.
    """
    etype = event.get("type")
    if etype == "prospects":
        return [("prospects", p) for p in map_prospects_event(event)]
    if etype == "scored":
        return [("scored", p) for p in map_scored_event(event)]
    if etype == "validations":
        return [("validations", p) for p in map_validations_event(event)]
    if etype == "zip_codes":
        return [("zip_codes", p) for p in map_zip_codes_event(event)]
    if etype == "completed":
        # Only the four counters AEO's ScanCompletedSummaryDto declares. Anything
        # else (e.g. the engine's provider name) is dropped rather than sent: the
        # global ValidationPipe whitelists, and relying on it to strip an extra key
        # makes the payload depend on a pipe setting rather than on this contract.
        summary = event.get("summary") or {}
        return [
            (
                "completed",
                {
                    "summary": {
                        k: summary[k]
                        for k in (
                            "total_zips",
                            "total_prospects",
                            "total_validated",
                            "total_scored",
                        )
                        if isinstance(summary.get(k), (int, float))
                    }
                },
            )
        ]
    if etype == "error":
        return [("error", {"message": event.get("message", "")})]
    return []
