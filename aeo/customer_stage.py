"""Customer pipeline-stage resolution — a PORT, not a redesign.

Faithful mirror of the gateway's ``src/common/utils/customer-pipeline-stage.util.ts``
(aeo-backend). It exists so the scanner can resolve the stage for a prospect the
judgment phase could not judge, **before scoring**, instead of scoring against
``calculate_pipeline``'s construction ladder while the gateway displays something else.

🔴 **The gateway's output is the reference and this file's only job is to reproduce it.**
If a label differs, THIS is wrong — not the gateway. Do not "improve" a rule here; port
it and report the difference.

Why it matters, in one measured row. On run ``741b7b3b`` groninger USA carried::

    pipeline_status  = "1 - Early Discovery"   <- displayed (gateway-derived)
    pipeline_timing  = 2                        <- scored (scanner's "7 - Too Late")

One prospect, two stages, and the score built on the one nobody sees. With this module
the scanner reaches the gateway's answer itself, so the score is computed from the stage
the prospect actually displays.

**Scope: the fallback only.** When ``ai_judgment`` returns a verdict the scanner's own
label already wins and nothing here runs — that is ~95% of prospects on a healthy run.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable, Optional, Sequence

# Field names read out of `discovery_data.by_source.*` as timing signals.
# ⚠️ EVENT dates, not completion dates — the distinction the gateway's file exists to
# undo. A skill may override via `config.pipeline.signalFields`.
DEFAULT_SIGNAL_FIELDS: tuple[str, ...] = ("trigger_date", "transaction_date")

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_US = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_NAMED_DAY = re.compile(r"^([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})$")
_NAMED_MONTH = re.compile(r"^([A-Za-z]+)\s+(\d{4})$")


class StageDef:
    """One rung. ``min_months``/``max_months`` present => a TIMING rung; neither => a
    NO-SIGNAL rung, used only when no timing rung matched."""

    __slots__ = ("key", "min_months", "max_months", "requires_contact")

    def __init__(
        self,
        key: str,
        min_months: Optional[float] = None,
        max_months: Optional[float] = None,
        requires_contact: bool = False,
    ) -> None:
        self.key = key
        self.min_months = min_months
        self.max_months = max_months
        self.requires_contact = requires_contact

    @property
    def is_timing(self) -> bool:
        return self.min_months is not None and self.max_months is not None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"StageDef({self.key!r}, {self.min_months}, {self.max_months}, "
            f"{self.requires_contact})"
        )


def _is_number(v: Any) -> bool:
    """Mirrors TS ``typeof x === 'number'``. **bool is excluded deliberately** — Python
    makes ``True`` an int, so a config carrying ``minMonths: true`` would otherwise
    become the band ``1`` here and be dropped by the gateway. The two must agree."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _band(entry: dict, snake: str, camel: str) -> Optional[float]:
    """One band value, accepting either wire spelling. See the shape note below."""
    for name in (snake, camel):
        v = entry.get(name)
        if _is_number(v):
            return v
    return None


