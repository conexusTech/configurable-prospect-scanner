"""AI pipeline-stage judgment — the step that replaces the engine's date map.

The engine stages a prospect by parsing one date and bucketing the months to it.
That is wrong in a way a date map cannot fix, and a real run proved it: a
`Commercial building permit` dated **2026-02-20** was filed as *"7 - Too Late"*, and a
`New Construction Permit` from **July 2026** as *"6 - Likely Awarded"*. For a flooring
contractor the work FOLLOWS the permit, so a recent past event is the strongest signal
there is. Meanwhile a `Lease` from **December 2019** genuinely is cold. Same direction,
same arithmetic, opposite meanings — because the **event type** decides what the date
implies, and the map never read it.

The type was being collected all along (`transaction_type`, `trigger_type`) and simply
never used.

**Why a model and not a lookup table.** Measured on that run: 19 distinct
`transaction_type` values across 41 rows and 30+ `trigger_type` across 44 — free text,
case-inconsistent, eight spellings of "commercial building permit". Nearly one distinct
value per row. A keyword table over model-generated text is the same mistake as the
church-AV `phase_fallback` it would replace: it works on the vertical it was written
for and silently mis-stages every other one.

Design choices, none forced by the contract:

1. **ONE PROSPECT PER CALL — quality over throughput, by ruling (PO 2026-08-21).**
   Batching is cheaper: 20 per call turns 150 prospects into 8 requests instead of 150,
   against a provider that has already produced `429 RESOURCE_EXHAUSTED` here. It was
   the first design and it was rejected on the right grounds — a long list is precisely
   where a model stops attending to the tail, and the distinction this phase exists to
   draw (a lease *signing* versus a lease *start*; a permit that begins work versus an
   award that ends it) is a nuance, not a lookup. Losing it on prospect 18 of 20 costs
   more than the extra requests do.

   The batching machinery is kept and `DEFAULT_BATCH_SIZE` is 1, so raising it is a
   config change rather than a rewrite if spend ever forces the trade. Bounded
   concurrency and retry — the reason `_concurrent.py` exists — carry the 429 risk, and
   these calls are ungrounded, so far lighter than the grounded validation calls that
   set that bound.
2. **The vocabulary comes from AEO, and is never invented here.** `build_tool_context`
   refuses a context without it. A stage this phase made up would be a column no
   operator defined.
3. **An unjudged prospect is `None`, never a guess.** Same rule as validation's
   `validated: null`: "not judged" must stay distinguishable from "judged and placed
   early", and the caller records *why* on `ai_analysis` so a stage without reasoning
   is visible rather than merely wrong.
4. **The month bounds are given to the model as guidance, not as a rule.** Handing them
   over as arithmetic would reproduce the defect with extra steps.
"""

from __future__ import annotations

from typing import Any, Callable

from aeo.phases._concurrent import (
    DEFAULT_CALL_TIMEOUT_S,
    PHASE_RETRY_ATTEMPTS,
    concurrency_from,
    map_bounded,
)

#: Fields this phase produces per prospect. Named so the shape is a contract rather
#: than whatever the model happened to return.
JUDGMENT_FIELDS = (
    "pipeline_status",
    "stage_score",
    "ai_analysis",
    "ai_score_adjustment",
)

#: Prospects per model call. **1 by ruling, not by accident.**
#:
#: The judgement is a nuance — a lease *signing* versus a lease *start*, a permit that
#: starts work versus an award that ends it — and a model attends to one prospect better
#: than to the eighteenth of twenty. The PO ruled quality over throughput
#: (2026-08-21): take the extra requests.
#:
#: Raising it is a supported trade, not a rewrite: the loop batches either way. If spend
#: forces it, raise it and re-measure the staging against the 131-prospect baseline
#: rather than assuming the judgement survived.
DEFAULT_BATCH_SIZE = 1

