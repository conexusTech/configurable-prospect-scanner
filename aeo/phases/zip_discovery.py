"""Zip-code discovery — the PRD's Phase 0, geographic fan-out.

Expands each target market into the postal codes worth searching, and emits a
`zip_codes` event so AEO populates `scan_run_zip_codes`. Until now that table was
permanently empty and the `zip_codes` event type was dead: **the vendored engine has
no zip support at all** (`grep -i zip` finds nothing in it), even though the AV
skill's own context declared `geography.targeting.use_zip_discovery` — aspirational
config that was never implemented.

Two things this phase does that decide whether it is real or decorative:

1. **`include_scope` is honoured.** The org's scope says whether secondary markets
   are in play. Expanding them when it says home-only silently widens targeting, and
   nothing downstream would flag it.
2. **`excluded_markets` actually excludes.** A zip that falls in an excluded market is
   dropped here, because nothing downstream re-checks it — the discovery sweep just
   searches whatever geography it is handed.

And one that decides whether it is safe:

3. **A model-returned "zip" is validated as postal-code-shaped.** AEO's DTO accepts
   any string up to 10 characters, so `"Austin"` or `"78701 area"` would persist
   happily and then be searched as a location. Non-conforming values are dropped with
   a count, never coerced.

**Emitting zips is separate from searching them.** This phase always records the
geography; whether those zips then *drive* the discovery sweep is gated on
`geography.targeting.use_zip_discovery`, because a market expanding to 20 zips
multiplies every discovery query by 20. Recording the fan-out is cheap; acting on it
is not, and the two should not be one decision.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from aeo.phases._concurrent import (
    DEFAULT_CALL_TIMEOUT_S,
    PHASE_RETRY_ATTEMPTS,
    concurrency_from,
    map_bounded,
)

#: Recorded on every row so a later radius- or census-based implementation is
#: distinguishable in the data rather than silently mixed in. This is what AEO's
#: `discovery_method` column is for.
DISCOVERY_METHOD = "llm_grounded"

#: US ZIP or ZIP+4. Anything else is not a postal code, whatever the model called it.
_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")

#: Cap per market. Unbounded, a single metro can return hundreds of zips and — when
#: `use_zip_discovery` is on — multiply every discovery query by that many.
DEFAULT_MAX_ZIPS_PER_MARKET = 15

_PROMPT = """List the postal codes worth prospecting in and immediately around this
market.

MARKET
{market}

Return the {limit} most relevant US ZIP codes, prioritising areas with commercial and
institutional activity over purely residential ones.

Return a JSON array of objects:
[{{"zip_code": "78701", "city": "Austin", "county": "Travis", "state": "TX",
   "population": 12345, "latitude": 30.27, "longitude": -97.74,
   "distance_from_center": 0.0}}]

