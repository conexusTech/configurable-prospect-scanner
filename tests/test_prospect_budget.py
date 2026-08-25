"""Tests for the per-run prospect ceiling. Ours.

Exists because three earlier things looked like a cap and none of them was one —
`discovery.target_prospects` is a floor, `SCANNER_TOP_N`'s ranking cut is dead on the
AEO path, and `contacts.max_prospects` sits downstream of the expensive phases. A
production run discovered 262 prospects and was killed by a one-hour deadline having
scored none of them.

The properties that actually matter here are the *allocation* ones. A cap that took
the first N in completion order would be a plausible-looking regression: it would pass
any count assertion while silently reducing a four-source recipe to one source.
"""

from __future__ import annotations

import json

from aeo.phases.geo_filter import build_target_area
from aeo.phases.geo_loop import discover_in_area
from aeo.phases.prospect_budget import ProspectBudget, allocate, capped_discover


def _p(pid: str, *sources: str) -> dict:
    return {
        "id": pid,
        "company_name": f"Firm {pid}",
        "discovery_data": {
            "sources_found_in": sorted(sources),
            "source_count": len(set(sources)),
        },
    }


# ── allocate: the ordering guarantees ────────────────────────────────────


def test_under_the_limit_is_a_passthrough():
    items = [_p("a", "s1"), _p("b", "s2")]
    assert allocate(items, 10) == items


def test_zero_and_negative_keep_nothing():
    items = [_p("a", "s1")]
    assert allocate(items, 0) == []
    assert allocate(items, -5) == []


def test_multi_source_prospects_are_kept_first():
    """`source_count > 1` is the only quality signal available before scoring."""
    items = [
        _p("single-1", "permits"),
        _p("double", "permits", "rfps"),
        _p("single-2", "permits"),
    ]
    kept = allocate(items, 1)
    assert [p["id"] for p in kept] == ["double"]


def test_a_cap_never_starves_a_source():
    """The regression a naive `[:n]` would pass: four sources in, one source out."""
    items = [_p(f"permits-{i}", "permits") for i in range(10)]
    items += [_p(f"rfps-{i}", "rfps") for i in range(10)]
    items += [_p(f"props-{i}", "props") for i in range(10)]
    items += [_p(f"upstream-{i}", "upstream") for i in range(10)]

    kept = allocate(items, 8)

    per_source = {}
    for prospect in kept:
        source = prospect["discovery_data"]["sources_found_in"][0]
        per_source[source] = per_source.get(source, 0) + 1
    assert len(kept) == 8
    assert per_source == {"permits": 2, "rfps": 2, "props": 2, "upstream": 2}


def test_uneven_sources_still_fill_the_whole_budget():
    """A source running dry must not leave the ceiling unmet."""
    items = [_p("only-rfp", "rfps")] + [_p(f"permits-{i}", "permits") for i in range(9)]
    kept = allocate(items, 5)
    assert len(kept) == 5
    assert sum(1 for p in kept if p["id"] == "only-rfp") == 1


def test_allocation_is_deterministic():
    """Prospect ids are uuid5(scan_run_id:name), so the same slice must recur."""
    items = [_p(f"permits-{i}", "permits") for i in range(6)]
    items += [_p(f"rfps-{i}", "rfps") for i in range(6)]
    assert [p["id"] for p in allocate(items, 5)] == [
        p["id"] for p in allocate(list(reversed(items)), 5)
    ]


def test_missing_discovery_data_does_not_raise():
    kept = allocate([{"id": "bare"}, {"id": "odd", "discovery_data": "not-a-dict"}], 1)
    assert len(kept) == 1


# ── ProspectBudget: the cumulative guarantee ─────────────────────────────


def test_absent_limit_is_unbounded():
    budget = ProspectBudget(None)
    assert budget.unbounded
    assert not budget.exhausted
    assert len(budget.take([_p(str(i), "s") for i in range(500)])) == 500


def test_non_positive_and_junk_limits_read_as_unbounded():
    """An absent ceiling means no ceiling — never a ceiling of zero."""
    for limit in (0, -1, None):
        assert ProspectBudget(limit).unbounded


def test_the_budget_is_cumulative_across_rounds():
    """Per-round would bound nothing: max_discovery_rounds defaults to 2."""
    budget = ProspectBudget(10)
    first = budget.take([_p(f"r1-{i}", "s") for i in range(8)])
    second = budget.take([_p(f"r2-{i}", "s") for i in range(8)])
    assert len(first) == 8
    assert len(second) == 2
    assert budget.exhausted
    assert budget.take([_p("r3-0", "s")]) == []


def test_exhausted_only_once_the_allowance_is_spent():
    budget = ProspectBudget(3)
    assert not budget.exhausted
    budget.take([_p("a", "s"), _p("b", "s")])
    assert not budget.exhausted
    assert budget.remaining == 1
    budget.take([_p("c", "s")])
    assert budget.exhausted


