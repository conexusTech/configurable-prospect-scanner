"""Prospects read from a gateway feed instead of found by a paid search.

`sources-read-gateway-records` (Phase CRM-3). A discovery source may say its rows come
from a feed the gateway already holds — health-inspection findings, first — by carrying

    "records": {"feed": "health_inspections", "window_days": 30}

beside the usual `name_field`, `fields` and `queries`. The `queries` are the feed's terms
(`["gasket"]`), so the gateway's config schema and the engine's own `validate_context`
pass unchanged; the GATEWAY reads terms and window from the stored skill, so this scanner
never sends them and a caller cannot widen the question. The feed skill's `name_field`
is `establishment_name`.

What replaces the search, in order:

1. **Fetch** the rows: one `GET .../source-records?skill=<slug>&source=<name>` per feed
   source (the runner owns the HTTP; this module takes it as `fetch`). An answer for
   another feed, with no item list, with items none of which is an object, or with more
   than `MAX_ROWS` items is refused.
2. **Keep only the rows placed in the organization's territory**, the same area (markets
   plus zip discovery's ZIPs) a search is held to. Under the default `metro` strictness a
   row is placed only POSITIVELY: its ZIP is one of the area's ZIPs or its city one of the
   area's cities (`geo_filter.placed_in_area`). A matching state alone places nothing —
   the classifier would admit such a row, because a searched candidate is verified
   afterwards, but a record's address is all there is. Under `geo_strictness: state` the
   classifier's own answer stands. No model is asked to verify anything.
3. **One establishment per name.** The engine groups by `normalize_name` and merges every
   field of a group, so two establishments whose names meet there ("McDonald's" and
   "MCDONALDS") would become one prospect with one's address and the other's finding.
   Per source and normalised name, the establishment (`permit_id`) with the newest
   `signal_date` is kept and the rest are dropped with a WARNING. Rows of the same permit
   still merge.
4. **Assemble** through the engine's own `_assemble_prospects` (injected), so a row
   becomes a prospect with the same deterministic id a search would mint, the
   commissioning org's own listing is excluded, and the row's non-empty values land in
   `discovery_data.by_source.<source>`.
5. Hand the prospects back as a sweep. The runner wraps it in `capped_discover`, which
   drops companies the org already holds BEFORE the prospect ceiling, applies the ceiling,
   and only then forwards the `prospects` event.

**Names are the gateway's business.** Every row's `establishment_name` is
"<name>, <street>" (the address's first line; else the city; else the plain name alone),
shared name or not, with the plain name in `establishment_name_plain`: "Subway, 123 Main
St", "Bella's Bistro, 100 E Cary St". So every branch of a chain is its own prospect and a
restaurant keeps one name in every answer. The company name here is the `name_field`
value exactly as given; step 3 above is only a safety net for two names that still meet.

A feed skill reads feeds only and validates nothing (v1): `feed_source_problems` is what
`config_mapping` refuses a skill mixing feed and searched sources, or authoring
`validation`, with — before any provider call.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from aeo.phases.geo_filter import (
    IN_AREA,
    STRICTNESS_STATE,
    TargetArea,
    classify_prospect,
    placed_in_area,
)

#: The key on a discovery source that makes it a feed source.
FEED_KEY = "records"

#: The most rows the gateway answers per source (the contract). More is refused.
MAX_ROWS = 500

#: Longest gateway-supplied value echoed into an error or a log line.
ECHO_LIMIT = 200

#: Fetch one source's rows: takes the source name, returns the gateway's parsed answer.
Fetch = Callable[[str], Any]

#: Assemble grouped rows into prospects: `(groups, tool_context) -> prospects`. The runner
#: binds the engine's `_assemble_prospects` with the run's id, canonical fields and
#: excluded names.
Assemble = Callable[[dict[str, list[dict[str, Any]]], dict[str, Any]], list[dict[str, Any]]]


class SourceRecordsError(RuntimeError):
    """A feed run that cannot go on. It fails the run, named.

    `reason` is a stable code the runner prefixes onto the run's error, the same contract
    the engine's `ScanAbort` keeps: consumers match the prefix, not the prose.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def clip(value: Any, limit: int = ECHO_LIMIT) -> str:
    """`str(value)`, cut to `limit` characters. For anything the gateway said."""
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_feed_source(source: Any) -> bool:
    """True when a source carries a `records` block, whatever its shape."""
    return isinstance(source, dict) and FEED_KEY in source


