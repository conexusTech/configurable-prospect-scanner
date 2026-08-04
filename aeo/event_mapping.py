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
SCORED_PASSTHROUGH = (
    "prospect_id",
    "contact_name",
    "score",
    "rank",
    "score_factors",
)

# Fields on the engine's `prospects` item that map straight onto AEO columns.
PROSPECT_PASSTHROUGH = (
    "id",
    "company_name",
    "city",
    "state",
    "address",
    "website",
    "discovery_data",
)

#: ⚠️ **`pipeline_status` IS A NAME COLLISION AND MUST NOT BE MAPPED THROUGH.**
#:
#: The engine's `pipeline_status` comes from `calculate_pipeline()` — a *construction
#: project* stage inferred from timeline arithmetic ("in campaign", "breaking
#: ground"). AEO's `prospects.pipeline_status` is the **sales** pipeline workflow an
#: operator drives by hand via `PATCH /prospects/:id/pipeline-status`.
#:
#: Same name, unrelated meanings. Mapping one onto the other would silently
#: overwrite an operator's sales state with a construction-timeline string, on every
#: scan, with nothing erroring. It is harmless today only because AEO's scored DTO
#: has no top-level `pipeline_status` — so this constant exists to make the trap
#: explicit for whoever next reads two identically-named fields and connects them.
#:
#: This is the sixth name collision this feature has produced. The other five each
#: cost a defect.
PIPELINE_STATUS_HAZARD = "pipeline_status"


def phase_name_for(phase: str) -> str:
    """Display name for a phase identifier. Never returns empty — AEO requires it."""
    if phase in PHASE_NAMES:
        return PHASE_NAMES[phase]
    return phase.replace("_", " ").replace("-", " ").strip().capitalize() or "Unknown"


def _chunk(items: list[Any]) -> Iterator[list[Any]]:
    """Split into AEO-sized batches. One event in, one-or-more events out."""
    if not items:
        yield []
        return
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

    return [{"items": batch} for batch in _chunk(mapped)]


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

        # The overflow, including `pipeline_status`, which deliberately does NOT
        # travel top-level — see PIPELINE_STATUS_HAZARD.
        payload = {
            k: v for k, v in item.items() if k not in SCORED_PASSTHROUGH
        }
        if payload:
            out["scoring_payload"] = payload

        # `prospect_id` is the only required field; an item without it cannot be
        # attached to anything, so it is dropped loudly rather than sent to 400.
        if not out.get("prospect_id"):
            continue
        mapped.append(out)

    return [{"items": batch} for batch in _chunk(mapped)]


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
    if etype == "completed":
        return [("completed", {"summary": event.get("summary", {})})]
    if etype == "error":
        return [("error", {"message": event.get("message", "")})]
    return []