def extract_customer_stage_defs(pipeline: Any) -> Optional[list[StageDef]]:
    """Read the rungs from the runtime context's ``pipeline`` block.

    🔴 **This reads the WIRE shape, not the raw skill config, and the difference is not
    cosmetic.** The gateway's ``pipelineVocabulary()`` (runtime-context.service.ts) does
    two things before the scanner ever sees a vocabulary:

    1. **It applies the default ladder.** ``extractCustomerStageDefs(config) ??
       timelineStagesAsDefs()`` — so a skill that declares no ``pipeline.stages`` still
       arrives with the shared TIMELINE ladder. **MYgroup declares none** (verified
       2026-08-31), which is exactly how the gateway derived groninger's
       ``1 - Early Discovery``. The scanner therefore never needs that fallback itself.
    2. **It emits snake_case** — ``min_months`` / ``max_months`` / ``requires_contact``,
       plus an explicit ``kind``.

    A first cut of this file parsed the gateway's *internal* camelCase config shape and
    consequently resolved ``None`` for every prospect. Both spellings are accepted here
    so a raw-config fixture in a test cannot silently resolve to nothing, but the wire
    shape is the one that runs in production.

    Bare strings are tolerated (a vocabulary with no timing rules can still resolve its
    no-signal rungs). Returns ``None`` when nothing usable is present — read that as
    "no vocabulary", never as "anything goes".
    """
    if isinstance(pipeline, dict) and "stages" in pipeline:
        raw = pipeline.get("stages")
    else:
        raw = pipeline
    if not isinstance(raw, list):
        return None

    defs: list[StageDef] = []
    for s in raw:
        if isinstance(s, str) and s:
            defs.append(StageDef(s))
            continue
        if not isinstance(s, dict):
            continue
        key = s.get("key")
        if not isinstance(key, str) or not key:
            continue
        lo = _band(s, "min_months", "minMonths")
        hi = _band(s, "max_months", "maxMonths")
        # `kind` is authoritative when the wire supplies it; the gateway derives it as
        # "both bands present", so the two agree by construction. Preferring it keeps a
        # rung the gateway called `no_signal` from being treated as timing here.
        if s.get("kind") == "no_signal":
            lo = hi = None
        defs.append(
            StageDef(
                key=key,
                min_months=lo,
                max_months=hi,
                # Strict `is True`, so a truthy string does not gate a rung.
                requires_contact=(
                    s.get("requires_contact") is True or s.get("requiresContact") is True
                ),
            )
        )
    return defs or None


def extract_signal_fields(pipeline: Any) -> tuple[str, ...]:
    """The signal field names a skill wants read, or the default pair.

    🔴 Takes the **pipeline block**, the same argument as `extract_customer_stage_defs`,
    and reads the wire spelling `signal_fields`. Both halves were wrong in the first cut:
    it expected the outer config and read camelCase, so called with what the scanner
    actually holds it silently returned the default pair — discarding a skill's declared
    fields exactly the way the gateway's own hardcoded copy once did. A nested
    ``{"pipeline": {...}}`` is still unwrapped so a raw-config caller is not surprised.
    """
    if isinstance(pipeline, dict) and isinstance(pipeline.get("pipeline"), dict):
        pipeline = pipeline["pipeline"]
    if not isinstance(pipeline, dict):
        return DEFAULT_SIGNAL_FIELDS
    raw = pipeline.get("signal_fields")
    if not isinstance(raw, list):
        raw = pipeline.get("signalFields")
    if not isinstance(raw, list):
        return DEFAULT_SIGNAL_FIELDS
    out = tuple(f for f in raw if isinstance(f, str) and f)
    return out or DEFAULT_SIGNAL_FIELDS


def collect_signal_dates(discovery_data: Any, signal_fields: Sequence[str]) -> list[str]:
    """Raw date strings out of ``discovery_data.by_source.<source>.<field>``."""
    dates: list[str] = []
    if not isinstance(discovery_data, dict):
        return dates
    by_source = discovery_data.get("by_source")
    if not isinstance(by_source, dict):
        return dates
    for src in by_source.values():
        if not isinstance(src, dict):
            continue
        for field in signal_fields:
            v = src.get(field)
            if isinstance(v, str) and v.strip():
                dates.append(v)
    return dates


def _ymd(y: int, m: int, d: int) -> Optional[date]:
    """Guards a rolled-over date (02/31/2026 -> March 3) from passing as valid, the way
    the gateway's ``utc()`` helper does."""
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_signal_date(raw: Any) -> Optional[date]:
    """Parse the spellings real runs contain (``2026-01-16``, ``02/21/2026``,
    ``April 27, 2023``, ``August 2026``). ``None`` for anything else.

    🔑 **A bare year is REFUSED.** ``"2026"`` carries no month, and the engine's habit of
    inventing one is what manufactured fabricated completion dates. Refusing costs a
    stage on a handful of rows and buys never asserting a month nobody supplied.
    """
    t = ("" if raw is None else str(raw)).strip()
    if not t:
        return None

    m = _ISO.match(t)
    if m:
        return _ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    m = _US.match(t)
    if m:  # month/day/year
        return _ymd(int(m.group(3)), int(m.group(1)), int(m.group(2)))

    m = _NAMED_DAY.match(t)
    if m:
        mo = _MONTH_NAMES.get(m.group(1).lower())
        return None if mo is None else _ymd(int(m.group(3)), mo, int(m.group(2)))

    # Month + year, day unknown. Mid-month rather than the 1st: the 1st biases every such
    # row half a month early, which can tip it across a band boundary.
    m = _NAMED_MONTH.match(t)
    if m:
        mo = _MONTH_NAMES.get(m.group(1).lower())
        return None if mo is None else _ymd(int(m.group(2)), mo, 15)

    return None


