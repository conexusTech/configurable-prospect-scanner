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

from aeo.phases._batching import (
    BATCH_OUTPUT_RULES,
    chunk,
    numbered_input,
    reconcile,
)
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

THE SELLER'S EXPLICIT REQUIREMENTS (thresholds and constraints they stated)
{rules}

A requirement you CAN evaluate and the prospect FAILS is a disqualifier: say so in
`disqualifiers_hit` using the requirement's own wording. A requirement you cannot
evaluate from the evidence available is NOT a failure — leave it out and do not guess
a value in order to judge it.

Decide, using only the prospect information above plus what you can ground from
public sources. Do not invent facts about the prospect.

Return a JSON array with exactly one object:
[{{"validated": true|false,
   "signals_found": ["<signal text you judged present>"],
   "disqualifiers_hit": ["<disqualifier text that applies>"],
   "reasoning": "<one or two sentences>"}}]

`validated` is false if any disqualifier applies, or if no in-market signal is
present. Return only the JSON array."""


#: Prospects per validation call.
#:
#: 🔴 **1 — deliberately, and NOT because §4 forbids more.** §4's table permits 8 for a
#: single-signal phase, worth 111 grounded calls on the reference run (20% of it). It is
#: not taken because this phase decides `validated`, and a false verdict REMOVES the lead:
#:
#:   *"Recording an unjudged prospect as invalid would silently shrink every result set,
#:   and nothing would look wrong — the failure mode this whole feature keeps producing."*
#:
#: A degraded enrichment call returns fewer rows, which is visible. A degraded validation
#: call returns fewer PROSPECTS, which is indistinguishable from a thin market. §6's bar
#: for a signal whose wrong answer zeroes a lead is an accuracy A/B against a known
#: sample, judged on correctness and never on row count.
#:
#: `scripts/validation_batch_ab.py` is that A/B. **IT WAS RUN 2026-08-31 AT BATCH 8 AND
#: IT FAILED — decisively.** 20 real consulting prospects, balanced 10 previously-validated
#: / 10 previously-rejected, control arm complete (judged all 20):
#:
#:     agreed            6 / 20
#:     True -> None     11   the batch DROPPED the prospect
#:     True -> False     3   the batch wrongly REJECTED a qualified lead
#:
#: 🔴 **14 of 20 qualified leads lost.** The 11 is not a multiple of the batch size, so
#: these are partial drops from WITHIN batches — the genuine dilution signal, not a
#: timed-out call. Exactly §4's *"an oversized batch degrades silently by thinning results
#: per entity and dropping entities from the response, both of which look like 'the data
#: wasn't out there'."*
#:
#: Shipping this for its 111-call saving would have returned a customer roughly a third of
#: their real qualified leads, with no error anywhere. **Do not raise this constant to 8.**
#: §10's smaller sizes (5, 3) are untested here and the value falls with them; measure
#: before assuming a smaller batch is safe, because the failure at 8 was not marginal.
#:
#: Evidence: `<scratchpad>/ab-run2.log`. An earlier run at concurrency 2 is INVALID —
#: its control arm failed 9 of 20 calls on timeouts, so it measured infrastructure.
DEFAULT_VALIDATION_BATCH = 1

_BATCHED_PROMPT = """Judge each organization below against the seller's criteria.

ORGANIZATIONS — {count} of them, numbered. Judge every one INDEPENDENTLY.
{prospects}

THE SELLER IS LOOKING FOR THESE IN-MARKET SIGNALS
{signals}

ANY OF THESE DISQUALIFIES A PROSPECT OUTRIGHT
{disqualifiers}

THE SELLER'S EXPLICIT REQUIREMENTS (thresholds and constraints they stated)
{rules}

A requirement you CAN evaluate and a prospect FAILS is a disqualifier: say so in that
prospect's `disqualifiers_hit` using the requirement's own wording. A requirement you
cannot evaluate from the evidence available is NOT a failure — leave it out and do not
guess a value in order to judge it.

Decide using only the information above plus what you can ground from public sources.
Do not invent facts about any prospect.

