"""The gated floor-plus-bonus model. Opt-in via ``scoring.model: gated``.

**What it replaces.** A flat additive sum over seven axes with **no term meaning "this
lead is qualified"**. On MYGroup's run 741b7b3b all 17 leads satisfying both dominant
qualifiers scored **below 80** — best 73, mean 50.4. Being qualified was worth nothing.

    G1 = in the org's target market
    G2 = in an active buying window AND carrying a signal < 18 months old

    if G1 and G2:  score = 80 + bonus          # bonus in [0,20]  ->  80..100
    else:          score = lane.base + bonus   # clamped by the lane ceiling -> 0..45

Qualified scores occupy **[80,100]** and the highest gated-out score is **45**, so the
range **46-79 is structurally empty** — any score landing there is a bug, catchable by one
assertion rather than by reasoning about weights.

🔴 **Nothing here subtracts.** Every band is non-negative and `ai_adjustment` does not
enter the total. A lead that clears both gates cannot be dragged below the floor by an
axis that measures something else.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Optional, Sequence

#: Structurally empty under a correct implementation. Exposed so callers can assert it.
FORBIDDEN_BAND: tuple[int, int] = (46, 79)


# ─────────────────────────── dates ───────────────────────────

def _parse_partial(raw: Any) -> Optional[tuple[int, int, int]]:
    """``(y, m, d)`` from a date that may be year-, month- or day-precise.

    🔑 **A partial date resolves to the EARLIEST instant in its precision** — `2026` is
    `2026-01-01`, `2026-08` is `2026-08-01`. That ages the lead, so the only thing an
    imprecise date can do is CLOSE the gate, never open one. The opposite convention
    would let a bare year admit a lead on evidence nobody supplied.

    ⚠️ Deliberately NOT `customer_stage.parse_signal_date`, which REFUSES a bare year.
    That refusal is right for placing a stage (a wrong rung is a visible lie) and wrong
    here (a missing recency point is a small, conservative loss). Two parsers, two jobs.
    """
    t = str(raw or "").strip()
    if not t:
        return None
    parts = t.replace("/", "-").split("-")
    try:
        nums = [int(p) for p in parts[:3]]
    except ValueError:
        return None
    if not nums or not (1900 <= nums[0] <= 2999):
        return None
    y = nums[0]
    m = nums[1] if len(nums) > 1 else 1
    d = nums[2] if len(nums) > 2 else 1
    if not (1 <= m <= 12) or not (1 <= d <= 31):
        return None
    return y, m, d


def age_months(raw: Any, today: date) -> Optional[int]:
    """Whole months from a signal date to ``today``, floored at 0.

    ``(ty-y)*12 + (tm-m) - (1 if td < d else 0)`` — whole elapsed months, so a signal
    dated the 20th is not "one month old" on the 5th of the next month.

    **A future date clamps to 0**, not to a negative age: a signal dated next quarter is
    as fresh as evidence gets, and a negative age would sort it outside every band.
    """
    parsed = _parse_partial(raw)
    if parsed is None:
        return None
    y, m, d = parsed
    months = (today.year - y) * 12 + (today.month - m) - (1 if today.day < d else 0)
    return max(0, months)


# ─────────────────────────── the gate ───────────────────────────

def _norm(s: Any) -> str:
    return " ".join(str(s or "").strip().lower().split())


def in_target_market(
    lead: dict[str, Any], gate_cfg: dict[str, Any], aliases: dict[str, str]
) -> bool:
    """G1 — the lead's state is one the org serves, and no disqualifier matches.

    🔴 **Normalised in BOTH directions, and this is D1's actual fix.** The org writes
    `"North Carolina"`; every lead carries `state = 'NC'`. The dead `region_bonus` axis
    awarded 0 of 5 points to 24 of 24 leads that all satisfied this, because nothing
    reconciled the two spellings.
    """
    allowed = {_norm(s) for s in (gate_cfg.get("allowed_states") or []) if _norm(s)}
    if not allowed:
        # 🔴 An unresolvable allow-list is a DEAD GATE, and a dead gate fails every lead
        # CLOSED — strictly worse than the dead bonus it replaces. The caller lints for
        # this; here it can only refuse to admit.
        return False
    expanded = set(allowed)
    for a in allowed:
        if a in aliases:
            expanded.add(_norm(aliases[a]))
        for k, v in aliases.items():
            if _norm(v) == a:
                expanded.add(_norm(k))

    state = _norm(lead.get(gate_cfg.get("state_field") or "state"))
    if not state or state not in expanded:
        return False

    haystack = " ".join(
        _norm(lead.get(k)) for k in ("company_name", "industry", "description")
    )
    for rule in gate_cfg.get("exclude_rules") or []:
        r = _norm(rule)
        if r and r in haystack:
            return False
    return True


def fresh_signals(
    signals: Sequence[dict[str, Any]], months: int, today: date
) -> list[dict[str, Any]]:
    """Signals strictly younger than ``months``.

    🔑 **Strictly ``<``, while the recency band awards on ``<=``.** Deliberate asymmetry:
    strict about ADMITTING a lead, generous about CREDITING one already admitted. Smart
    Wires sits exactly on the boundary at 18 whole months and is correctly gated out.
    """
    out = []
    for s in signals:
        age = age_months(s.get("signal_date"), today)
        if age is not None and age < months:
            out.append(s)
    return out


def in_buying_window(
    stage: Any,
    signals: Sequence[dict[str, Any]],
    gate_cfg: dict[str, Any],
    today: date,
) -> bool:
    """G2 — an active stage AND **any** signal inside the freshness window.

    🔴 **``any_fresh``, never "the strongest is fresh".** Letting the selection rule decide
    admission was the mirror-image bug: a 31-month-old RFP beside a one-week-old broker
    change would fail the gate outright, despite carrying exactly the evidence the gate
    exists to detect. Filter first, select second.
    """
    window = {_norm(s) for s in (gate_cfg.get("window_stages") or [])}
    if not window or _norm(stage) not in window:
        return False
    months = int(gate_cfg.get("signal_freshness_months") or 18)
    return bool(fresh_signals(signals, months, today))


# ─────────────────────────── selection ───────────────────────────

def select_signal(
    signals: Sequence[dict[str, Any]],
    classes: dict[str, int],
    months: int,
    today: date,
) -> Optional[dict[str, Any]]:
    """The signal the prose and the bands describe: **strongest among the fresh**.

    Ties break fresher. When nothing is fresh the strongest OVERALL is returned so a
    gated-out lead's analysis still describes its best evidence and says it is stale —
    scoring stale evidence as 0 would make every no-fresh lead indistinguishable, which
    is D1/D2 again.
    """
    if not signals:
        return None
    pool = fresh_signals(signals, months, today) or list(signals)

    def rank(s: dict[str, Any]) -> tuple[int, int]:
        strength = classes.get(str(s.get("signal_class") or ""), -1)
        age = age_months(s.get("signal_date"), today)
        return (strength, -(age if age is not None else 10**6))

    return max(pool, key=rank)


# ─────────────────────────── bonus bands ───────────────────────────

def band_signal_strength(sig: Optional[dict[str, Any]], cfg: dict[str, Any]) -> int:
    """0-8 from the canonical class, falling back to the signal's own type.

    🔴 **An unusable class scores the MIDPOINT and is logged — never 0.** "We could not
    classify this" is not evidence of weakness, and scoring it 0 bends a data rule to
    protect an invariant. It is why exactly-80 is unreachable and why that requirement
    was dropped: the practical minimum is 81, or 83 with size unknown.

    🔑 **The `signal_type` fallback is what makes this band work outside one vertical.**
    `signal_class` comes from `signal_class.SIGNAL_CLASSES`, a closed seven-value enum
    written for the EAP/benefits vocabulary: `benefits_change`, `broker_carrier_change`,
    `workforce_change`. Measured on real books:

      - consulting (property development): **37 of 46** signals classify to `None` —
        `groundbreaking_announcement`, `permit_and_zoning_filings`, `zoning_approval`
        are simply not in the enum, and no amount of config can name a class the
        classifier never emits;
      - MYgroup, whose vertical the enum WAS written for: the classifier is fine (13 of
        204 unclassified) but its config keyed the band on `contract_renewal`,
        `provider_change`, `broker_change` — six of seven keys absent from the enum, so
        **196 of 204 signals fell to the midpoint** on a band that read as fully
        configured.

    Matching the type as well lets a skill declare the vocabulary its own vertical
    actually produces, without editing a shared enum on behalf of one customer. The class
    is still tried first, so every existing config keeps its exact behaviour; this can
    only turn a midpoint into a weight the config explicitly asked for.
    """
    classes: dict[str, int] = cfg.get("classes") or {}
    top = int(cfg.get("max", 8))
    if not sig:
        return 0  # no signal at all is a real absence, not an unusable class
    cls = str(sig.get("signal_class") or "")
    # `cls` is "" for every row written before `signal_class` was attached at enrichment
    # (2026-08-31), so an empty key in a config would silently match all of them and pay
    # its weight to signals that were never classified at all. Require a real class.
    if cls and cls in classes:
        return max(0, min(top, int(classes[cls])))
    # Normalised on both sides, for the same reason `signal_class.normalize` exists: the
    # model writes `groundbreaking announcement` and a config declares
    # `groundbreaking_announcement`, and an underscore should not decide a score.
    def _key(v: Any) -> str:
        return _norm(v).replace(" ", "_")

    raw = _key(sig.get("signal_type"))
    # 🔑 Bail BEFORE the scan, not inside it. An empty type has nothing to match, and a
    # config key that normalised to empty (`""`, `"---"`) would otherwise match it and
    # hand a weight to a signal carrying no type at all.
    if not raw:
        return top // 2
    for key, weight in classes.items():
        if _key(key) == raw:
            return max(0, min(top, int(weight)))
    return top // 2


def band_company_size(lead: dict[str, Any], cfg: dict[str, Any]) -> int:
    """0-4 by headcount. **Unknown maps to the midpoint, not to 0.**

    Revision 1 treated "we don't know" as identical to "under 50 employees" — data
    hygiene leaking into the score, on 7 of the 17 qualified leads.
    """
    top = int(cfg.get("max", 4))
    raw = lead.get(cfg.get("field") or "employee_count")
    try:
        n = int(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return int(cfg.get("unknown", top // 2))
    for threshold, pts in cfg.get("tiers") or []:
        if n >= int(threshold):
            return max(0, min(top, int(pts)))
    return 0


def band_contact(lead: dict[str, Any], cfg: dict[str, Any]) -> int:
    """0-4, additive over the reachability fields the config names."""
    top = int(cfg.get("max", 4))
    pts = 0
    for field, value in cfg.get("rules") or []:
        if str(lead.get(field) or "").strip():
            pts += int(value)
    return max(0, min(top, pts))


def band_recency(sig: Optional[dict[str, Any]], cfg: dict[str, Any], today: date) -> int:
    """0-4 from the SELECTED signal's age. Awards on ``<=`` — see `fresh_signals`."""
    top = int(cfg.get("max", 4))
    if not sig:
        return 0
    age = age_months(sig.get("signal_date"), today)
    if age is None:
        return 0
    for months, pts in cfg.get("months") or []:
        if age <= int(months):
            return max(0, min(top, int(pts)))
    return 0


