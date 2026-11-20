"""A source that names a gateway feed reads its prospects from the gateway's records. Ours.

`sources-read-gateway-records` (Phase CRM-3). The rows replace a paid search: they are
kept only when they fall in the organization's territory, assembled by the engine's own
assembly (same ids, own-listing exclusion), filtered against what the org already holds
before the run's prospect ceiling, and then go through scoring and contacts as any
prospect does.

The gateway answer is pinned in `fixtures/source-records/health_inspections.json`, a file
aeo-backend's `scan-sources-read-gateway-feeds` pins too.
"""

from __future__ import annotations

import copy
import http.client
import io
import json
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

import av_lead_scanner as als
import runner_harness as harness
from aeo.config_mapping import UnmappedConfigError, build_tool_context
from aeo.phases.geo_filter import STRICTNESS_METRO, STRICTNESS_STATE, build_target_area
from aeo.phases.source_records import (
    MAX_ROWS,
    SourceRecordsError,
    feed_source_problems,
    feed_sources,
    in_territory,
    records_sweep,
    rows_of,
)

FIXTURE = Path(__file__).parent / "fixtures" / "source-records" / "health_inspections.json"
SCAN_RUN_ID = "11111111-2222-4333-8444-555555555555"
MARKETS = ["Richmond, VA"]
ZIP_ROWS = [
    {"zip_code": "23220", "city": "Richmond", "state": "VA"},
    {"zip_code": "23111", "city": "Mechanicsville", "state": "VA"},
]

#: Every key the contract pins on an item.
ITEM_KEYS = {
    "establishment_name", "establishment_name_plain", "location_address", "city", "state",
    "zip_code", "industry", "signal_type", "signal_date", "source_url", "signal_description",
    "matched_terms", "inspection_id", "permit_id", "district", "source_name",
}

#: The fixture's names as the gateway gives them: every row "<name>, <street>" (the
#: address's first line; else the city; else the plain name alone), shared name or not.
BELLA = "Bella's Bistro, 100 E Cary St"
SUBWAY_BROAD = "Subway, 123 W Broad St"
SUBWAY_FOREST = "Subway, 4500 Forest Hill Ave"
MECHANICSVILLE = "Mechanicsville Pho, 8100 Mechanicsville Tpke"
RIVER_CITY = "River City Diner, 803 N Arthur Ashe Blvd"
FAN_DISTRICT = "Fan District Cafe, 1700 W Main St"
HARBOR = "Harbor Grill, 9 Waterside Dr"
CAROLINA = "Carolina Smokehouse, 12 Fayetteville St"
FOOD_TRUCK = "Food Truck 7"  # no street and no city: the plain name alone
WATERSIDE = "Waterside Oyster Bar, 333 Waterside Dr"

IN_TERRITORY = {BELLA, SUBWAY_BROAD, SUBWAY_FOREST, MECHANICSVILLE, RIVER_CITY, FAN_DISTRICT}

FEED_SOURCE = {
    "name_field": "establishment_name",
    "fields": [
        "establishment_name", "location_address", "city", "state", "zip_code", "industry",
        "signal_type", "signal_date", "signal_description", "source_url",
    ],
    "queries": ["gasket"],
    "records": {"feed": "health_inspections", "window_days": 30},
}


def _answer() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _ctx(org_name: str = "Seal Pros of Virginia", **sources: Any) -> dict[str, Any]:
    return {
        "organization": {"name": org_name, "markets": MARKETS},
        "sources": sources or {"inspections": copy.deepcopy(FEED_SOURCE)},
    }


class _Run:
    """One sweep, with what it emitted and logged."""

    def __init__(self, answer: Any = None, *, ctx: dict[str, Any] | None = None,
                 area: Any = None, strictness: str = STRICTNESS_METRO,
                 fetch: Any = None) -> None:
        self.ctx = ctx or _ctx()
        self.events: list[dict[str, Any]] = []
        self.logs: list[str] = []
        self.asked: list[str] = []
        payload = _answer() if answer is None else answer

        def _fetch(source: str) -> Any:
            self.asked.append(source)
            return copy.deepcopy(payload)

        sweep = records_sweep(
            feed_sources(self.ctx["sources"]),
            fetch=fetch or _fetch,
            area=area if area is not None else build_target_area(ZIP_ROWS, MARKETS),
            strictness=strictness,
            normalize=als.normalize_name,
            assemble=lambda groups, ctx: als._assemble_prospects(
                groups=groups,
                scan_run_id=SCAN_RUN_ID,
                canonical=als.canonical_fields_from_sources(ctx.get("sources")),
                excluded_names=als.excluded_prospect_names(ctx),
                log=self.logs.append,
            ),
            log=self.logs.append,
        )
        self.prospects = sweep(self.ctx, self.events.append)

    def names(self) -> set[str]:
        return {p["company_name"] for p in self.prospects}

    def by_name(self, name: str) -> dict[str, Any]:
        return next(p for p in self.prospects if p["company_name"] == name)


def _row(name: str) -> dict[str, Any]:
    return next(r for r in _answer()["items"] if r["establishment_name"] == name)