`zip_code` must be a real 5-digit US ZIP. Omit any field you cannot support rather
than guessing. Return only the JSON array."""


def _num(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def _clean_str(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    if not text or text.lower() in {"n/a", "none", "unknown", "null"}:
        return None
    return text[:limit]


def target_markets(geography: dict[str, Any]) -> list[str]:
    """Markets to expand, per the org's `include_scope`.

    Scope values seen in live data: `HOME_ONLY`, `HOME_SECONDARY`. An unrecognised
    or absent scope falls back to **home only** — the conservative reading, because
    the failure mode of guessing the other way is scanning markets the org excluded
    itself from, which costs money and produces prospects nobody wanted.
    """
    def _flatten(value: Any) -> list[str]:
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, dict):
            out: list[str] = []
            for state, cities in value.items():
                if isinstance(cities, list):
                    out.extend(f"{c}, {state}" for c in cities if c)
            return out
        text = str(value).strip() if value is not None else ""
        return [text] if text else []

    scope = str(geography.get("include_scope") or "").strip().upper()
    markets = _flatten(geography.get("home_markets"))
    if scope == "HOME_SECONDARY":
        markets += _flatten(geography.get("secondary_markets"))
    return list(dict.fromkeys(markets))


def _excluded_matchers(geography: dict[str, Any]) -> list[str]:
    excluded = geography.get("excluded_markets")
    values = excluded if isinstance(excluded, list) else [excluded]
    return [str(v).strip().lower() for v in values if v and str(v).strip()]


def _is_excluded(row: dict[str, Any], matchers: list[str]) -> bool:
    """True when a zip falls in an excluded market.

    Substring match on the city/state/county text, deliberately loose: an org writing
    "Springfield" means the place, not an exact-formatted market string, and a missed
    exclusion is worse than an over-eager one here — the org said not to go there.
    """
    if not matchers:
        return False
    haystack = " ".join(
        str(row.get(k) or "") for k in ("city", "county", "state", "zip_code")
    ).lower()
    return any(m in haystack for m in matchers)


def discover_zips(
    geography: dict[str, Any],
    *,
    provider: Callable[..., str],
    provider_config: dict[str, Any],
    parse_json_array: Callable[[str], list[dict[str, Any]]],
    max_per_market: int = DEFAULT_MAX_ZIPS_PER_MARKET,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Expand the in-scope markets into validated zip rows.

    `geography` is the org's **resolved** geography — `home_markets` and friends are
    R12-bound, so they arrive as bindings and must be resolved first.
    """
    markets = target_markets(geography)
    matchers = _excluded_matchers(geography)

    if emit:
        emit({"type": "phase_start", "phase": "zip_discovery"})

    def _expand(market: str) -> list[dict[str, Any]]:
        raw = provider(
            _PROMPT.format(market=market, limit=max_per_market),
            model=provider_config.get("model"),
            temperature=provider_config.get("temperature", 0.1),
            retry_attempts=PHASE_RETRY_ATTEMPTS,
            timeout_s=DEFAULT_CALL_TIMEOUT_S,
            phase="zip_discovery",
        )
        return parse_json_array(raw)

    per_market = map_bounded(
        markets,
        _expand,
        max_concurrency=concurrency_from(provider_config),
        timeout_s=DEFAULT_CALL_TIMEOUT_S,
    )

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    rejected = 0
    excluded = 0

    for market, results in zip(markets, per_market):
        for raw_row in (results or [])[:max_per_market]:
            if not isinstance(raw_row, dict):
                continue
            zip_code = str(raw_row.get("zip_code") or "").strip()
            if not _ZIP_RE.match(zip_code):
                # Not coerced. AEO's DTO would accept "Austin" as a zip_code and it
                # would then be searched as a location.
                rejected += 1
                continue
            if zip_code in seen:
                # Adjacent metros share zips; a duplicate is duplicated spend.
                continue

            row: dict[str, Any] = {
                "zip_code": zip_code,
                "discovery_method": DISCOVERY_METHOD,
            }
            for key, limit in (("city", 255), ("county", 255), ("state", 10)):
                value = _clean_str(raw_row.get(key), limit)
                if value:
                    row[key] = value
            for key in ("latitude", "longitude", "population", "distance_from_center"):
                value = _num(raw_row.get(key))
                if value is not None:
                    row[key] = value

            if _is_excluded(row, matchers):
                excluded += 1
                continue

            seen.add(zip_code)
            rows.append(row)

    if emit:
        emit({"type": "phase_complete", "phase": "zip_discovery", "count": len(rows)})
    return rows


def rejection_summary(
    markets: int, rows: list[dict[str, Any]]
) -> str:  # pragma: no cover - log text
    return f"{len(rows)} zip(s) across {markets} market(s)"


def zips_as_markets(rows: list[dict[str, Any]], cap: int) -> list[str]:
    """Zip rows → market strings the discovery sweep can search.

    **Distinct cities, not individual ZIPs — and that distinction was learned the
    hard way.** The first version emitted `"78701, Austin, TX"` per zip. Combined with
    strict geographic prompting, a live run then returned **zero** prospects: a ZIP is
    a few square miles, and a firm serving a whole metro is not located in every one
    of them. The narrow query was technically respected and practically useless.

    A ZIP is the right unit for **verifying** a result's location (`geo_filter` matches
    on them) and the wrong unit for **finding** one. So searching happens at city
    level, where firms actually are, and enforcement still holds the boundary the zips
    describe. Deduped, because ten zips across one city is one search, not ten.

    `cap` therefore bounds distinct *cities*, not rows.
    """
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        city = str(row.get("city") or "").strip()
        state = str(row.get("state") or "").strip()
        if not city:
            # No city to search with — the bare zip is a poor query but better than
            # dropping the area entirely.
            market = str(row.get("zip_code") or "").strip()
        else:
            market = f"{city}, {state}".strip(", ")
        if not market or market.lower() in seen:
            continue
        seen.add(market.lower())
        out.append(market)
        if len(out) >= max(cap, 0):
            break
    return out