def feed_sources(sources: Any) -> dict[str, dict[str, Any]]:
    """The feed sources of a `sources` map, in authored order."""
    if not isinstance(sources, dict):
        return {}
    return {name: cfg for name, cfg in sources.items() if is_feed_source(cfg)}


def feed_name(source: dict[str, Any]) -> str:
    """The feed a feed source names, or "" when its `records` block names none."""
    block = source.get(FEED_KEY)
    feed = block.get("feed") if isinstance(block, dict) else None
    return feed.strip() if isinstance(feed, str) else ""


def feed_source_problems(sources: Any, *, validation: Any = None) -> list[str]:
    """Mapping-time faults of a skill with feed sources, worded for the operator.

    - feed and searched sources mixed (refused in v1);
    - a `records` block that is not an object naming a feed;
    - any authored `validation` (lanes included): it is a paid grounded call per
      prospect, and a feed skill pays for no search.

    Returns `[]` for a skill with no feed source at all, so a searched skill is untouched.
    """
    if not isinstance(sources, dict):
        return []
    feeds = [name for name, cfg in sources.items() if is_feed_source(cfg)]
    if not feeds:
        return []
    problems: list[str] = []
    searched = [name for name, cfg in sources.items() if isinstance(cfg, dict) and name not in feeds]
    if searched:
        problems.append(
            "`discovery.sources` mixes gateway-feed sources ("
            + ", ".join(f"`{n}`" for n in feeds)
            + ") with searched sources ("
            + ", ".join(f"`{n}`" for n in searched)
            + "). A skill reads its prospects from a feed or from a search, not both."
        )
    for name in feeds:
        if not feed_name(sources[name]):
            problems.append(
                f"`discovery.sources.{name}.records` must be an object naming the gateway "
                f"feed its rows come from, e.g. {{\"feed\": \"health_inspections\", "
                f"\"window_days\": 30}}."
            )
    if validation:
        problems.append(
            "`validation` is authored on a skill whose sources read a gateway feed ("
            + ", ".join(f"`{n}`" for n in feeds)
            + "). Validation, lanes included, is a paid grounded call per prospect, and a "
            "feed skill pays for no search: remove the section."
        )
    return problems


def in_territory(row: dict[str, Any], area: TargetArea, strictness: str) -> bool:
    """True when the row's own address places it in the area.

    Under `state` strictness, the classifier's answer. Under `metro` (and anything else,
    as the classifier reads it) only a positive placement: its ZIP or its city is the
    area's. See the module docstring for why a state alone is not enough here.
    """
    candidate = {
        "city": row.get("city"),
        "state": row.get("state"),
        "zip_code": row.get("zip_code"),
        "address": row.get("location_address"),
    }
    if strictness == STRICTNESS_STATE:
        return classify_prospect(candidate, area, strictness=strictness) == IN_AREA
    return placed_in_area(candidate, area)