class TestTheContractFixture:
    """The file both repos pin. A shape drifting here is a drift in the contract."""

    def test_the_answer_carries_every_pinned_key(self):
        answer = _answer()
        assert {"feed", "terms", "since", "total", "truncated", "items"} <= set(answer)
        assert answer["total"] == len(answer["items"])

    def test_every_item_carries_every_pinned_key(self):
        for item in _answer()["items"]:
            assert set(item) == ITEM_KEYS, item["establishment_name"]
            assert item["signal_type"] in {
                "health_inspection_violation", "health_inspection_comment",
            }

    def test_every_row_names_its_source_and_carries_the_bare_sentence(self):
        # The gateway's decided shape: the finding's source name rides on the row, and
        # the description is the sentence alone, never prefixed with its kind.
        for item in _answer()["items"]:
            assert item["source_name"] == "Virginia health inspection report", item["inspection_id"]
            assert not item["signal_description"].startswith("Comment:"), item["inspection_id"]
            assert item["signal_description"].strip() == item["signal_description"]

    def test_the_names_these_tests_use_are_the_fixtures_names(self):
        # A name that is not in the fixture makes every "not in" assertion pass vacuously.
        names = {item["establishment_name"] for item in _answer()["items"]}
        assert names == {BELLA, SUBWAY_BROAD, SUBWAY_FOREST, MECHANICSVILLE, RIVER_CITY,
                         FAN_DISTRICT, HARBOR, CAROLINA, FOOD_TRUCK, WATERSIDE}

    def test_every_name_is_the_plain_name_and_its_street_else_its_city(self):
        # The gateway's naming rule, as the fixture must carry it: every row, shared name
        # or not, is "<name>, <street>" (the address's first line; else the city; else
        # the plain name alone), so a restaurant keeps one name in every answer.
        for item in _answer()["items"]:
            plain = item["establishment_name_plain"]
            street = (item["location_address"] or "").split(",")[0].strip()
            city = (item["city"] or "").strip()
            expected = f"{plain}, {street}" if street else f"{plain}, {city}" if city else plain
            assert item["establishment_name"] == expected, item["inspection_id"]

    def test_the_fixture_carries_two_branches_named_apart(self):
        # Pins the FIXTURE, so the scanner's chain tests read the shape the gateway sends.
        # That the gateway really names branches apart is proved in aeo-backend, not here.
        subways = [i for i in _answer()["items"] if i["establishment_name_plain"] == "Subway"]
        assert len(subways) == 2
        assert len({s["establishment_name"] for s in subways}) == 2


class TestRowsInTerritoryOnly:
    """`rows-in-territory-only`: only rows in the organization's territory become prospects."""

    def test_only_rows_in_the_area_become_prospects(self):
        run = _Run()
        assert run.names() == IN_TERRITORY

    def test_out_of_area_out_of_state_and_unplaced_rows_are_left_out(self):
        run = _Run()
        assert HARBOR not in run.names()  # Norfolk: a Virginia city not in the area
        assert CAROLINA not in run.names()  # another state
        # A matching state alone is no placement: no city, no ZIP.
        assert FOOD_TRUCK not in run.names()
        # Nor a matching state with a ZIP outside the area and no city.
        assert WATERSIDE not in run.names()

    def test_a_zip_from_zip_discovery_admits_a_suburb(self):
        without_zips = _Run(area=build_target_area([], MARKETS))
        assert MECHANICSVILLE not in without_zips.names()
        assert MECHANICSVILLE in _Run().names()

    def test_state_strictness_admits_any_city_in_the_state_and_nothing_outside_it(self):
        run = _Run(strictness=STRICTNESS_STATE)
        assert HARBOR in run.names()
        # The classifier's own answer under `state`: the state matches, so they are in.
        assert FOOD_TRUCK in run.names()
        assert WATERSIDE in run.names()
        assert CAROLINA not in run.names()

    def test_the_rows_kept_and_left_out_are_counted_in_the_log(self):
        run = _Run()
        line = next(entry for entry in run.logs if "inspections" in entry and "row(s)" in entry)
        assert "answered 10 row(s)" in line
        assert "6 row(s) placed in the territory" in line
        assert "4 row(s) not placed in it" in line
        # Rows, not prospects: held companies and the ceiling come after this line.
        assert "before held companies and the prospect ceiling" in line

    def test_in_territory_reads_the_row_address_fields(self):
        area = build_target_area([], MARKETS)
        assert in_territory({"city": "Richmond", "state": "VA"}, area, STRICTNESS_METRO)
        assert not in_territory({"city": "Norfolk", "state": "VA"}, area, STRICTNESS_METRO)
        assert not in_territory({}, area, STRICTNESS_METRO)

    def test_a_zip_only_in_the_address_still_places_the_row(self):
        # No `zip_code`, and a city the area does not list: only the ZIP inside the
        # address can place it, so this is the address fallback and nothing else.
        row = {"city": "Glen Allen", "state": "VA", "zip_code": "",
               "location_address": "1 Main St, Glen Allen, VA 23220"}
        area = build_target_area(ZIP_ROWS, MARKETS)
        assert in_territory(row, area, STRICTNESS_METRO)
        assert not in_territory({**row, "location_address": "1 Main St, Glen Allen, VA 23059"},
                                area, STRICTNESS_METRO)

    def test_under_metro_a_state_match_alone_is_not_a_placement(self):
        area = build_target_area(ZIP_ROWS, MARKETS)
        state_only = {"city": "", "state": "VA", "zip_code": "", "location_address": ""}
        outside_zip = {**state_only, "zip_code": "23510", "location_address": "333 Waterside Dr"}
        for row in (state_only, outside_zip):
            assert not in_territory(row, area, STRICTNESS_METRO), row
            # Any strictness other than `state` reads as metro, as the classifier does.
            assert not in_territory(row, area, "anything-else"), row
            assert in_territory(row, area, STRICTNESS_STATE), row


