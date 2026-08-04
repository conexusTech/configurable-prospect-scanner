"""Geographic enforcement — verify the results are where the org asked.

**Why this exists.** Observed on live runs: zip discovery expanded Austin and Round
Rock, the discovery queries carried `"…near 78701, Austin, TX"`, and Gemini returned
**Dallas** firms. The geography reached the prompt correctly; the model simply did not
respect it. Nothing downstream noticed, and a scan that quietly ignores geography
looks exactly like one that worked.

The engine offers no structural place to constrain location — the market only reaches
the prompt as words inside the query string (`build_prompt`), so it is a hint the
model may weigh against whatever else its search surfaces. Big-city firms outrank
small-town ones in search results, which is precisely the drift observed.

So this module **verifies rather than asks**. Prompt wording is a soft measure worth
having (a source may supply its own `prompt` template with firmer language), but the
guarantee has to be deterministic.

## Expressed as a validation verdict, not a filter

The engine emits its `prospects` event *during* discovery, before this can run — so
out-of-area prospects are already persisted by the time we know they are wrong.
Dropping them locally would leave rows in AEO with no explanation.

Instead a geographic mismatch becomes a **`validations` verdict**: the prospect row
survives for audit, carries a stated reason, and is excluded from scoring exactly like
any other failed validation. That reuses a durable channel that already exists rather
than inventing a second notion of "rejected".

## Unknown is not out-of-area

A prospect with no city and no state cannot be judged, and is **kept** — the same rule
validation uses. Treating unverifiable as rejected would silently shrink results
whenever discovery returned sparse rows.
"""

from __future__ import annotations

import re
from typing import Any

#: Verdict values `classify_prospect` returns.
IN_AREA = "in_area"
OUT_OF_AREA = "out_of_area"
UNKNOWN = "unknown"

_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

#: US state abbreviation, as it appears in a market string like "Austin, TX".
_STATE_RE = re.compile(r"\b([A-Z]{2})\b")


class TargetArea:
    """The geography a scan was asked to cover, in matchable form."""

    def __init__(self, states: set[str], cities: set[str], zips: set[str]) -> None:
        self.states = states
        self.cities = cities
        self.zips = zips

    @property
    def is_empty(self) -> bool:
        """True when nothing is known about the target — enforcement must not run.

        An empty area would classify every prospect as out-of-area and reject the
        whole scan, which is a far worse failure than not enforcing at all.
        """
        return not (self.states or self.cities or self.zips)

    def describe(self) -> str:
        parts = []
        if self.cities:
            parts.append(f"{len(self.cities)} city/cities")
        if self.states:
            parts.append(f"states {', '.join(sorted(self.states))}")
        if self.zips:
            parts.append(f"{len(self.zips)} zip(s)")
        return "; ".join(parts) or "(nothing)"


def _norm_city(value: Any) -> str:
    return re.sub(r"[^a-z ]", "", str(value or "").strip().lower()).strip()


def build_target_area(
    zip_rows: list[dict[str, Any]] | None, markets: list[str] | None
) -> TargetArea:
    """Assemble the allowed geography from zip discovery and the raw markets.

    Both sources are used: zip rows are authoritative when Phase 0 ran, and the raw
    market strings cover the case where it did not (or returned nothing).
    """
    states: set[str] = set()
    cities: set[str] = set()
    zips: set[str] = set()

    for row in zip_rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("zip_code"):
            zips.add(str(row["zip_code"]).split("-")[0])
        if row.get("state"):
            states.add(str(row["state"]).strip().upper()[:2])
        if row.get("city"):
            cities.add(_norm_city(row["city"]))

    for market in markets or []:
        text = str(market)
        for match in _ZIP_RE.finditer(text):
            zips.add(match.group(1))
        # "Austin, TX" / "78701, Austin, TX" — the trailing 2-letter token is the
        # state, everything before it that is not a zip is city text.
        state_match = _STATE_RE.findall(text.upper())
        if state_match:
            states.add(state_match[-1])
        for part in text.split(","):
            candidate = _norm_city(part)
            # Skip the state token and bare zips.
            if candidate and len(candidate) > 2 and not candidate.isdigit():
                cities.add(candidate)

    return TargetArea(states=states, cities=cities, zips=zips)