# ── capped_discover: the interception ordering ────────────────────────────
#
# These are the load-bearing ones. The cap exists to bound DURABLE ROWS, and AEO
# writes prospects ON CONFLICT DO NOTHING — so a wrapper that forwarded the event and
# only truncated the return value would pass every phase-cost assertion above while
# persisting all 262 rows anyway.


def _sweep(prospects: list[dict], extra_events: list[dict] | None = None):
    """A stand-in for `als.discover`: emits its prospects event, then returns them."""
    calls = []

    def sweep(ctx, emit):
        calls.append(ctx)
        for event in extra_events or []:
            emit(event)
        emit({"type": "prospects", "phase": "discover", "items": list(prospects)})
        return list(prospects)

    sweep.calls = calls
    return sweep


def test_only_the_kept_prospects_are_ever_forwarded():
    found = [_p(f"permits-{i}", "permits") for i in range(6)]
    found += [_p(f"rfps-{i}", "rfps") for i in range(6)]
    emitted: list[dict] = []

    discover = capped_discover(
        _sweep(found), budget=ProspectBudget(4), emit=emitted.append
    )
    kept = discover({})

    forwarded = [e for e in emitted if e["type"] == "prospects"]
    assert len(forwarded) == 1
    assert len(forwarded[0]["items"]) == 4
    assert {i["id"] for i in forwarded[0]["items"]} == {p["id"] for p in kept}


def test_progress_events_pass_through_unheld():
    """`phase_start`/`phase_complete` must not be delayed behind the sweep."""
    emitted: list[dict] = []
    sweep = _sweep(
        [_p("a", "s")],
        extra_events=[
            {"type": "phase_start", "phase": "permits"},
            {"type": "phase_complete", "phase": "permits", "count": 1},
        ],
    )
    capped_discover(sweep, budget=ProspectBudget(1), emit=emitted.append)({})

    assert [e["type"] for e in emitted] == [
        "phase_start",
        "phase_complete",
        "prospects",
    ]


def test_the_event_keeps_its_phase_so_aeo_can_map_it():
    """`phase` is per-EVENT here and per-ITEM in AEO; dropping it 400s every item."""
    emitted: list[dict] = []
    capped_discover(
        _sweep([_p("a", "s"), _p("b", "s")]),
        budget=ProspectBudget(1),
        emit=emitted.append,
    )({})
    assert emitted[0]["phase"] == "discover"


def test_an_exhausted_budget_skips_the_sweep_entirely():
    """Not just its results — a discarded round is a dozen grounded searches wasted."""
    budget = ProspectBudget(2)
    budget.take([_p("a", "s"), _p("b", "s")])
    sweep = _sweep([_p("c", "s")])
    emitted: list[dict] = []

    assert capped_discover(sweep, budget=budget, emit=emitted.append)({}) == []
    assert sweep.calls == []
    assert emitted == []


def test_nothing_is_forwarded_when_the_whole_sweep_is_cut():
    """AEO declares @ArrayMinSize(1), so `{"data": []}` is a 400 that fails the run."""
    budget = ProspectBudget(1)
    budget.take([_p("a", "s")])
    # Budget is spent, but a caller could still invoke a wrapper built earlier —
    # guard the empty-forward path directly rather than relying on the skip above.
    emitted: list[dict] = []
    discover = capped_discover(
        _sweep([_p("b", "s")]), budget=ProspectBudget(0), emit=emitted.append
    )
    discover({})
    assert not [e for e in emitted if e.get("type") == "prospects" and not e["items"]]


def test_unbounded_forwards_everything_and_never_skips():
    found = [_p(str(i), "s") for i in range(50)]
    emitted: list[dict] = []
    sweep = _sweep(found)
    kept = capped_discover(
        sweep, budget=ProspectBudget(None), emit=emitted.append
    )({})
    assert len(kept) == 50
    assert len(emitted[0]["items"]) == 50
    assert len(sweep.calls) == 1


# ── composition with the real geo loop ────────────────────────────────────
#
# The units above prove the ceiling bounds what is PERSISTED. This proves it bounds
# what is SPENT — the reason the ceiling exists. `discover_in_area` verifies every
# fresh candidate with one grounded call each, BEFORE it checks its target count, so
# a cap applied anywhere downstream of the sweep would still pay for all 262.


def _counting_verifier(counter: list[str]):
    """Provider that answers the location question and records every call."""

    def call(prompt, **kwargs):
        counter.append(prompt)
        return json.dumps([{"city": "Austin", "state": "TX", "zip_code": "78701"}])

    return call


def _parse(text: str) -> list[dict]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [p for p in parsed if isinstance(p, dict)] if isinstance(parsed, list) else []