class TestAssembly:
    """Through the engine's own assembly: same ids, the row kept whole, own listing out."""

    def test_ids_are_the_engines_ids(self):
        run = _Run()
        for p in run.prospects:
            expected = uuid.uuid5(als._NAMESPACE, f"{SCAN_RUN_ID}:{als.normalize_name(p['company_name'])}")
            assert p["id"] == str(expected)

    def test_the_row_lands_whole_in_discovery_data(self):
        run = _Run()
        row = _row(BELLA)
        stored = run.by_name(BELLA)["discovery_data"]["by_source"]["inspections"]
        # The engine keeps every non-empty value of a source row; this row has no empty one.
        assert stored == row

    def test_the_name_as_given_is_the_company_and_the_plain_name_rides_along(self):
        run = _Run()
        assert {SUBWAY_BROAD, SUBWAY_FOREST} <= run.names()
        for name in (SUBWAY_BROAD, SUBWAY_FOREST):
            stored = run.by_name(name)["discovery_data"]["by_source"]["inspections"]
            assert stored["establishment_name_plain"] == "Subway"

    def test_the_prospect_columns_come_from_the_row(self):
        p = _Run().by_name(MECHANICSVILLE)
        assert (p["city"], p["state"], p["zip_code"]) == ("Mechanicsville", "VA", "23111")
        assert p["address"] == "8100 Mechanicsville Tpke, Mechanicsville, VA 23111"
        assert p["industry"] == "Restaurant"
        assert p["sources"] == "inspections"

    def test_the_organizations_own_listing_is_excluded(self):
        # Matched on the name as the feed gives it ("<name>, <street>"): the engine's
        # exclusion compares normalised names, so an org named only "River City Diner"
        # would need that form among its names or aliases.
        run = _Run(ctx=_ctx(org_name=RIVER_CITY))
        assert RIVER_CITY not in run.names()
        assert any("own listing" in entry for entry in run.logs)

    def test_it_emits_start_then_complete_then_prospects_like_a_search(self):
        run = _Run()
        types = [(e["type"], e.get("phase")) for e in run.events]
        assert types == [
            ("phase_start", "inspections"),
            ("phase_complete", "inspections"),
            ("prospects", "discover"),
        ]
        assert run.events[1]["count"] == 6
        items = run.events[2]["items"]
        assert {i["company_name"] for i in items} == IN_TERRITORY
        assert all("_internal" not in i for i in items)

    def test_the_gateway_is_asked_once_per_feed_source(self):
        ctx = _ctx(inspections=copy.deepcopy(FEED_SOURCE), second=copy.deepcopy(FEED_SOURCE))
        run = _Run(ctx=ctx)
        assert run.asked == ["inspections", "second"]
        # One establishment from two feed sources is one prospect found in both.
        p = run.by_name(BELLA)
        assert p["discovery_data"]["sources_found_in"] == ["inspections", "second"]


class TestWhatTheSweepRefuses:
    """Each refusal is a named run error: the run stops and says why."""

    def test_an_answer_for_another_feed_is_refused(self):
        answer = {**_answer(), "feed": "something_else"}
        with pytest.raises(SourceRecordsError) as exc:
            _Run(answer)
        assert exc.value.reason == "source_records_feed_mismatch"
        assert "health_inspections" in str(exc.value) and "something_else" in str(exc.value)

    def test_an_answer_with_no_item_list_is_refused(self):
        for bad in ({"feed": "health_inspections"}, [], "nope",
                    {"feed": "health_inspections", "items": {"a": 1}}):
            with pytest.raises(SourceRecordsError) as exc:
                _Run(bad)
            assert exc.value.reason == "source_records_malformed"

    def test_items_none_of_which_is_an_object_are_refused(self):
        with pytest.raises(SourceRecordsError) as exc:
            _Run({**_answer(), "items": ["junk", 7, None, ["a"]]})
        assert exc.value.reason == "source_records_malformed"

    def test_more_rows_than_the_contract_allows_are_refused(self):
        rows = [{**_row(BELLA), "permit_id": f"P{i}"} for i in range(MAX_ROWS + 1)]
        with pytest.raises(SourceRecordsError) as exc:
            _Run({**_answer(), "items": rows, "total": len(rows)})
        assert exc.value.reason == "source_records_malformed"
        assert str(MAX_ROWS) in str(exc.value)

    def test_exactly_the_contracts_limit_is_read(self):
        rows = [{**_row(BELLA), "establishment_name": f"Bistro {i}",
                 "permit_id": f"P{i}"} for i in range(MAX_ROWS)]
        run = _Run({**_answer(), "items": rows, "total": len(rows)})
        assert len(run.prospects) == MAX_ROWS

    def test_rows_that_never_carry_the_sources_name_field_are_refused(self):
        source = {**copy.deepcopy(FEED_SOURCE), "name_field": "organization_name"}
        with pytest.raises(SourceRecordsError) as exc:
            _Run(ctx=_ctx(inspections=source))
        assert exc.value.reason == "source_records_unnamed"
        assert "organization_name" in str(exc.value)
        # It says what the rows do carry, so the fix is one look away.
        assert "establishment_name" in str(exc.value) and "permit_id" in str(exc.value)

    def test_a_long_feed_name_from_the_gateway_is_cut_in_the_error(self):
        answer = {**_answer(), "feed": "x" * 5000}
        with pytest.raises(SourceRecordsError) as exc:
            _Run(answer)
        assert exc.value.reason == "source_records_feed_mismatch"
        assert len(str(exc.value)) < 500

    def test_long_terms_and_totals_from_the_gateway_are_cut_in_the_log(self):
        run = _Run({**_answer(), "terms": ["t" * 5000], "since": "s" * 5000,
                    "truncated": True, "total": "9" * 5000})
        assert run.logs and all(len(entry) < 1200 for entry in run.logs)

    def test_an_empty_answer_is_not_a_refusal(self):
        run = _Run({**_answer(), "items": [], "total": 0})
        assert run.prospects == []

    def test_an_area_with_nothing_to_match_is_refused_before_the_gateway_is_asked(self):
        asked: list[str] = []
        with pytest.raises(SourceRecordsError) as exc:
            _Run(area=build_target_area([], []), fetch=lambda s: asked.append(s))
        assert exc.value.reason == "source_records_no_territory"
        assert asked == []

    def test_a_refusal_from_the_fetch_propagates_unchanged(self):
        def _refuse(source: str) -> Any:
            raise SourceRecordsError("source_records_refused", "HTTP 404")

        with pytest.raises(SourceRecordsError) as exc:
            _Run(fetch=_refuse)
        assert exc.value.reason == "source_records_refused"

    def test_a_truncated_answer_says_how_much_was_left_unread(self):
        run = _Run({**_answer(), "truncated": True, "total": 812})
        assert any("WARNING" in e and "10 of 812" in e for e in run.logs)
        assert run.names() == IN_TERRITORY

    def test_items_that_are_not_records_are_counted_in_the_log(self):
        run = _Run({**_answer(), "items": [_row(BELLA), "junk", 7], "total": 3})
        line = next(entry for entry in run.logs if "inspections" in entry and "row(s)" in entry)
        assert "answered 1 row(s)" in line
        assert "2 item(s) that are not records" in line
        assert run.names() == {BELLA}

    def test_only_the_boolean_true_means_truncated(self):
        for not_true in ("true", "false", 1, "yes", None):
            run = _Run({**_answer(), "truncated": not_true, "total": 812})
            assert not any("WARNING" in e for e in run.logs), not_true