_PROMPT = """You are a sales operations analyst placing discovered prospects into a \
pipeline stage for a seller.

WHAT THE SELLER SELLS
{product_description}

TODAY'S DATE
{today}

THE PIPELINE STAGES YOU MAY CHOOSE FROM
{stages}

HOW TO DECIDE
Each prospect carries a dated event. **The TYPE of event decides what its date means \
— never the date alone.**

- A permit, approval, lease signing or property purchase is the START of work, not the \
end of it. Recent ones are the strongest opportunities: the buying decision for what \
this seller sells usually comes AFTER that event, often months after.
- A completed project or an award that has already been made is genuinely late.
- A long-settled fact — "current occupant since 2011", an old lease with no renewal \
signal — is not an event at all. It describes tenure, not timing.
- Distinguish a signing from an occupation or start date. A lease signed last month may \
not be occupied for months, and the fit-out work is still ahead.
- If the event tells you nothing about timing, say so and place the prospect at the \
FIRST stage listed. Do not guess a late stage from a distant date.

The month ranges on the stages are GUIDANCE for ordering them, not a formula. Do not \
compute months and look up a row; reason about when this seller's work would actually \
happen.

ALSO RATE THE FIT
Give a score adjustment from {adj_min} to {adj_max}. Positive when this prospect is a \
better fit for what the seller sells than its raw data suggests; negative when worse. \
0 when you have no basis. This adjusts an existing score — it is not the score.

THE PROSPECTS
{prospects}

Return ONLY a JSON array, one object per prospect, no other text:
[{{"id": "<the id given>", "stage": "<exact stage key from the list>", \
"reasoning": "<one or two sentences: which event, what it implies, why this stage>", \
"adjustment": <number>}}]

Use the stage KEY exactly as written. If you cannot judge a prospect, still return it \
with the first stage listed and say why in the reasoning."""


def _stage_lines(stages: list[dict[str, Any]]) -> str:
    """The vocabulary as the model sees it.

    `description` carries the meaning and is the field an operator should change if
    staging is wrong; the bounds are appended only as ordering guidance. A rung with no
    description still gets listed — omitting it would silently narrow the model's
    choices to a subset of the vocabulary AEO validates against.
    """
    out: list[str] = []
    for s in stages:
        key = s.get("key")
        if not key:
            continue
        bits = [f'- "{key}"']
        if s.get("description"):
            bits.append(str(s["description"]))
        lo, hi = s.get("min_months"), s.get("max_months")
        if lo is not None and hi is not None:
            bits.append(f"(guidance: roughly {lo} to {hi} months from today)")
        if s.get("requires_contact"):
            bits.append("(only if the prospect has a contact route)")
        out.append(" ".join(bits))
    return "\n".join(out)