# ─────────────────────────── the model ───────────────────────────

def score(
    lead: dict[str, Any],
    signals: Sequence[dict[str, Any]],
    stage: Any,
    scoring_cfg: dict[str, Any],
    aliases: dict[str, str],
    today: date,
) -> dict[str, Any]:
    """The finished breakdown: gates, bands, lane, and the total.

    Returns the shape the renderer and `score_factors` both read. **No key is optional** —
    a consumer that has to test for a missing gate verdict cannot tell "false" from
    "not computed", which is the confusion this whole redesign exists to remove.
    """
    gate_cfg = scoring_cfg.get("gate") or {}
    bonus_cfg = scoring_cfg.get("bonus") or {}

    # 🔑 **The four bands are NAMED PROPERTIES, not an array of `{name: ...}` items.**
    #
    # The array shape was replaced 2026-08-31. It joined config to engine on a
    # case-sensitive display string with a space in it ("Company size"), which nothing
    # validated. Measured cost of ONE capital letter, everything else held: the lookup
    # missed, the band function received `{}`, and it fell through its empty tier list
    # to 0 — a 500-employee company scoring the same as a 5-person one. Total 98 -> 94,
    # all four mis-cased 98 -> 84, and every one of those SILENT: legal total, unchanged
    # `qualified` lane, no invariant, no lint rule, nothing logged.
    #
    # Named properties make that a schema error at authoring time instead of fourteen
    # quiet points at scoring time. Reported by AGENT for a different reason — the
    # array left one description carrying all four bands' semantics, 3.6x over the
    # renderer budget, and the sentence it truncated away was the midpoint rule.
    months = int(gate_cfg.get("signal_freshness_months") or 18)

    g1 = in_target_market(lead, gate_cfg.get("target_market") or {}, aliases)
    g2 = in_buying_window(stage, signals, gate_cfg.get("buying_window") or {}, today)

    strength_cfg = bonus_cfg.get("signal_strength") or {}
    sig = select_signal(signals, strength_cfg.get("classes") or {}, months, today)

    parts = {
        "signal_strength": band_signal_strength(sig, strength_cfg),
        "company_size": band_company_size(lead, bonus_cfg.get("company_size") or {}),
        "confirmed_contact": band_contact(lead, bonus_cfg.get("confirmed_contact") or {}),
        "signal_recency": band_recency(sig, bonus_cfg.get("signal_recency") or {}, today),
    }
    bonus = min(sum(parts.values()), int(bonus_cfg.get("max", 20)))

    partial = scoring_cfg.get("partial") or {}
    if g1 and g2:
        lane, base, ceiling = "qualified", int(scoring_cfg.get("floor", 80)), int(
            scoring_cfg.get("score_cap", 100)
        )
    else:
        key = (
            "target_market_only" if g1 else "signal_only" if g2 else "neither"
        )
        lane_cfg = partial.get(key) or {}
        lane, base, ceiling = key, int(lane_cfg.get("base", 0)), int(
            lane_cfg.get("ceiling", 15)
        )

    total = min(base + bonus, ceiling)

    return {
        "total": total,
        "lane": lane,
        "gates": {"target_market": g1, "buying_window": g2},
        "bonus": bonus,
        "bands": parts,
        "selected_signal": sig,
        # 🔑 Reported, not inferred. A caller cannot otherwise distinguish "this lead had
        # no fresh signal" from "the selector had nothing to choose between", and the
        # nurture-lane inversion (a weak fresh signal scoring below a stale strong one)
        # is an ACCEPTED consequence that only reads as acceptable when it is visible.
        "selected_from_fresh": bool(sig and sig in fresh_signals(signals, months, today)),
    }
