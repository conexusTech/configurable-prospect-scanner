"""Phase 2b — re-score prospects already in the database, from data already held.

**Ruled 2026-08-31.** Forward-only was the rule until the mixed-scale consequence was
weighed: with nothing re-scored, one org's list holds a good lead at 52 beside an equally
good lead at 93 **permanently**, because a later run SKIPS a company it has already seen
(`runtime-scan-events.service.ts` — "SKIP, not update") rather than refreshing it. So the
old scores never age out on their own.

🔴 **This reads stored rows and performs NO discovery.** No search, no browser, no
grounded request — the inputs are the `validation_data` and `discovery_data` the original
scan already paid for. That is the whole reason it is affordable: run `741b7b3b` cost
**$58.08** across 223 calls and 3,183 grounded search queries, and re-scoring costs none
of it. The only spend is the optional explanation pass, which is non-grounded.

⚠️ **It does NOT generalise across verticals.** The gate needs dated signals, and only the
healthcare skill has them — the other four carry `signals_found`, plain strings with no
dates. Re-scoring those would fail every lead at G2 and cap it at the nurture ceiling,
which is the inverse of the point. `plan_rescore` refuses a config that cannot gate.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Sequence

from aeo.gated_score import score as gated_score
from aeo.signal_class import classify

#: A prospect whose recomputed score would land here indicates a bug in the model, not a
#: lead. Mirrors `gated_score.FORBIDDEN_BAND`; asserted per row so a re-score can never
#: write one.
FORBIDDEN_LO, FORBIDDEN_HI = 46, 79


class RescoreRefused(Exception):
    """Raised when a config cannot support the gated model.

    Deliberately an exception rather than a skipped row: re-scoring 306 flooring
    prospects into the nurture lane would be a silent, confident, total regression, and
    the operator would see 306 changed numbers rather than an error.
    """


def _signals(row: dict[str, Any], source: str) -> list[dict[str, Any]]:
    validation = row.get("validation_data")
    raw = (validation or {}).get(source) if isinstance(validation, dict) else None
    out = []
    for s in raw or []:
        if isinstance(s, dict):
            # Re-derive rather than trust: rows written before 2026-08-31 carry only the
            # free-text `signal_type`, so a stored `signal_class` is absent on exactly
            # the historical rows this job exists for.
            out.append(dict(s, signal_class=s.get("signal_class") or classify(s.get("signal_type"))))
    return out


def _flatten_discovery(row: dict[str, Any]) -> dict[str, Any]:
    """Lift `discovery_data.by_source.*` fields onto the lead, as scoring does at run time.

    🔑 `employee_count` lives here, not in a column — the size band reads nothing without
    this, and every prospect would score the unknown midpoint. Measured: it silently
    costs or grants 2 points on every lead.
    """
    flat = dict(row)
    by_source = (row.get("discovery_data") or {}).get("by_source")
    if isinstance(by_source, dict):
        for src in by_source.values():
            if not isinstance(src, dict):
                continue
            for k, v in src.items():
                if v is not None and str(v).strip() and k not in flat:
                    flat[k] = v
    return flat


def can_gate(scoring_cfg: dict[str, Any], rows: Sequence[dict[str, Any]], source: str) -> bool:
    """Whether these rows carry what the gate needs: at least one dated signal.

    Checked against the DATA, not the config. A config can declare a perfect gate over a
    field its vertical never produces — which is exactly the state of the four non-health
    skills, and exactly the failure this guard exists to make loud.
    """
    if not (scoring_cfg.get("gate") or {}):
        return False
    for r in rows:
        for s in _signals(r, source):
            if str(s.get("signal_date") or "").strip():
                return True
    return False


def plan_rescore(
    rows: Sequence[dict[str, Any]],
    scoring_cfg: dict[str, Any],
    aliases: dict[str, str],
    today: date,
    *,
    signal_source: str = "switching_signal",
) -> list[dict[str, Any]]:
    """One update per prospect: `{id, old_score, score, lane, band, breakdown}`.

    **Pure.** No database, no network, no clock of its own — `today` is passed so a
    re-score is reproducible and so a run can be scored as of ITS OWN date rather than
    today's, which would age every signal by however long the row has been sitting there.
    """
    if str(scoring_cfg.get("model") or "").strip().lower() != "gated":
        raise RescoreRefused(
            "the skill is not configured for the gated model (`scoring.model: gated`), "
            "so re-scoring would apply a model this skill has not opted into"
        )
    if not can_gate(scoring_cfg, rows, signal_source):
        raise RescoreRefused(
            f"no prospect carries a dated signal under `validation_data.{signal_source}`, "
            "so every lead would fail the buying-window gate and be capped at a partial "
            "ceiling — the inverse of the point. This vertical needs the scanner to emit "
            "dated signals before it can be re-scored"
        )

    plans: list[dict[str, Any]] = []
    for row in rows:
        lead = _flatten_discovery(row)
        bd = gated_score(
            lead,
            _signals(row, signal_source),
            row.get("pipeline_status"),
            scoring_cfg,
            aliases,
            today,
        )
        total = bd["total"]
        if FORBIDDEN_LO <= total <= FORBIDDEN_HI:
            # Refuse the whole batch. A score in the structurally-empty band is proof of
            # a defect, and writing even one would destroy the invariant that makes the
            # model auditable by a single query.
            raise RescoreRefused(
                f"prospect {row.get('id')} recomputed to {total}, inside the "
                f"structurally-empty band {FORBIDDEN_LO}-{FORBIDDEN_HI}. That is a bug "
                "in the model, not a lead — refusing the entire batch"
            )
        plans.append(
            {
                "id": row.get("id"),
                "company_name": row.get("company_name"),
                "old_score": row.get("score"),
                "score": total,
                "lane": bd["lane"],
                "breakdown": bd,
            }
        )
    return plans


def summarise(plans: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """What changed, for the operator to read BEFORE anything is written."""
    if not plans:
        return {"count": 0}
    moved = [p for p in plans if p["old_score"] is not None and p["score"] != p["old_score"]]
    qualified = [p for p in plans if p["lane"] == "qualified"]
    deltas = [p["score"] - (p["old_score"] or 0) for p in moved]
    return {
        "count": len(plans),
        "moved": len(moved),
        "qualified": len(qualified),
        "score_range": (min(p["score"] for p in plans), max(p["score"] for p in plans)),
        "qualified_range": (
            (min(p["score"] for p in qualified), max(p["score"] for p in qualified))
            if qualified
            else None
        ),
        "biggest_gain": max(deltas) if deltas else 0,
        "biggest_drop": min(deltas) if deltas else 0,
        "in_forbidden_band": sum(
            1 for p in plans if FORBIDDEN_LO <= p["score"] <= FORBIDDEN_HI
        ),
    }