def rows_of(answer: Any, *, source: str, feed: str) -> list[dict[str, Any]]:
    """The item rows of one gateway answer, after checking it is one this scanner reads."""
    if not isinstance(answer, dict) or not isinstance(answer.get("items"), list):
        raise SourceRecordsError(
            "source_records_malformed",
            f"the gateway's answer for source {source!r} carries no `items` list",
        )
    answered = answer.get("feed")
    if answered != feed:
        raise SourceRecordsError(
            "source_records_feed_mismatch",
            f"source {source!r} names feed {feed!r}, but the gateway answered for "
            f"feed {clip(repr(answered))}",
        )
    items = answer["items"]
    if len(items) > MAX_ROWS:
        raise SourceRecordsError(
            "source_records_malformed",
            f"the gateway answered {len(items)} rows for source {source!r}; the contract "
            f"allows at most {MAX_ROWS}",
        )
    rows = [row for row in items if isinstance(row, dict)]
    if items and not rows:
        raise SourceRecordsError(
            "source_records_malformed",
            f"none of the {len(items)} item(s) the gateway answered for source {source!r} "
            f"is an object",
        )
    return rows


def _establishment(row: dict[str, Any], index: int) -> str:
    """The establishment a row is about: its permit, or itself when it names none."""
    permit = str(row.get("permit_id") or "").strip()
    return f"permit:{permit}" if permit else f"row:{index}"


def finding_date(value: Any) -> date:
    """A row's `signal_date` as a date; anything that is not an ISO date string is oldest.

    The contract's `signal_date` is an ISO `YYYY-MM-DD` string (a timestamp reads as its
    date). Compared as dates, never as strings: as a string "unknown" sorts after every
    2026 date. A non-string is not the contract even when its digits would parse, as
    Python 3.11+ reads `20261101` as a basic-format date.
    """
    if not isinstance(value, str):
        return date.min
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return date.min


def _one_establishment_per_name(
    named: list[tuple[str, int, dict[str, Any]]],
    name_field: str,
) -> tuple[list[tuple[str, dict[str, Any]]], list[str], int]:
    """Per normalised name, the rows of the establishment with the newest finding.

    `named` is `(normalised name, position, row)` in answer order. Returns the kept
    `(normalised name, row)` pairs in answer order, the `name_field` values of the rows
    dropped, and how many of those may be the SAME establishment as the one kept, because
    one of the two carries no `permit_id`. A tie keeps the establishment that came first.
    """
    # Each establishment's newest finding, and where it first appears, per name.
    latest: dict[str, dict[str, tuple[date, int]]] = {}
    for norm, index, row in named:
        est = _establishment(row, index)
        when = finding_date(row.get("signal_date"))
        seen = latest.setdefault(norm, {}).get(est)
        latest[norm][est] = (max(when, seen[0]), seen[1]) if seen else (when, index)
    # Newest date wins; on a tie, the establishment that appears first.
    chosen = {
        norm: max(ests, key=lambda e: (ests[e][0], -ests[e][1]))
        for norm, ests in latest.items()
    }
    kept: list[tuple[str, dict[str, Any]]] = []
    dropped: list[str] = []
    unproven = 0
    for norm, index, row in named:
        est = _establishment(row, index)
        if est == chosen[norm]:
            kept.append((norm, row))
            continue
        dropped.append(str(row.get(name_field) or norm))
        if not (est.startswith("permit:") and chosen[norm].startswith("permit:")):
            unproven += 1
    return kept, dropped, unproven