class TestNamesThatCollide:
    """Two establishments whose names meet under the engine's normalisation stay apart.

    The gateway names every row "<name>, <street>", so two rows meet only when name and
    street both do: "McDonald's, 5 E Main St" (one permit) and "MCDONALDS, 5 E MAIN ST"
    (another, the same storefront re-permitted). The engine groups rows by
    `normalize_name` and merges every field of a group, so they would become ONE prospect
    carrying the first's address and the second's finding. A safety net: one row per
    source and normalised name is kept, the newest finding. One permit under two
    spellings still merges.
    """

    @staticmethod
    def _two(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
        base = _row(BELLA)
        return {**_answer(), "items": [{**base, **first}, {**base, **second}], "total": 2}

    # OLDER's name and address are the longer ones, so a merge (which keeps the longest
    # value of each field) would show OLDER's where NEWER's belongs.
    OLDER = {"establishment_name": "McDonald's, 5 E Main St",
             "establishment_name_plain": "McDonald's",
             "permit_id": "P1", "signal_date": "2026-11-01", "inspection_id": "IR-1",
             "location_address": "5 E Main St, Richmond, VA 23220-1234",
             "signal_description": "Older finding: cooler gasket torn."}
    NEWER = {"establishment_name": "MCDONALDS, 5 E MAIN ST",
             "establishment_name_plain": "MCDONALDS",
             "permit_id": "P2", "signal_date": "2026-11-08", "inspection_id": "IR-2",
             "location_address": "5 E Main St, Richmond, VA 23220",
             "signal_description": "Newer finding: freezer gasket split."}

    def test_two_establishments_are_not_merged_and_the_newest_finding_is_kept(self):
        run = _Run(self._two(self.OLDER, self.NEWER))
        assert len(run.prospects) == 1
        [p] = run.prospects
        stored = p["discovery_data"]["by_source"]["inspections"]
        newest = {**_row(BELLA), **self.NEWER}
        # Every field from the one row: none of the older establishment's leaks in.
        assert stored == newest
        assert p["company_name"] == "MCDONALDS, 5 E MAIN ST"
        assert p["address"] == self.NEWER["location_address"]

    def test_the_newest_wins_whichever_comes_first(self):
        run = _Run(self._two(self.NEWER, self.OLDER))
        [p] = run.prospects
        assert p["discovery_data"]["by_source"]["inspections"]["permit_id"] == "P2"

    def test_the_dropped_establishment_is_counted_in_a_warning(self):
        run = _Run(self._two(self.OLDER, self.NEWER))
        warning = next(e for e in run.logs if e.startswith("WARNING") and "collide" in e)
        assert "1 row(s)" in warning and "McDonald's" in warning

    def test_one_permit_under_two_spellings_still_merges_without_a_warning(self):
        same = {**self.NEWER, "permit_id": "P1"}
        run = _Run(self._two(self.OLDER, same))
        assert len(run.prospects) == 1
        assert not any("collide" in e for e in run.logs)


    @pytest.mark.parametrize("unreadable", ["unknown", 20261101, "11/05/2026", "", None])
    def test_a_date_that_is_not_an_iso_string_counts_as_the_oldest(self, unreadable):
        # Compared as dates: as strings, "unknown" and 20261101 would both sort NEWER
        # than any 2026 date and win. The integer is not the contract's ISO string even
        # though Python 3.11+ would read its digits as a basic-format date.
        odd = {**self.OLDER, "signal_date": unreadable}  # P1
        earlier = {**self.NEWER, "signal_date": "2026-10-15"}  # P2, an ISO date
        for first, second in ((odd, earlier), (earlier, odd)):
            [p] = _Run(self._two(first, second)).prospects
            assert p["discovery_data"]["by_source"]["inspections"]["permit_id"] == "P2", (
                unreadable, first["permit_id"],
            )

    def test_a_timestamp_reads_as_its_date(self):
        stamped = {**self.OLDER, "signal_date": "2026-11-09T08:30:00Z"}  # P1
        [p] = _Run(self._two(stamped, self.NEWER)).prospects  # P2 is 2026-11-08
        assert p["discovery_data"]["by_source"]["inspections"]["permit_id"] == "P1"

    def test_two_unreadable_dates_keep_the_first_establishment(self):
        a = {**self.OLDER, "signal_date": "unknown"}
        b = {**self.NEWER, "signal_date": "n/a"}
        for first, second in ((a, b), (b, a)):
            [p] = _Run(self._two(first, second)).prospects
            assert (p["discovery_data"]["by_source"]["inspections"]["permit_id"]
                    == first["permit_id"])

    def test_the_warning_names_rows_by_the_sources_name_field(self):
        # Named by `establishment_name_plain` here, so the warning must read that field.
        source = {**copy.deepcopy(FEED_SOURCE), "name_field": "establishment_name_plain"}
        older = {**self.OLDER, "establishment_name": "McD West End"}
        newer = {**self.NEWER, "establishment_name": "McD Church Hill"}
        run = _Run(self._two(older, newer), ctx=_ctx(inspections=source))
        warning = next(e for e in run.logs if e.startswith("WARNING") and "collide" in e)
        assert "McDonald's" in warning
        assert "McD West End" not in warning

    def test_rows_without_a_permit_are_worded_as_possibly_one_establishment(self):
        older = {**self.OLDER, "permit_id": ""}
        newer = {**self.NEWER, "permit_id": ""}
        run = _Run(self._two(older, newer))
        warning = next(e for e in run.logs if e.startswith("WARNING") and "collide" in e)
        assert "may be the same establishment" in warning
        assert "another establishment" not in warning

    def test_rows_with_different_permits_are_worded_as_another_establishment(self):
        run = _Run(self._two(self.OLDER, self.NEWER))
        warning = next(e for e in run.logs if e.startswith("WARNING") and "collide" in e)
        assert "another establishment" in warning
        assert "may be the same" not in warning


class TestRowsOf:
    def test_non_object_items_are_skipped(self):
        answer = {**_answer(), "items": [_row(BELLA), "junk", 7, None]}
        assert rows_of(answer, source="s", feed="health_inspections") == [_row(BELLA)]


class TestTheMappingRefusesWhatV1CannotRun:
    """Checked when the config is mapped, before any provider call."""

    SEARCHED = {"name_field": "company_name", "fields": ["company_name"],
                "queries": ["restaurants in {market}"]}

    def test_a_skill_mixing_feed_and_searched_sources_is_refused(self):
        problems = feed_source_problems({"inspections": FEED_SOURCE, "restaurants": self.SEARCHED})
        assert len(problems) == 1
        assert "inspections" in problems[0] and "restaurants" in problems[0]

    def test_all_feed_or_all_searched_is_fine(self):
        assert feed_source_problems({"a": FEED_SOURCE, "b": FEED_SOURCE}) == []
        assert feed_source_problems({"a": self.SEARCHED}) == []

    def test_a_records_block_must_name_a_feed(self):
        for bad in ({"window_days": 30}, {"feed": ""}, {"feed": 7}, "health_inspections", []):
            problems = feed_source_problems({"inspections": {**FEED_SOURCE, "records": bad}})
            assert len(problems) == 1 and "inspections" in problems[0], bad

    def test_build_tool_context_raises_the_mixed_refusal(self):
        from runner_harness import PIPELINE

        ctx = {
            "organization": {"name": "Seal Pros"},
            "geography": {"home_markets": ["Richmond, VA"]},
            "skill": {"config": {
                "product_description": "Gaskets",
                "discovery": {"sources": {"inspections": FEED_SOURCE, "restaurants": self.SEARCHED}},
                "scoring": {"region_bonus": {"max": 10}},
            }},
            "pipeline": PIPELINE,
        }
        with pytest.raises(UnmappedConfigError) as exc:
            build_tool_context(ctx)
        assert any("mixes" in p for p in exc.value.problems)

    def test_a_feed_skill_authoring_validation_is_refused(self):
        for validation in ({"in_market_signals": ["walk-in cooler"]},
                           {"lanes": [{"name": "news", "fields": ["headline"]}]},
                           "validate everything"):
            problems = feed_source_problems({"inspections": FEED_SOURCE}, validation=validation)
            assert len(problems) == 1 and "validation" in problems[0], validation

    def test_no_validation_or_an_empty_one_is_fine_and_a_searched_skill_may_validate(self):
        assert feed_source_problems({"inspections": FEED_SOURCE}, validation=None) == []
        assert feed_source_problems({"inspections": FEED_SOURCE}, validation={}) == []
        assert feed_source_problems({"a": self.SEARCHED}, validation={"lanes": [{"n": 1}]}) == []

    def test_build_tool_context_raises_the_validation_refusal(self):
        from runner_harness import PIPELINE

        ctx = {
            "organization": {"name": "Seal Pros"},
            "geography": {"home_markets": ["Richmond, VA"]},
            "skill": {"config": {
                "product_description": "Gaskets",
                "discovery": {"sources": {"inspections": FEED_SOURCE}},
                "validation": {"lanes": [{"name": "news", "fields": ["headline"]}]},
                "scoring": {"region_bonus": {"max": 10}},
            }},
            "pipeline": PIPELINE,
        }
        with pytest.raises(UnmappedConfigError) as exc:
            build_tool_context(ctx)
        assert any("`validation`" in p and "gateway feed" in p for p in exc.value.problems)

    def test_the_feed_key_survives_the_mapping(self):
        from runner_harness import PIPELINE

        ctx = {
            "organization": {"name": "Seal Pros"},
            "geography": {"home_markets": ["Richmond, VA"]},
            "skill": {"config": {
                "product_description": "Gaskets",
                "discovery": {"sources": {"inspections": FEED_SOURCE}},
                "scoring": {"region_bonus": {"max": 10}},
            }},
            "pipeline": PIPELINE,
        }
        mapped = build_tool_context(ctx)
        assert set(feed_sources(mapped["sources"])) == {"inspections"}


# ── Whole runs, offline (`runner_harness`) ──────────────────────────────────────────


class _Resp(io.BytesIO):
    """A response body the stub opener hands back; `read` may be made to fail."""

    def __init__(self, body: bytes, read_error: Exception | None = None) -> None:
        super().__init__(body)
        self.read_error = read_error
        self.read_sizes: list[int | None] = []

    def read(self, size: int | None = -1) -> bytes:
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        return super().read(size)

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class _Opener:
    """Stands in for the runner's one gateway opener: records requests, opens no socket."""

    def __init__(self, *, body: bytes = b"", error: Exception | None = None,
                 read_error: Exception | None = None) -> None:
        self.body, self.error, self.read_error = body, error, read_error
        self.seen: list[Any] = []
        self.responses: list[_Resp] = []

    def open(self, req: Any, timeout: float = 0) -> Any:
        self.seen.append((req, timeout))
        if self.error is not None:
            raise self.error
        resp = _Resp(self.body, self.read_error)
        self.responses.append(resp)
        return resp


def _id_of(name: str) -> str:
    return str(uuid.uuid5(als._NAMESPACE, f"{harness.SCAN_RUN_ID}:{als.normalize_name(name)}"))


class TestAFeedRunKeepsTheTerritory:
    """`rows-in-territory-only`, through the runner: the area it holds rows to is the org's."""

    def test_only_in_territory_rows_are_sent_and_none_is_verified_by_a_model(self, monkeypatch):
        run = harness.run_main(monkeypatch, harness.feed_context(), records=harness.feed_answer())
        assert run.code == 0, run.error_message()
        assert {i["company_name"] for i in run.items_of("prospects")} == IN_TERRITORY
        assert run.provider.count("geography") == 0

    def test_without_zip_discovery_the_markets_alone_are_the_territory(self, monkeypatch):
        run = harness.run_main(
            monkeypatch, harness.feed_context(), records=harness.feed_answer(),
            env={"PHASES": "discovery,contacts,scoring"},
        )
        assert run.provider.count("zip_discovery") == 0
        assert {i["company_name"] for i in run.items_of("prospects")} == IN_TERRITORY - {
            MECHANICSVILLE
        }


class TestAHeldRestaurantIsNotRepeated:
    """`held-restaurant-not-repeated`: what the org holds is dropped BEFORE the ceiling."""

    def test_a_held_restaurant_is_not_sent_again(self, monkeypatch):
        ctx = harness.feed_context(known_companies=[FAN_DISTRICT])
        run = harness.run_main(monkeypatch, ctx, records=harness.feed_answer())
        names = {i["company_name"] for i in run.items_of("prospects")}
        assert names == IN_TERRITORY - {FAN_DISTRICT}

    def test_the_held_one_frees_its_place_under_the_ceiling(self, monkeypatch):
        # The ceiling keeps the lowest ids first, so hold the restaurant whose id is
        # lowest: a filter applied AFTER the ceiling would spend a place on it and send 4.
        held = min(IN_TERRITORY, key=_id_of)
        ctx = harness.feed_context(known_companies=[held.upper()])
        ctx["skill"]["config"]["discovery"]["max_prospects"] = 5
        run = harness.run_main(monkeypatch, ctx, records=harness.feed_answer())
        names = {i["company_name"] for i in run.items_of("prospects")}
        assert held not in names
        assert names == IN_TERRITORY - {held}

    def test_each_branch_of_a_chain_is_its_own_prospect(self, monkeypatch):
        # The gateway made the names distinct; holding one branch does not hide another.
        ctx = harness.feed_context(known_companies=[SUBWAY_BROAD])
        run = harness.run_main(monkeypatch, ctx, records=harness.feed_answer())
        names = {i["company_name"] for i in run.items_of("prospects")}
        assert SUBWAY_FOREST in names
        assert SUBWAY_BROAD not in names


class TestWhatAFeedRunRefuses:
    """Each a named run error, posted as the run's error; nothing else reaches the gateway."""

    def test_no_markets_is_refused_by_the_mapping_before_any_call(self, monkeypatch):
        # `no-territory-refused`, first half: an org with no markets at all is refused by
        # the mapping's own "No resolved markets", which every skill shares, before any
        # feed code runs. The feed code's own refusal is the next test.
        ctx = harness.feed_context(geography={})
        run = harness.run_main(monkeypatch, ctx, records=harness.feed_answer())
        assert run.code == 1
        assert [t for t, _ in run.posted] == ["error"]
        assert "markets" in run.error_message()
        assert run.provider.calls == {}
        assert run.record_requests == []

    def test_a_territory_of_states_alone_is_refused_under_metro_before_the_feed_is_read(
        self, monkeypatch
    ):
        # Market "VA" with zip discovery left out: a state, no city, no ZIP. Under metro
        # nothing can ever be placed in it, so a green run would mean zero prospects.
        env = {"PHASES": "discovery,contacts,scoring"}
        ctx = harness.feed_context(geography={"home_markets": ["VA"]})
        run = harness.run_main(monkeypatch, ctx, records=harness.feed_answer(), env=env)
        assert run.code == 1
        message = run.error_message()
        assert message.startswith("source_records_no_territory:")
        assert "city" in message and "ZIP" in message and "geo_strictness" in message
        assert run.record_requests == []
        assert not run.posted_of("prospects")
        # The same territory under `state` strictness is enough to read the feed.
        ctx = harness.feed_context(geography={"home_markets": ["VA"]},
                                   config_geography={"targeting": {"geo_strictness": "state"}})
        run = harness.run_main(monkeypatch, ctx, records=harness.feed_answer(), env=env)
        assert run.code == 0, run.error_message()
        assert run.record_requests == ["inspections"]
        assert CAROLINA not in {i["company_name"] for i in run.items_of("prospects")}

    def test_markets_that_place_nothing_are_refused_before_the_feed_is_read(self, monkeypatch):
        ctx = harness.feed_context(geography={"home_markets": ["--"]})
        run = harness.run_main(
            monkeypatch, ctx, records=harness.feed_answer(),
            provider=harness.StubProvider({"zip_discovery": "[]"}),
        )
        assert run.code == 1
        assert run.error_message().startswith("source_records_no_territory:")
        assert run.record_requests == []
        assert not run.posted_of("prospects")

    def test_a_feed_skill_authoring_validation_is_refused_before_any_call(self, monkeypatch):
        ctx = harness.feed_context(config_validation={
            "in_market_signals": ["walk-in cooler"],
            "lanes": [{"name": "news", "fields": ["headline"]}],
        })
        run = harness.run_main(monkeypatch, ctx, records=harness.feed_answer())
        assert run.code == 1
        assert [t for t, _ in run.posted] == ["error"]
        assert "`validation`" in run.error_message() and "gateway feed" in run.error_message()
        assert run.provider.calls == {}
        assert run.record_requests == []

    def test_a_skill_mixing_feed_and_searched_sources_is_refused(self, monkeypatch):
        ctx = harness.feed_context()
        ctx["skill"]["config"]["discovery"]["sources"]["restaurants"] = {
            "name_field": "company_name", "fields": ["company_name"],
            "queries": ["restaurants in {market}"],
        }
        run = harness.run_main(monkeypatch, ctx, records=harness.feed_answer())
        assert run.code == 1
        assert [t for t, _ in run.posted] == ["error"]
        assert "mixes gateway-feed sources" in run.error_message()
        assert run.provider.calls == {}
        assert run.record_requests == []

    @pytest.mark.parametrize("gateway_says", [
        "Unknown feed 'health_inspections'",
        "Organization not found",
    ])
    def test_a_gateway_refusal_fails_the_run_with_its_reason(self, monkeypatch, gateway_says):
        from aeo import runner

        # The real fetch, taken before the harness stands its stub in for it; the 404
        # then comes from urllib itself, so the whole path from HTTP to run error is real.
        real_fetch = runner.fetch_source_records

        def _refuse(source: str) -> Any:
            fake = io.BytesIO(json.dumps({"statusCode": 404, "message": gateway_says}).encode())
            error = HTTPError("http://gateway.invalid/x", 404, "Not Found", {}, fake)
            monkeypatch.setattr(runner, "_GATEWAY_OPENER", _Opener(error=error))
            return real_fetch("http://gateway.invalid", "o", "s", source)

        run = harness.run_main(monkeypatch, harness.feed_context(), records=_refuse)
        assert run.code == 1
        message = run.error_message()
        assert message.startswith("source_records_refused:")
        assert "404" in message and gateway_says in message and "inspections" in message
        assert not run.posted_of("prospects")
        assert run.provider.count("contacts") == run.provider.count("judgment") == 0


class TestTheFetch:
    """`fetch_source_records`: the route, the auth and the refusals, with no network."""

    @staticmethod
    def _install(monkeypatch: Any, opener: _Opener) -> _Opener:
        from aeo import runner

        monkeypatch.setattr(runner, "_GATEWAY_OPENER", opener)
        return opener

    def test_it_asks_the_route_for_this_skill_and_source_with_basic_auth(self, monkeypatch):
        from aeo import runner

        monkeypatch.setenv("CI_USER", "scanner")
        monkeypatch.setenv("CI_PASSWORD", "s3cret")
        opener = self._install(monkeypatch, _Opener(body=FIXTURE.read_bytes()))
        answer = runner.fetch_source_records(
            "http://gateway.invalid/", "org-1", "restaurant-gaskets", "inspections"
        )
        assert answer == _answer()
        [(req, timeout)] = opener.seen
        assert req.get_method() == "GET"
        assert req.full_url == (
            "http://gateway.invalid/runtime/organizations/org-1/source-records"
            "?skill=restaurant-gaskets&source=inspections"
        )
        # The same header the context fetch sends.
        assert req.get_header("Authorization") == runner._auth_headers()["Authorization"]
        assert req.get_header("Authorization").startswith("Basic ")
        assert timeout == 30

    def test_an_http_refusal_is_a_named_error_carrying_the_gateways_words(self, monkeypatch):
        from aeo import runner

        body = io.BytesIO(b'{"statusCode":400,"message":"source has no records block"}')
        self._install(monkeypatch, _Opener(error=HTTPError("u", 400, "Bad Request", {}, body)))
        with pytest.raises(SourceRecordsError) as exc:
            runner.fetch_source_records("http://g", "o", "s", "inspections")
        assert exc.value.reason == "source_records_refused"
        assert "HTTP 400" in str(exc.value) and "no records block" in str(exc.value)
        assert "inspections" in str(exc.value)

    def test_a_long_body_from_the_gateway_is_cut_in_the_error(self, monkeypatch):
        from aeo import runner

        body = io.BytesIO(b"x" * 5000)
        self._install(monkeypatch, _Opener(error=HTTPError("u", 404, "Not Found", {}, body)))
        with pytest.raises(SourceRecordsError) as exc:
            runner.fetch_source_records("http://g", "o", "s", "inspections")
        assert "x" * 199 in str(exc.value)
        assert "x" * 201 not in str(exc.value)
        assert len(str(exc.value)) < 500

    def test_a_rejected_event_still_logs_800_characters_of_the_gateways_body(
        self, monkeypatch, capsys
    ):
        # Deliberately unchanged: the event POST's body goes only to the container log,
        # and the gateway's field-level validation detail is what diagnoses a 400 there.
        from aeo import runner

        body = io.BytesIO(b"y" * 5000)
        self._install(monkeypatch, _Opener(error=HTTPError("u", 400, "Bad Request", {}, body)))
        with pytest.raises(HTTPError):
            runner.post_event("http://g", "run", "prospects", {"data": []},
                              tenant_id="t", organization_id="o")
        logged = capsys.readouterr().err
        assert "y" * 800 in logged and "y" * 801 not in logged

    def test_a_long_reason_from_the_gateway_is_cut(self, monkeypatch):
        from aeo import runner

        error = HTTPError("u", 404, "R" * 5000, {}, io.BytesIO(b""))
        self._install(monkeypatch, _Opener(error=error))
        with pytest.raises(SourceRecordsError) as exc:
            runner.fetch_source_records("http://g", "o", "s", "inspections")
        assert len(str(exc.value)) < 500

    @pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
    def test_a_redirect_is_a_named_error_of_its_own(self, monkeypatch, code):
        from aeo import runner

        error = HTTPError("u", code, "Found", {"Location": "http://elsewhere/"}, io.BytesIO(b""))
        self._install(monkeypatch, _Opener(error=error))
        with pytest.raises(SourceRecordsError) as exc:
            runner.fetch_source_records("http://g", "o", "s", "inspections")
        assert exc.value.reason == "source_records_redirected"
        assert str(code) in str(exc.value)

    def test_an_unreachable_gateway_is_a_named_error(self, monkeypatch):
        from aeo import runner

        self._install(monkeypatch, _Opener(error=URLError("connection refused")))
        with pytest.raises(SourceRecordsError) as exc:
            runner.fetch_source_records("http://g", "o", "s", "inspections")
        assert exc.value.reason == "source_records_unreachable"

    @pytest.mark.parametrize("read_error", [
        http.client.IncompleteRead(b"{\"feed\": \"heal"),
        http.client.HTTPException("connection broken mid-body"),
        TimeoutError("timed out"),
    ])
    def test_a_body_cut_short_is_a_named_error(self, monkeypatch, read_error):
        from aeo import runner

        self._install(monkeypatch, _Opener(body=b"{}", read_error=read_error))
        with pytest.raises(SourceRecordsError) as exc:
            runner.fetch_source_records("http://g", "o", "s", "inspections")
        assert exc.value.reason == "source_records_unreachable"

    def test_an_answer_that_is_not_json_is_a_named_error(self, monkeypatch):
        from aeo import runner

        self._install(monkeypatch, _Opener(body=b"<html>gateway error</html>"))
        with pytest.raises(SourceRecordsError) as exc:
            runner.fetch_source_records("http://g", "o", "s", "inspections")
        assert exc.value.reason == "source_records_malformed"

    def test_the_read_is_bounded_and_an_answer_over_the_bound_is_refused(self, monkeypatch):
        from aeo import runner

        monkeypatch.setattr(runner, "_MAX_RECORDS_BYTES", 64)
        at_limit = b'{"feed": "f", "items": []}'.ljust(64)
        opener = self._install(monkeypatch, _Opener(body=at_limit))
        assert runner.fetch_source_records("http://g", "o", "s", "x") == {"feed": "f", "items": []}
        # Never an unbounded read.
        assert opener.responses[0].read_sizes == [65]

        self._install(monkeypatch, _Opener(body=at_limit + b" "))
        with pytest.raises(SourceRecordsError) as exc:
            runner.fetch_source_records("http://g", "o", "s", "x")
        assert exc.value.reason == "source_records_malformed"
        assert "64" in str(exc.value)

    def test_the_real_bound_holds_the_largest_answer_the_contract_allows(self):
        from aeo import runner

        # 500 rows of the fixture's largest row, with room to spare.
        widest = max(_answer()["items"], key=lambda r: len(json.dumps(r)))
        answer = {**_answer(), "items": [widest] * MAX_ROWS}
        assert len(json.dumps(answer).encode()) * 2 < runner._MAX_RECORDS_BYTES
