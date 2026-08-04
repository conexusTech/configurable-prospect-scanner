"""Signal-validation phase — the PRD's third scan phase.

The vendored engine implements discovery and scoring. This adds the step between
them: for each discovered prospect, judge whether the org's **in-market signals**
are present and whether any **disqualifier** applies, then emit a `validations`
event so AEO persists the verdict on `prospects.validation_data` / `validated_at`.

Two design choices worth stating, because neither is forced by the contract:

1. **Disqualifiers are decided before signals are scored.** A disqualified prospect
   gets no signal work done on it — that is a model call per prospect saved, and it
   avoids the incoherent record where something is "strongly in-market" *and*
   excluded.
2. **An unparseable model response is `validated: null`, never `False`.** Absence of
   evidence is not disqualification. Recording an unjudged prospect as invalid would
   silently shrink every result set, and nothing would look wrong — the failure mode
   this whole feature keeps producing.

Reuses the engine's `SearchProvider` and `parse_json_array` rather than opening a
second model client: one provider, one retry policy, one place to change.
"""

from __future__ import annotations

from typing import Any, Callable

from aeo.phases._concurrent import (
    DEFAULT_CALL_TIMEOUT_S,
    PHASE_RETRY_ATTEMPTS,
    concurrency_from,
    map_bounded,
)

# Fields the phase writes into `validation_data` (an open JSONB column in AEO).
# Named here so the shape is a contract rather than whatever the model returned.
VALIDATION_FIELDS = ("validated", "signals_found", "disqualifiers_hit", "reasoning")

_PROMPT = """You are qualifying a discovered business prospect for a seller.

PROSPECT
{prospect}

THE SELLER IS LOOKING FOR THESE IN-MARKET SIGNALS
{signals}

ANY OF THESE DISQUALIFIES THE PROSPECT OUTRIGHT
{disqualifiers}

Decide, using only the prospect information above plus what you can ground from
public sources. Do not invent facts about the prospect.

Return a JSON array with exactly one object:
[{{"validated": true|false,
   "signals_found": ["<signal text you judged present>"],
   "disqualifiers_hit": ["<disqualifier text that applies>"],
   "reasoning": "<one or two sentences>"}}]

`validated` is false if any disqualifier applies, or if no in-market signal is
present. Return only the JSON array."""


def _as_lines(value: Any) -> str:
    """Render a resolved context value as prompt lines. Never returns empty."""
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
        return "\n".join(f"- {i}" for i in items) if items else "(none specified)"
    if isinstance(value, dict):
        items = [f"- {k}: {v}" for k, v in value.items() if v]
        return "\n".join(items) if items else "(none specified)"
    text = str(value).strip() if value is not None else ""
    return f"- {text}" if text else "(none specified)"


def _prospect_summary(prospect: dict[str, Any]) -> str:
    parts = [
        f"name: {prospect.get('company_name') or '(unknown)'}",
        f"location: {prospect.get('city') or '?'}, {prospect.get('state') or '?'}",
    ]
    if prospect.get("website"):
        parts.append(f"website: {prospect['website']}")
    discovery = prospect.get("discovery_data") or {}
    by_source = discovery.get("by_source") if isinstance(discovery, dict) else None
    if isinstance(by_source, dict):
        for source, row in by_source.items():
            if isinstance(row, dict):
                detail = "; ".join(f"{k}={v}" for k, v in row.items() if v)
                if detail:
                    parts.append(f"found via {source}: {detail}")
    return "\n".join(parts)


def validate_prospects(
    prospects: list[dict[str, Any]],
    *,
    validation_config: dict[str, Any],
    provider: Callable[..., str],
    provider_config: dict[str, Any],
    parse_json_array: Callable[[str], list[dict[str, Any]]],
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Judge each prospect. Returns `[{prospect_id, validation_data}]`.

    `validation_config` is the config's `validation` section **already resolved** —
    its `in_market_signals` and `disqualifiers` are two of the nine R12-bound
    positions, so they arrive as bindings and must be resolved before this runs.
    """
    signals = _as_lines(validation_config.get("in_market_signals"))
    disqualifiers = _as_lines(validation_config.get("disqualifiers"))

    if emit:
        emit({"type": "phase_start", "phase": "validation"})

    targets = [p for p in prospects if p.get("id")]

    def _judge(prospect: dict[str, Any]) -> dict[str, Any]:
        prompt = _PROMPT.format(
            prospect=_prospect_summary(prospect),
            signals=signals,
            disqualifiers=disqualifiers,
        )
        raw = provider(
            prompt,
            model=provider_config.get("model"),
            temperature=provider_config.get("temperature", 0.1),
            # 1 attempt, not the config value — see PHASE_RETRY_ATTEMPTS.
            retry_attempts=PHASE_RETRY_ATTEMPTS,
            timeout_s=DEFAULT_CALL_TIMEOUT_S,
        )
        parsed = parse_json_array(raw)
        return parsed[0] if parsed else {}

    # Bounded + timeout-enforced: the provider ignores `timeout_s`, so a sequential
    # loop here has no upper bound at all. See aeo/phases/_concurrent.py.
    verdicts = map_bounded(
        targets,
        _judge,
        max_concurrency=concurrency_from(provider_config),
        timeout_s=DEFAULT_CALL_TIMEOUT_S,
    )

    results: list[dict[str, Any]] = []
    for prospect, verdict in zip(targets, verdicts):
        # `validated: None` on a failed/unparseable/timed-out call — see the module
        # docstring. "Not judged" must stay distinguishable from "judged and
        # rejected".
        verdict = verdict or {}
        data = {
            "validated": verdict.get("validated") if verdict else None,
            "signals_found": verdict.get("signals_found") or [],
            "disqualifiers_hit": verdict.get("disqualifiers_hit") or [],
            "reasoning": verdict.get("reasoning") or "",
        }
        results.append({"prospect_id": prospect["id"], "validation_data": data})

    if emit:
        emit({"type": "phase_complete", "phase": "validation", "count": len(results)})
    return results


def surviving_ids(validations: list[dict[str, Any]]) -> set[str]:
    """Prospect ids that were not disqualified.

    **Includes unjudged prospects (`validated is None`).** Dropping them would let a
    transient model failure quietly delete prospects from the run, which is worse
    than carrying one through to scoring with an empty verdict.
    """
    keep: set[str] = set()
    for entry in validations:
        data = entry.get("validation_data") or {}
        if data.get("validated") is not False:
            keep.add(entry["prospect_id"])
    return keep