def records_sweep(
    sources: dict[str, dict[str, Any]],
    *,
    fetch: Fetch,
    area: TargetArea,
    strictness: str,
    normalize: Callable[[str], str],
    assemble: Assemble,
    log: Callable[[str], None] = lambda _: None,
) -> Callable[[dict[str, Any], Callable[[dict[str, Any]], None]], list[dict[str, Any]]]:
    """A discovery sweep over `sources` (all feed sources) that reads instead of searches.

    Shaped as `prospect_budget.Sweep`, so `capped_discover` can hold its `prospects` event
    behind the org's held companies and the ceiling. `normalize` is the engine's
    `normalize_name`, the key a search groups by.
    """

    def _sweep(ctx: dict[str, Any], emit: Callable[[dict[str, Any]], None]) -> list[dict[str, Any]]:
        if area.is_empty:
            # Checked before the gateway is asked: with nothing to match against, every
            # row would be left out and the run would look like a quiet week.
            raise SourceRecordsError(
                "source_records_no_territory",
                "the organization's markets give no city, state or ZIP to hold the "
                "feed's rows to; set its markets before running a feed skill",
            )
        if strictness != STRICTNESS_STATE and not (area.cities or area.zips):
            # States alone ("VA"), and zip discovery left out or empty: under metro a row
            # is placed only by its city or ZIP, so nothing could ever be placed.
            raise SourceRecordsError(
                "source_records_no_territory",
                f"the organization's markets give only states ({area.describe()}) and no "
                "city or ZIP, and under the default metro strictness a row is placed only "
                "by its city or ZIP; write the markets as cities (\"Richmond, VA\") or ZIPs, "
                "run zip discovery, or set `geography.targeting.geo_strictness: state`",
            )

        groups: dict[str, list[dict[str, Any]]] = {}
        for name, cfg in sources.items():
            feed = feed_name(cfg)
            name_field = str(cfg.get("name_field") or "")
            emit({"type": "phase_start", "phase": name})

            answer = fetch(name)
            rows = rows_of(answer, source=name, feed=feed)
            not_records = len(answer["items"]) - len(rows)
            if rows and not any(str(row.get(name_field) or "").strip() for row in rows):
                # Every row lacks the field the source names, so the skill names the
                # wrong one. Refused rather than reported as a run that found nobody.
                carried = sorted({str(k) for row in rows for k in row})
                raise SourceRecordsError(
                    "source_records_unnamed",
                    f"none of the {len(rows)} row(s) feed {feed!r} answered for source "
                    f"{name!r} carries its name_field {name_field!r}; the rows carry "
                    f"{clip(', '.join(carried), 2 * ECHO_LIMIT)}",
                )
            if answer.get("truncated") is True:
                log(
                    f"WARNING source {name!r}: the gateway capped feed {feed!r} at "
                    f"{len(rows)} of {clip(answer.get('total'))} matching row(s); the rest "
                    f"were not read"
                )

            named: list[tuple[str, int, dict[str, Any]]] = []
            outside = nameless = 0
            for index, row in enumerate(rows):
                if not in_territory(row, area, strictness):
                    outside += 1
                    continue
                norm = normalize(str(row.get(name_field) or "").strip())
                if not norm:
                    nameless += 1
                    continue
                named.append((norm, index, row))

            kept, dropped, unproven = _one_establishment_per_name(named, name_field)
            if dropped:
                if unproven:
                    whose = (
                        f"a newer finding's under the same name ({unproven} with no "
                        f"permit_id to tell, so it may be the same establishment)"
                    )
                else:
                    whose = "a newer finding's from another establishment"
                log(
                    f"WARNING source {name!r}: dropped {len(dropped)} row(s) whose names "
                    f"collide with {whose}: {clip(', '.join(dropped))}"
                )
            for norm, row in kept:
                groups.setdefault(norm, []).append(
                    {"source": name, "name_field": name_field, "raw": row}
                )

            log(
                f"source {name!r}: feed {feed!r} answered {len(rows)} row(s)"
                + (f" and {not_records} item(s) that are not records, skipped"
                   if not_records else "")
                + f" for terms {clip(answer.get('terms'))} since {clip(answer.get('since'))}: "
                f"{len(kept)} row(s) placed in the territory, {outside} row(s) not placed "
                f"in it ({area.describe()}), {nameless} with no {name_field}, "
                f"{len(dropped)} dropped as name collisions; counted before held companies "
                f"and the prospect ceiling"
            )
            emit({"type": "phase_complete", "phase": name, "count": len(kept)})

        prospects = assemble(groups, ctx)
        emit({
            "type": "prospects",
            "phase": "discover",
            # The engine's own wire rule: the internal scoring bag never leaves.
            "items": [{k: v for k, v in p.items() if k != "_internal"} for p in prospects],
        })
        return prospects

    return _sweep