def months_from_today(d: date, today: date) -> int:
    """Whole months from ``today`` to ``d``. Negative = past. Day is ignored, matching
    the gateway's UTC year/month arithmetic."""
    return (d.year - today.year) * 12 + (d.month - today.month)


def resting_stage(defs: Sequence[StageDef]) -> Optional[str]:
    """Where a prospect rests when nothing else resolved.

    ⚠️ "Unbanded" and "ungated" are different and conflating them is a bug the gateway
    shipped once: a rung with bands but no contact gate got picked, filing every
    signal-less prospect under the most actionable stage on the board. The resting rung
    must carry **no timing claim at all**.

    ⚠️ **Not the last rung** — for the shared ladder that is ``7 - Too Late``, which would
    file every evidence-free prospect under the one verdict telling an operator to give up.
    """
    if not defs:
        return None
    for d in defs:
        if (d.min_months is None or d.max_months is None) and not d.requires_contact:
            return d.key
    return defs[0].key


def resolve_with_evidence(
    defs: Sequence[StageDef],
    signal_dates: Iterable[str],
    has_contact: bool,
    today: date,
) -> tuple[Optional[str], Optional[date]]:
    """``(stage_key, deciding_date)`` — the resolution AND what actually decided it.

    🔑 **The second element exists because "banded a real date" and "found no date and
    fell back" are otherwise indistinguishable downstream, and they are not the same
    claim.** aeo-frontend renders provenance beside every stage chip; its `derived` copy
    reads *"Placed by measuring how long ago the event was"*. For an org whose
    `discovery_data` carries no timing fields at all — MYgroup, measured — nothing was
    measured and there was no date, so that sentence asserts work that never happened.
    A missing input rendering as a confident value is the defect this whole redesign is
    about; it should not be reintroduced by the label describing the fix.

    ``None`` for the date means the stage came from a no-signal rung or from resting.
    """
    timing = [d for d in defs if d.is_timing]

    parsed = [p for p in (parse_signal_date(raw) for raw in signal_dates) if p]

    if parsed and timing:
        # Mirrors the TS reduce: strictly-less, so a tie keeps the EARLIER value.
        best = parsed[0]
        best_m = months_from_today(best, today)
        for p in parsed[1:]:
            m = months_from_today(p, today)
            if abs(m) < abs(best_m):
                best, best_m = p, m
        for d in timing:
            if best_m >= d.min_months and best_m < d.max_months:  # type: ignore[operator]
                return d.key, best

    # No timing rung applied. Fall back in declared order: a contact-gated rung only when
    # the prospect is reachable, then the first ungated one.
    no_signal = [d for d in defs if d.min_months is None or d.max_months is None]
    if has_contact:
        for d in no_signal:
            if d.requires_contact:
                return d.key, None
    for d in no_signal:
        if not d.requires_contact:
            return d.key, None
    return None, None


def resolve_customer_stage(
    defs: Sequence[StageDef],
    signal_dates: Iterable[str],
    has_contact: bool,
    today: date,
) -> Optional[str]:
    """Resolve a stage key, or ``None`` to abstain. AEO's signature, unchanged.

    Timing rungs win over no-signal rungs. Among the parsed dates the one **closest to
    now** decides: a decade-old ``transaction_date`` beside a fresh ``trigger_date`` must
    not drag the prospect backwards, and a date far outside every declared band falls
    through to the no-signal rungs rather than snapping to the nearest edge.
    """
    return resolve_with_evidence(defs, signal_dates, has_contact, today)[0]
