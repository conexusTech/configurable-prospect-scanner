"""A cumulative ceiling on how many prospects one run may produce.

**Why this is not a `[:n]` at the end.** Three things in this repo already look like
a per-run cap and not one of them is:

- `discovery.target_prospects` is a **floor**. `discover_in_area` reads it only to
  decide whether to run ANOTHER round, and it verifies every candidate of the round
  it already has *before* that check (`geo_loop.py`). Round one returns whatever it
  returns, however large.
- `SCANNER_TOP_N`'s documented "output ranking cut" is real code — in
  `FileSink.close()`. The AEO path uses `AeoEventSink`, which has no `close()`.
  Dead on our path.
- `contacts.max_prospects` bounds contact enrichment only, which is downstream of
  both expensive per-prospect phases, so it saves almost no wall-clock.

**Measured cost of having none.** A production run (`0a85e4bd`, 2026-08-13)
discovered 262 prospects in 11 minutes, then spent 49 minutes on per-prospect
location verification at concurrency 2 and was killed by its one-hour job deadline
having validated, scored and enriched nothing. 262 rows persisted; every counter on
the run stayed NULL.

## Where the ceiling has to sit, and why it cannot sit anywhere else

The engine emits its single `prospects` event **inside** `discover`, before the
caller ever sees the list. And AEO writes prospects `ON CONFLICT ("id") DO NOTHING`,
so a row that is posted is a row forever — the first write wins permanently. A
truncation applied to `discover`'s return value would therefore bound the *work*
while leaving all 262 *rows*, which is the worst of both. So the runner intercepts
the event and applies the ceiling before forwarding it.

## The slice is a volume control, not a shortlist

Ranking needs scores and scoring runs after discovery, so nothing here knows which
prospects are best. `config_mapping` already refuses a config with no `scoring` for
precisely this reason — *"an arbitrary slice of results would be indistinguishable
from a shortlist"*. Taking the first N in thread-pool completion order would earn
that criticism, so the allocation does three things instead:

1. **Multi-source prospects first.** `source_count > 1` means two independent
   searches named the same firm. It is the only quality signal that exists before
   scoring, and it is one the config itself scores (`scoring.multi_source`), so
   preferring it follows the skill's stated values rather than inventing a rank.
2. **Then round-robin across discovery sources**, so a ceiling never starves a
   source. The same rule `max_search_zips` applies to markets: a cap that consumed
   sources in order would silently reduce a four-source recipe to one.
3. **Ties broken on prospect id**, which is `uuid5(scan_run_id:name)` — stable
   across rounds and across reruns of the same scan, so the slice is reproducible.
"""

from __future__ import annotations

from typing import Any, Callable


def _source_count(prospect: dict[str, Any]) -> int:
    """How many discovery sources named this firm. At least 1, never raises."""
    discovery = prospect.get("discovery_data")
    if not isinstance(discovery, dict):
        return 1
    try:
        return max(1, int(discovery.get("source_count") or 1))
    except (TypeError, ValueError):
        return 1


def _primary_source(prospect: dict[str, Any]) -> str:
    """The bucket this prospect round-robins under.

    `sources_found_in` is sorted by the engine, so a multi-source prospect lands in
    a stable bucket rather than in whichever source happened to report first.
    """
    discovery = prospect.get("discovery_data")
    found_in = discovery.get("sources_found_in") if isinstance(discovery, dict) else None
    if isinstance(found_in, list) and found_in:
        return str(found_in[0])
    return ""


def allocate(prospects: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """The `limit` prospects to keep. See the module docstring for the ordering."""
    if limit <= 0:
        return []
    if limit >= len(prospects):
        return list(prospects)

    # Bucket in preference order, so each round-robin pass takes the best remaining
    # prospect from each source rather than an arbitrary one.
    buckets: dict[str, list[dict[str, Any]]] = {}
    for prospect in sorted(
        prospects, key=lambda p: (-_source_count(p), str(p.get("id") or ""))
    ):
        buckets.setdefault(_primary_source(prospect), []).append(prospect)

    kept: list[dict[str, Any]] = []
    queues = list(buckets.values())
    while len(kept) < limit and any(queues):
        for queue in queues:
            if not queue:
                continue
            kept.append(queue.pop(0))
            if len(kept) >= limit:
                break
    return kept


class ProspectBudget:
    """The run's remaining prospect allowance, **cumulative across rounds**.

    Per-round would not bound anything: `max_discovery_rounds` defaults to 2 and a
    per-round cap of 100 permits 200.
    """

    def __init__(self, limit: int | None) -> None:
        self._remaining: int | None = (
            limit if isinstance(limit, int) and limit > 0 else None
        )

    @property
    def unbounded(self) -> bool:
        """True when the skill config declared no ceiling."""
        return self._remaining is None

    @property
    def exhausted(self) -> bool:
        return self._remaining is not None and self._remaining <= 0

    @property
    def remaining(self) -> int | None:
        return self._remaining

    def take(self, prospects: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The subset this run may still keep, decrementing the allowance."""
        if self._remaining is None:
            return list(prospects)
        kept = allocate(prospects, self._remaining)
        self._remaining -= len(kept)
        return kept


#: A raw discovery sweep: takes the tool context and the emit callback the engine
#: should publish through, and returns the prospects it assembled.
Sweep = Callable[
    [dict[str, Any], Callable[[dict[str, Any]], None]], list[dict[str, Any]]
]


def capped_discover(
    sweep: Sweep,
    *,
    budget: ProspectBudget,
    emit: Callable[[dict[str, Any]], None],
    log: Callable[[str], None] = lambda _: None,
) -> Callable[[dict[str, Any]], list[dict[str, Any]]]:
    """Wrap a discovery sweep so the ceiling applies BEFORE anything is forwarded.

    This ordering is the whole point, and it is not an implementation detail. The
    engine publishes its `prospects` event from *inside* the sweep, and AEO writes
    prospects `ON CONFLICT ("id") DO NOTHING`, so a forwarded row is permanent and
    un-retractable. A wrapper that let the event through and truncated the return
    value would bound the per-prospect phases while still persisting every row —
    passing a count assertion on the phases and failing the actual requirement.

    Every non-`prospects` event passes straight through, unheld: `phase_start` /
    `phase_complete` are progress and must not be delayed behind a sweep.
    """

    def _discover(ctx: dict[str, Any]) -> list[dict[str, Any]]:
        if budget.exhausted:
            # Skip the SWEEP, not just its results. A round whose every candidate
            # would be discarded is a dozen grounded searches spent to learn
            # nothing, and `discover_in_area` reads an empty result as an exhausted
            # search and stops — which is the outcome we want anyway.
            log("prospect ceiling reached — skipping this discovery sweep")
            return []

        held: list[dict[str, Any]] = []

        def _hold(event: dict[str, Any]) -> None:
            if event.get("type") == "prospects":
                held.append(event)
                return
            emit(event)

        found = sweep(ctx, _hold)
        kept = budget.take(found)
        if len(kept) < len(found):
            log(
                f"prospect ceiling: keeping {len(kept)} of {len(found)} discovered "
                f"(multi-source first, then round-robin by source)"
            )

        kept_ids = {p.get("id") for p in kept}
        for event in held:
            items = [
                item
                for item in event.get("items") or []
                if isinstance(item, dict) and item.get("id") in kept_ids
            ]
            # An empty list is NOT forwarded. We key on `items` (the ENGINE's event
            # shape); `aeo.event_mapping` later renames it to AEO's `data`, where
            # `@ArrayMinSize(1)` makes an empty array a 400 that fails the whole run.
            if items:
                emit({**event, "items": items})
        return kept

    return _discover
