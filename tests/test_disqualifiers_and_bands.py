"""Hard disqualifier rules, priority bands, and that all three reach the wire.

Phase 4 of the EAP-parity work. Two facts were being conflated:

* **"Scored below the operator's cutoff"** — a ranking statement. `disqualify_below`
  already expressed it, as a flag rather than a filter, because the operator picks that
  threshold before ever seeing a score distribution.
* **"Ineligible regardless of score"** — too small, wrong country, a multi-year contract
  signed with someone else last month. No threshold on a score can express this, because
  a disqualified prospect may still score well on every other axis.

Priority bands exist for the same reason a score is not a verdict: a number ranks, a band
says what to do. Without one, every consumer re-derives its own thresholds and the same 62
reads as two different verdicts on two screens.

⚠️ The wire test at the bottom is not redundant with the parity check in
`test_event_mapping.py`. That check compares produced fields against the whitelist, and it
already had `disqualified` in its exclusion list under a FALSE justification ("carried on
the validations event") — on a fixture that never produced the field. So it passed while
the flag was computed on every real run and dropped before the wire. A named test that
asserts the value arrives is the only thing that catches that.
"""
from __future__ import annotations

from datetime import date

import av_lead_scanner as als
from aeo.event_mapping import SCORED_PASSTHROUGH, map_scored_event

TODAY = date(2026, 7, 20)

#: Three industry-neutral rules, standing in for "too small", "wrong country", and
#: "already committed elsewhere".
RULES = [
    {"key": "too_small", "source_field": "employee_count", "below": 100,
     "reason": "Below the size floor for a standalone program"},
    {"key": "outside_market", "source_field": "country",
     "required_keywords": ["united states", "usa", "us"],
     "reason": "Located outside the served market"},
    {"key": "locked_in", "source_field": "incumbent_type",
     "keywords": ["standalone-recent"],
     "reason": "Multi-year contract awarded within the last 18 months"},
]

BANDS = [
    {"range": [80, 100], "label": "Critical", "action": "Call today"},
    {"range": [60, 79], "label": "High", "action": "Call this week"},
    {"range": [40, 59], "label": "Medium", "action": "Research"},
    {"range": [20, 39], "label": "Low", "action": "Nurture"},
    {"range": [0, 19], "label": "Skip", "action": "Do not pursue"},
]


class TestHardDisqualifierRules:
    def test_a_numeric_floor_fires(self):
        assert als.hard_disqualifier({"employee_count": "40"}, RULES) == (
            "Below the size floor for a standalone program"
        )

    def test_a_value_above_the_floor_does_not_fire(self):
        assert als.hard_disqualifier({"employee_count": "850"}, RULES) is None

    def test_required_keywords_fire_when_none_are_present(self):
        assert als.hard_disqualifier(
            {"employee_count": "500", "country": "Canada"}, RULES
        ) == "Located outside the served market"

    def test_required_keywords_do_not_fire_when_one_is_present(self):
        assert als.hard_disqualifier(
            {"employee_count": "500", "country": "United States"}, RULES
        ) is None

    def test_keywords_fire_on_a_match(self):
        assert als.hard_disqualifier(
            {"employee_count": "500", "country": "USA", "incumbent_type": "standalone-recent"},
            RULES,
        ) == "Multi-year contract awarded within the last 18 months"

    def test_a_rule_never_fires_on_a_field_the_prospect_does_not_carry(self):
        """Absence of evidence is not grounds for exclusion.

        A prospect with no `country` collected is not "outside the served market" — that
        would silently delete every prospect whose country the sources did not return.
        """
        assert als.hard_disqualifier({"employee_count": "500"}, RULES) is None
        assert als.hard_disqualifier({"country": "   "}, RULES) is None

    def test_an_above_bound_fires(self):
        rule = [{"key": "too_big", "source_field": "employee_count", "above": 50_000,
                 "reason": "National-account territory"}]
        assert als.hard_disqualifier({"employee_count": "80000"}, rule) == (
            "National-account territory"
        )

    def test_the_first_matching_rule_wins(self):
        lead = {"employee_count": "40", "country": "Canada"}
        assert als.hard_disqualifier(lead, RULES) == (
            "Below the size floor for a standalone program"
        )

    def test_a_rule_with_no_reason_falls_back_to_its_key(self):
        rule = [{"key": "too_small", "source_field": "n", "below": 10}]
        assert als.hard_disqualifier({"n": "5"}, rule) == "too_small"

    def test_no_rules_disqualifies_nothing(self):
        for rules in (None, [], "not-a-list", [None, 7]):
            assert als.hard_disqualifier({"employee_count": "1"}, rules) is None

    def test_a_malformed_bound_is_skipped_not_fatal(self):
        rule = [{"key": "k", "source_field": "n", "below": "not-a-number"}]
        assert als.hard_disqualifier({"n": "5"}, rule) is None


class TestPriorityBands:
    def test_each_score_lands_in_its_authored_band(self):
        assert als.priority_band(84, BANDS) == "Critical"
        assert als.priority_band(69, BANDS) == "High"
        assert als.priority_band(52, BANDS) == "Medium"
        assert als.priority_band(25, BANDS) == "Low"
        assert als.priority_band(0, BANDS) == "Skip"

    def test_the_boundaries_belong_to_the_band_that_declares_them(self):
        assert als.priority_band(80, BANDS) == "Critical"
        assert als.priority_band(79, BANDS) == "High"
        assert als.priority_band(100, BANDS) == "Critical"

    def test_min_max_is_accepted_as_well_as_range(self):
        bands = [{"min": 50, "max": 100, "label": "Good"}, {"min": 0, "max": 49, "label": "Poor"}]
        assert als.priority_band(75, bands) == "Good"
        assert als.priority_band(10, bands) == "Poor"

    def test_no_bands_means_no_band(self):
        assert als.priority_band(75, None) is None
        assert als.priority_band(75, []) is None