#: How tightly to enforce. **`metro` is the default and it is the one that fixes the
#: observed drift.**
#:
#: The live failure was Austin → *Dallas*, both Texas. A state-level boundary lets
#: that through untouched, so "enforce geography" would have been a label on nothing.
#: `metro` requires a zip or city match, which is what "respect the zips" means.
#:
#: The cost is real: a legitimate neighbouring town absent from the zip list gets
#: rejected. That cost is bounded by Phase 0 doing its job — it returns up to 15 zips
#: per market, so the city set covers a metro's suburbs rather than just its centre.
#: `state` remains available for verticals where a distant firm serving the market is
#: a genuine prospect.
STRICTNESS_METRO = "metro"
STRICTNESS_STATE = "state"


def classify_prospect(
    prospect: dict[str, Any], area: TargetArea, *, strictness: str = STRICTNESS_METRO
) -> str:
    """Judge one prospect against the target area.

    Match rules, in order:
    - a zip inside the target set → in area (strongest signal)
    - a city name in the target set → in area
    - an unrecognised city:
        * `metro` (default) → **out of area**, even in a target state. This is the
          rule that catches Austin → Dallas.
        * `state` → in area when the state matches; the looser reading.
    - a state outside the target set → out of area under either strictness
    - nothing to go on → unknown (never rejected)
    """
    if area.is_empty:
        return UNKNOWN

    city = _norm_city(prospect.get("city"))
    state = str(prospect.get("state") or "").strip().upper()[:2]
    zip_code = ""
    for field in ("zip_code", "address"):
        match = _ZIP_RE.search(str(prospect.get(field) or ""))
        if match:
            zip_code = match.group(1)
            break

    if zip_code and zip_code in area.zips:
        return IN_AREA
    if city and city in area.cities:
        return IN_AREA

    # A state outside the target set is out of area regardless of strictness.
    if state and area.states and state not in area.states:
        return OUT_OF_AREA

    # Recognised state (or no state given), but the city did not match.
    if city and area.cities:
        # THE RULE THAT CATCHES THE OBSERVED DRIFT. Under `metro` an unlisted city
        # is a miss even inside a target state, because Austin → Dallas is exactly
        # that shape and is exactly what was asked to be fixed.
        return IN_AREA if strictness == STRICTNESS_STATE else OUT_OF_AREA

    if state and area.states:
        # No city to judge, but the state matches — nothing more to go on.
        return IN_AREA
    return UNKNOWN


def geographic_verdicts(
    prospects: list[dict[str, Any]],
    area: TargetArea,
    *,
    strictness: str = STRICTNESS_METRO,
) -> list[dict[str, Any]]:
    """Validation-shaped entries for every prospect that is out of area.

    Only mismatches are returned: an in-area prospect needs no verdict, and emitting
    one would overwrite a real signal verdict with a geographic pass.
    """
    if area.is_empty:
        return []

    out: list[dict[str, Any]] = []
    for prospect in prospects:
        prospect_id = prospect.get("id")
        if not prospect_id:
            continue
        if classify_prospect(prospect, area, strictness=strictness) != OUT_OF_AREA:
            continue
        where = ", ".join(
            str(prospect.get(k)) for k in ("city", "state") if prospect.get(k)
        ) or "an unstated location"
        out.append(
            {
                "prospect_id": prospect_id,
                "validation_data": {
                    "validated": False,
                    "signals_found": [],
                    "disqualifiers_hit": ["outside the target geography"],
                    "reasoning": (
                        f"Discovered in {where}, which is outside the scan's target "
                        f"area ({area.describe()}). The search asked for that "
                        f"geography and the result did not match it."
                    ),
                },
            }
        )
    return out