def _prospect_lines(
    prospects: list[dict[str, Any]], signal_fields: list[str]
) -> str:
    """One block per prospect: identity, then every dated event WITH its type.

    The type/date pairing is the whole point — `transaction_type` beside
    `transaction_date` is what lets "Lease, 2019" and "Permit, 2026-02" reach opposite
    stages. Pairing is by prefix (`trigger_*`, `transaction_*`) so a vertical that
    names its fields differently still pairs, without a per-vertical table.
    """
    blocks: list[str] = []
    for p in prospects:
        lines = [f'id: {p.get("id")}', f'company: {p.get("company_name") or "?"}']
        for field in ("industry", "address", "city", "state"):
            if p.get(field):
                lines.append(f"{field}: {p[field]}")

        seen: set[str] = set()
        for src_name, row in (p.get("_by_source") or {}).items():
            if not isinstance(row, dict):
                continue
            for date_field in signal_fields:
                date_val = row.get(date_field)
                if not (isinstance(date_val, str) and date_val.strip()):
                    continue
                prefix = date_field.rsplit("_", 1)[0]
                type_val = row.get(f"{prefix}_type") or ""
                entry = f"event: {type_val or '(type not given)'} - dated {date_val} (source: {src_name})"
                if entry not in seen:
                    seen.add(entry)
                    lines.append(entry)

            # Any other free text the source returned about this prospect: the meeting's
            # "the output has no WHY" complaint applies to the judge too — it cannot
            # weigh a signal it was never shown.
            for k, v in row.items():
                if k in signal_fields or k.endswith("_type"):
                    continue
                if k in ("company_name", "industry", "address", "city", "state", "website"):
                    continue
                if isinstance(v, str) and v.strip() and len(v) < 300:
                    entry = f"{k}: {v}"
                    if entry not in seen:
                        seen.add(entry)
                        lines.append(entry)

        if not any(l.startswith("event:") for l in lines):
            lines.append("event: none found - no dated signal for this prospect")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _chunk(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def judge_prospects(
    prospects: list[dict[str, Any]],
    *,
    pipeline: dict[str, Any],
    product_description: str,
    today: str,
    provider: Callable[..., str],
    provider_config: dict[str, Any],
    parse_json_array: Callable[[str], list[dict[str, Any]]],
    adjustment_bounds: tuple[float, float] = (-15.0, 15.0),
    batch_size: int = DEFAULT_BATCH_SIZE,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Judge each prospect's stage and fit. Returns `{prospect_id: {...JUDGMENT_FIELDS}}`.

    A prospect the model did not judge is **absent from the result**, not present with a
    guess — the caller decides the fallback and records that it was one.
    """
    stages = [s for s in (pipeline.get("stages") or []) if isinstance(s, dict) and s.get("key")]
    if not stages or not prospects:
        return {}

    valid_keys = {str(s["key"]) for s in stages}
    # Weight per rung, so the SCORE the engine assigns matches the STAGE the judge
    # chose. Absent means the vocabulary does not weight its rungs; the engine then
    # falls back to its own table rather than inventing a number.
    stage_scores = {
        str(s["key"]): s.get("score") for s in stages if s.get("score") is not None
    }
    entry_key = str(stages[0]["key"])
    signal_fields = [
        str(f) for f in (pipeline.get("signal_fields") or ["trigger_date", "transaction_date"])
    ]
    adj_min, adj_max = adjustment_bounds
    stage_block = _stage_lines(stages)

    targets = [p for p in prospects if p.get("id")]
    batches = _chunk(targets, max(1, int(batch_size)))

    if emit:
        emit(
            {
                "type": "phase_start",
                "phase": "ai_judgment",
                "prospects": len(targets),
                "batches": len(batches),
            }
        )

    def _judge_batch(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prompt = _PROMPT.format(
            product_description=product_description or "(not specified)",
            today=today,
            stages=stage_block,
            adj_min=adj_min,
            adj_max=adj_max,
            prospects=_prospect_lines(batch, signal_fields),
        )
        raw = provider(
            prompt,
            # The judgment tier, falling back to the shared model. Reasoning quality
            # converts directly into a correct sales stage here, which is not true of
            # the retrieval phases — see `judgment_model` in DEFAULT_PROVIDER for the
            # measurement that justifies the second tier.
            model=provider_config.get("judgment_model") or provider_config.get("model"),
            temperature=provider_config.get("temperature", 0.1),
            retry_attempts=PHASE_RETRY_ATTEMPTS,
            timeout_s=DEFAULT_CALL_TIMEOUT_S,
        )
        return parse_json_array(raw)

    results = map_bounded(
        batches,
        _judge_batch,
        max_concurrency=concurrency_from(provider_config),
        timeout_s=DEFAULT_CALL_TIMEOUT_S,
    )

    judged: dict[str, dict[str, Any]] = {}
    for batch, parsed in zip(batches, results):
        by_id = {str(p.get("id")): p for p in batch}
        for item in parsed or []:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("id") or "")
            if pid not in by_id or pid in judged:
                # An id the model invented, or a duplicate. Dropping it leaves the
                # prospect unjudged, which the caller handles — accepting it would
                # attach a verdict to the wrong company.
                continue

            stage = str(item.get("stage") or "").strip()
            reasoning = str(item.get("reasoning") or "").strip()
            if stage not in valid_keys:
                # Out of vocabulary. Keep the reasoning and fall to the entry rung
                # rather than discarding the whole judgement, but say what happened —
                # a silently corrected stage is indistinguishable from a judged one.
                reasoning = (
                    f"[stage {stage!r} is not in this skill's vocabulary; placed at "
                    f"the entry stage] {reasoning}".strip()
                )
                stage = entry_key

            try:
                adj = float(item.get("adjustment") or 0)
            except (TypeError, ValueError):
                adj = 0.0
            # Clamped, not rejected: a model returning 40 means "very good fit", and
            # honouring the sign while bounding the magnitude keeps that.
            adj = max(adj_min, min(adj_max, adj))

            judged[pid] = {
                "pipeline_status": stage,
                "stage_score": stage_scores.get(stage),
                "ai_analysis": reasoning or None,
                "ai_score_adjustment": adj,
            }

    if emit:
        emit(
            {
                "type": "phase_complete",
                "phase": "ai_judgment",
                "judged": len(judged),
                "unjudged": len(targets) - len(judged),
            }
        )
    return judged