class TestBandCoverageAdvisory:
    def test_contiguous_coverage_is_clean(self):
        assert als.band_coverage_gap(BANDS, 100) is None

    def test_a_hole_in_the_middle_is_reported(self):
        bands = [{"range": [60, 100], "label": "High"}, {"range": [0, 39], "label": "Low"}]
        assert als.band_coverage_gap(bands, 100) == "scores 40-59 have no band"

    def test_a_missing_top_is_reported(self):
        bands = [{"range": [0, 79], "label": "Low"}]
        assert als.band_coverage_gap(bands, 100) == "scores 80-100 have no band"

    def test_a_missing_bottom_is_reported(self):
        bands = [{"range": [20, 100], "label": "High"}]
        assert als.band_coverage_gap(bands, 100) == "scores 0-19 have no band"

    def test_an_overlap_is_reported(self):
        bands = [{"range": [0, 59], "label": "Low"}, {"range": [50, 100], "label": "High"}]
        assert "overlaps" in (als.band_coverage_gap(bands, 100) or "")

    def test_no_bands_is_not_a_gap(self):
        assert als.band_coverage_gap(None, 100) is None


class TestThroughScoreProspects:
    SCORING = {
        "score_cap": 100,
        "factors_max": 100,
        "completeness": {"max": 0},
        "fit": {"max": 0},
        "region_bonus": {"max": 0},
        "multi_source": {"max": 0},
        "pipeline": {"max": 0},
        "disqualify_rules": RULES,
        "priority_bands": BANDS,
        "factors": [
            {"key": "size", "source_field": "employee_count", "weight": 1,
             "tiers": [{"threshold": 500, "points": 10}, {"threshold": 0, "points": 5}]},
        ],
    }

    def _score(self, prospect):
        return als.score_prospects([prospect], {"scoring": self.SCORING}, today=TODAY)[0]

    def test_a_disqualified_prospect_is_zeroed_and_banded_skip_with_a_reason(self):
        got = self._score({"id": "p1", "company_name": "Tiny Co", "employee_count": "40",
                           "country": "USA"})
        assert got["score"] == 0
        assert got["disqualified"] is True
        assert got["disqualifier_reason"] == "Below the size floor for a standalone program"
        assert got["priority_band"] == "Skip"

    def test_a_qualified_prospect_scores_and_bands_normally(self):
        got = self._score({"id": "p2", "company_name": "Big Co", "employee_count": "850",
                           "country": "United States"})
        assert got["score"] == 100
        assert got["priority_band"] == "Critical"
        assert "disqualifier_reason" not in got

    def test_a_hard_rule_outranks_a_generous_score_floor(self):
        """A rule is about eligibility; the floor is about rank. The rule must win."""
        scoring = {**self.SCORING, "disqualify_below": 0}
        got = als.score_prospects(
            [{"id": "p3", "employee_count": "40", "country": "USA"}],
            {"scoring": scoring}, today=TODAY,
        )[0]
        # `disqualify_below: 0` alone would report False (0 < 0 is false).
        assert got["disqualified"] is True

    def test_the_score_floor_alone_flags_without_a_reason(self):
        scoring = {**self.SCORING, "disqualify_below": 90}
        del scoring["disqualify_rules"]
        got = als.score_prospects(
            [{"id": "p4", "employee_count": "100"}], {"scoring": scoring}, today=TODAY
        )[0]
        assert got["disqualified"] is True          # 50 < 90
        assert "disqualifier_reason" not in got      # nothing ruled it ineligible

    def test_the_clamp_is_applied_after_the_ai_adjustment_so_the_top_band_is_reachable(self):
        """A base of 95 plus +15 must land at the cap, not off the end of the table."""
        got = als.score_prospects(
            [{"id": "p5", "employee_count": "850", "country": "USA",
              "ai_score_adjustment": 15}],
            {"scoring": self.SCORING}, today=TODAY,
        )[0]
        assert got["score"] == 100
        assert got["priority_band"] == "Critical"


class TestTheseReachTheWire:
    """The named test the parity check cannot replace — see this module's docstring."""

    def test_all_three_fields_are_whitelisted(self):
        for field in ("disqualified", "disqualifier_reason", "priority_band"):
            assert field in SCORED_PASSTHROUGH, (
                f"{field} is computed and would be dropped one line before the wire — "
                "the fifth occurrence of this exact omission."
            )

    def test_disqualification_and_band_reach_the_wire(self):
        scored = als.score_prospects(
            [{"id": "p1", "company_name": "Tiny Co", "employee_count": "40", "country": "USA"}],
            {
                "scoring": {
                    "score_cap": 100,
                    "disqualify_rules": RULES,
                    "priority_bands": BANDS,
                    "disqualify_below": 40,
                },
                "sources": {},
            },
            today=TODAY,
        )
        payloads = map_scored_event({"type": "scored", "items": scored})
        items = [i for p in payloads for i in p["data"]]
        assert items, "nothing reached the wire"
        item = items[0]
        assert item["disqualified"] is True
        assert item["disqualifier_reason"] == "Below the size floor for a standalone program"
        assert item["priority_band"] == "Skip"
