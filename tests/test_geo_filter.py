"""Tests for geographic enforcement. Ours.

Motivated by a live observation, not a hypothetical: zip discovery expanded Austin and
Round Rock, the queries carried "…near 78701, Austin, TX", and the model returned
Dallas firms. Nothing noticed. A scan that ignores geography looks like one that
worked.
"""

from __future__ import annotations

from aeo.phases.geo_filter import (
    IN_AREA,
    OUT_OF_AREA,
    UNKNOWN,
    build_target_area,
    classify_prospect,
    geographic_verdicts,
)

ZIPS = [
    {"zip_code": "78701", "city": "Austin", "state": "TX"},
    {"zip_code": "78664", "city": "Round Rock", "state": "TX"},
]


class TestBuildTargetArea:
    def test_collects_zips_cities_and_states_from_zip_rows(self):
        area = build_target_area(ZIPS, None)
        assert area.zips == {"78701", "78664"}
        assert area.cities == {"austin", "round rock"}
        assert area.states == {"TX"}

    def test_also_parses_raw_market_strings(self):
        # Needed when Phase 0 is skipped or returned nothing.
        area = build_target_area(None, ["Austin, TX", "78701, Austin, TX"])
        assert "TX" in area.states
        assert "austin" in area.cities
        assert "78701" in area.zips

    def test_no_geography_is_empty_and_enforcement_must_skip(self):
        # An empty area would classify everything out-of-area and reject the whole
        # scan — worse than not enforcing.
        assert build_target_area(None, None).is_empty is True
        assert build_target_area([], []).is_empty is True


class TestClassify:
    AREA = build_target_area(ZIPS, None)

    def test_matching_zip_is_in_area(self):
        assert classify_prospect({"zip_code": "78701"}, self.AREA) == IN_AREA

    def test_matching_city_is_in_area(self):
        assert classify_prospect({"city": "Austin", "state": "TX"}, self.AREA) == IN_AREA

    def test_THE_OBSERVED_DRIFT_IS_CAUGHT(self):
        # The exact live failure: Austin/Round Rock zips searched, DALLAS firms
        # returned — both in Texas. A state-level boundary lets this through, which
        # is why `metro` is the default. If this test ever passes with IN_AREA, the
        # enforcement has stopped fixing the bug it was written for.
        assert classify_prospect({"city": "Dallas", "state": "TX"}, self.AREA) == OUT_OF_AREA

    def test_state_strictness_deliberately_allows_it(self):
        # The looser reading, for verticals where a distant firm serving the market
        # is a genuine prospect. Opt-in, never the default.
        assert (
            classify_prospect({"city": "Dallas", "state": "TX"}, self.AREA, strictness="state")
            == IN_AREA
        )

    def test_a_neighbouring_town_is_rejected_under_metro_and_that_is_the_tradeoff(self):
        # Stated rather than hidden: strict enforcement costs legitimate suburbs that
        # Phase 0 did not surface. The cost is bounded by Phase 0 returning up to 15
        # zips per market, so the city set covers a metro rather than a centre.
        assert classify_prospect({"city": "Pflugerville", "state": "TX"}, self.AREA) == OUT_OF_AREA
        assert (
            classify_prospect({"city": "Pflugerville", "state": "TX"}, self.AREA, strictness="state")
            == IN_AREA
        )

    def test_a_different_state_is_out_of_area(self):
        assert classify_prospect({"city": "Denver", "state": "CO"}, self.AREA) == OUT_OF_AREA

    def test_nothing_to_judge_on_is_unknown_not_rejected(self):
        # Same rule as validation: unverifiable must not mean rejected, or sparse
        # discovery rows silently shrink every result set.
        assert classify_prospect({}, self.AREA) == UNKNOWN
        assert classify_prospect({"company_name": "No Location Co"}, self.AREA) == UNKNOWN

    def test_an_empty_area_judges_nothing(self):
        empty = build_target_area(None, None)
        assert classify_prospect({"city": "Dallas", "state": "CA"}, empty) == UNKNOWN

    def test_finds_a_zip_inside_an_address_string(self):
        assert classify_prospect({"address": "100 Main St, 78664"}, self.AREA) == IN_AREA


class TestVerdicts:
    AREA = build_target_area(ZIPS, None)

    def test_emits_a_rejection_only_for_out_of_area_prospects(self):
        prospects = [
            {"id": "in", "city": "Austin", "state": "TX"},
            {"id": "out", "city": "Denver", "state": "CO"},
            {"id": "unk", "company_name": "Nowhere"},
        ]
        verdicts = geographic_verdicts(prospects, self.AREA)
        assert [v["prospect_id"] for v in verdicts] == ["out"]
        assert verdicts[0]["validation_data"]["validated"] is False

    def test_the_reason_names_where_it_was_and_what_was_asked(self):
        # An operator reading the row should not have to guess why it was rejected.
        verdicts = geographic_verdicts([{"id": "o", "city": "Denver", "state": "CO"}], self.AREA)
        reason = verdicts[0]["validation_data"]["reasoning"]
        assert "Denver" in reason
        assert "outside" in reason.lower()

    def test_flags_the_geographic_disqualifier_explicitly(self):
        verdicts = geographic_verdicts([{"id": "o", "city": "Denver", "state": "CO"}], self.AREA)
        assert verdicts[0]["validation_data"]["disqualifiers_hit"] == [
            "outside the target geography"
        ]

    def test_no_verdicts_when_the_area_is_unknown(self):
        empty = build_target_area(None, None)
        assert geographic_verdicts([{"id": "o", "city": "Denver", "state": "CO"}], empty) == []

    def test_skips_prospects_with_no_id(self):
        assert geographic_verdicts([{"city": "Denver", "state": "CO"}], self.AREA) == []