⚠️ Judge each organization on ITS OWN evidence. Evidence about one organization says
nothing about another, and a disqualifier that applies to one does not carry to the rest.

OUTPUT
Return a JSON array of exactly {count} objects, one per organization:

{{
  "n": <the organization's number from the list above>,
  "company_name": "<its exact official name, copied from the list>",
  "validated": true|false,
  "signals_found": ["<signal text you judged present>"],
  "disqualifiers_hit": ["<disqualifier text that applies>"],
  "reasoning": "<one or two sentences>"
}}

{batch_rules}

`validated` is false for a prospect if any disqualifier applies to IT, or if no in-market
signal is present for IT. Return only the JSON array."""


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
    batch_size: int = DEFAULT_VALIDATION_BATCH,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """Judge each prospect. Returns `[{prospect_id, validation_data}]`.

    `batch_size` is 1 by default and the single-prospect prompt is used unchanged at
    that size — so this phase's behaviour is byte-identical until someone raises it.
    See `DEFAULT_VALIDATION_BATCH` for why it is not already higher.

    `validation_config` is the config's `validation` section **already resolved** —
    its `in_market_signals` and `disqualifiers` are two of the nine R12-bound
    positions, so they arrive as bindings and must be resolved before this runs.
    """
    signals = _as_lines(validation_config.get("in_market_signals"))
    disqualifiers = _as_lines(validation_config.get("disqualifiers"))
    # `rules` carries the operator's explicit thresholds (e.g. min_square_footage).
    # ⚠️ It was authored by every CSB-built skill and read by NOTHING until 2026-08-12:
    # a real run carried `min_square_footage: 10000` while the judgement never saw it.
    rules = _as_lines(validation_config.get("rules"))

    if emit:
        emit({"type": "phase_start", "phase": "validation"})

    targets = [p for p in prospects if p.get("id")]

    size = max(1, int(batch_size or 1))

    def _ask(prompt: str) -> str:
        return provider(
            prompt,
            model=provider_config.get("model"),
            temperature=provider_config.get("temperature", 0.1),
            # 1 attempt, not the config value — see PHASE_RETRY_ATTEMPTS.
            retry_attempts=PHASE_RETRY_ATTEMPTS,
            timeout_s=DEFAULT_CALL_TIMEOUT_S,
            phase="validation",
        )

    def _judge_one(prospect: dict[str, Any]) -> dict[str, Any]:
        raw = _ask(
            _PROMPT.format(
                prospect=_prospect_summary(prospect),
                signals=signals,
                disqualifiers=disqualifiers,
                rules=rules,
            )
        )
        parsed = parse_json_array(raw)
        return parsed[0] if parsed else {}

    def _judge_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
        raw = _ask(
            _BATCHED_PROMPT.format(
                count=len(batch),
                prospects=numbered_input(batch, _prospect_summary),
                signals=signals,
                disqualifiers=disqualifiers,
                rules=rules,
                batch_rules=BATCH_OUTPUT_RULES.format(name_key="company_name"),
            )
        )
        return reconcile(batch, parse_json_array(raw) or [])

    # Bounded + timeout-enforced: the provider ignores `timeout_s`, so a sequential
    # loop here has no upper bound at all. See aeo/phases/_concurrent.py.
    if size == 1:
        # The unchanged path. Kept verbatim rather than routed through the batched one
        # at size 1, so "we did not change the default" is true of the PROMPT too — an
        # A/B whose control arm was silently rewritten measures nothing.
        verdicts = map_bounded(
            targets,
            _judge_one,
            max_concurrency=concurrency_from(provider_config),
            timeout_s=DEFAULT_CALL_TIMEOUT_S,
            log=log,
            label="prospects",
        )
    else:
        batches = chunk(targets, size)
        answered = map_bounded(
            batches,
            _judge_batch,
            max_concurrency=concurrency_from(provider_config),
            timeout_s=DEFAULT_CALL_TIMEOUT_S,
            log=log,
            label="batches",
        )
        verdicts = []
        for batch, matched in zip(batches, answered):
            rows = matched or [None] * len(batch)
            verdicts.extend(rows)

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