def test_the_ceiling_bounds_verification_calls_not_just_rows():
    area = build_target_area(
        [{"zip_code": "78701", "city": "Austin", "state": "TX"}], None
    )
    found = [_p(f"permits-{i}", "permits") for i in range(30)]
    found += [_p(f"rfps-{i}", "rfps") for i in range(30)]

    verify_calls: list[str] = []
    emitted: list[dict] = []

    def sweep(ctx, emit):
        emit({"type": "prospects", "phase": "discover", "items": list(found)})
        return list(found)

    in_area, rejects = discover_in_area(
        tool_context={"sources": {}},
        area=area,
        # A FLOOR, and deliberately tiny — it must not be what bounds the work.
        target_count=1,
        discover=capped_discover(
            sweep, budget=ProspectBudget(10), emit=emitted.append
        ),
        provider=_counting_verifier(verify_calls),
        provider_config={},
        parse_json_array=_parse,
        max_rounds=2,
    )

    # 60 discovered, 10 allowed: exactly 10 verification calls, not 60.
    assert len(verify_calls) == 10
    assert len(in_area) == 10
    assert rejects == []
    assert len(emitted[0]["items"]) == 10


# ── cross-run dedupe: exclude BEFORE the ceiling ─────────────────────────


def test_a_company_the_org_already_holds_is_never_forwarded():
    found = [_p("a", "permits"), _p("b", "permits"), _p("c", "permits")]
    emitted: list[dict] = []

    discover = capped_discover(
        _sweep(found),
        budget=ProspectBudget(10),
        emit=emitted.append,
        known_companies={"firm b"},
    )
    kept = discover({})

    assert {p["id"] for p in kept} == {"a", "c"}
    forwarded = [e for e in emitted if e["type"] == "prospects"][0]
    assert {i["id"] for i in forwarded["items"]} == {"a", "c"}


def test_an_exclusion_FREES_a_ceiling_slot_rather_than_consuming_one():
    """The whole point of filtering before the ceiling rather than after.

    Held duplicates used to be discovered, verified, enriched and only then
    discarded gateway-side at insert -- so each one consumed a ceiling slot a
    genuinely new prospect could have had. Measured waste before this: 8.3% on a
    young org and 38% on a mature one, the tax compounding with an org's tenure.

    With a ceiling of 2 and one of the first three already held, a correct
    implementation still returns 2 NEW prospects. An implementation that filtered
    after the ceiling would return 1.
    """
    found = [_p("a", "permits"), _p("b", "permits"), _p("c", "permits")]

    discover = capped_discover(
        _sweep(found),
        budget=ProspectBudget(2),
        emit=lambda _: None,
        known_companies={"firm a"},
    )
    kept = discover({})

    assert len(kept) == 2, "an excluded duplicate must not spend a ceiling slot"
    assert "a" not in {p["id"] for p in kept}


def test_absent_or_empty_known_companies_is_a_no_op():
    """Safe in either deploy order: a gateway that does not send the field yet."""
    found = [_p("a", "permits"), _p("b", "permits")]

    for known in (None, set()):
        discover = capped_discover(
            _sweep(found),
            budget=ProspectBudget(10),
            emit=lambda _: None,
            known_companies=known,
        )
        assert len(discover({})) == 2


def test_matching_ignores_case_and_surrounding_whitespace():
    """Must mirror the gateway's `lower(trim(company_name))` exactly.

    If the two normalisations diverge this filter excludes one set while the
    gateway drops a different one, and the symptom is silent -- prospects vanish
    with no error and no log.
    """
    found = [
        {"id": "x", "company_name": "  ACME Roofing  "},
        {"id": "y", "company_name": "Other Co"},
    ]
    discover = capped_discover(
        _sweep(found),
        budget=ProspectBudget(10),
        emit=lambda _: None,
        known_companies={"acme roofing"},
    )
    assert {p["id"] for p in discover({})} == {"y"}


def test_a_missing_or_blank_company_name_is_never_excluded():
    """A nameless row cannot be proved a duplicate, so it must survive."""
    found = [
        {"id": "n1", "company_name": None},
        {"id": "n2", "company_name": ""},
        {"id": "n3"},
    ]
    discover = capped_discover(
        _sweep(found),
        budget=ProspectBudget(10),
        emit=lambda _: None,
        known_companies={"acme roofing"},
    )
    assert len(discover({})) == 3


def test_the_dedupe_is_reported_not_silent():
    """A cap that quietly drops work reads as 'covered everything' when it did not."""
    found = [_p("a", "permits"), _p("b", "permits")]
    lines: list[str] = []

    discover = capped_discover(
        _sweep(found),
        budget=ProspectBudget(10),
        emit=lambda _: None,
        log=lines.append,
        known_companies={"firm a"},
    )
    discover({})

    assert any("cross-run dedupe" in ln and "1 of 2" in ln for ln in lines)
