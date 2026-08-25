"""Dated-event factors: a recency window and a bonus keyword set.

Phase 3 of the EAP-parity work. This is the one factor shape the other modes genuinely
cannot express, for two reasons that compound:

* Some events are worth more than others — a live procurement notice against a routine
  renewal — which `keywords` alone would handle.
* An old event is worth **nothing**, however strong it was. That is the part no other mode
  can do, and getting it wrong is not a small error: a stale notice scoring full points
  puts a dead prospect at the top of an operator's list.

The strict date parser is the load-bearing piece. The engine's existing
`parse_estimated_date` ends with a bare-year fallback that INVENTS a month, which for
recency does not degrade the answer, it changes it.
"""
from __future__ import annotations

from datetime import date

import av_lead_scanner as als

#: The scan date of the run whose published output these numbers come from.
SCAN_DATE = date(2026, 7, 20)

#: A procurement-notice bonus set, and a 12-month window.
SIGNAL_FACTOR = {
    "key": "in_market_signals",
    "weight": 25,
    "source_field": "signals",
    "base_points": 10,
    "bonus_points": 15,
    "bonus_keywords": ["rfp", "rfq", "solicitation", "procurement", "invitation to bid"],
    "date_field": "signal_date",
    "recency_months": 12,
}


def _credit(rows, spec=None, today=SCAN_DATE) -> float:
    return als._event_signal_credit(rows, spec or SIGNAL_FACTOR, today=today)


class TestStrictDateParser:
    def test_refuses_a_bare_year(self):
        """The whole reason this parser exists.

        `parse_estimated_date` turns "2019" into 2019-06-01 by inventing a month, which
        would silently place a signal inside or outside a trailing window.
        """
        assert als.parse_signal_date("2019") is None
        assert als.parse_signal_date("sometime in 2026") is None
        # The contrast with the estimating parser, on a year inside its accepted range:
        # it answers with an INVENTED June.
        assert als.parse_estimated_date("2026") == date(2026, 6, 1)
        assert als.parse_signal_date("2026") is None

    def test_parses_the_forms_that_appear_in_collected_data(self):
        assert als.parse_signal_date("2026-07-30") == date(2026, 7, 30)
        assert als.parse_signal_date("2026-07") == date(2026, 7, 1)
        assert als.parse_signal_date("02/14/2024") == date(2024, 2, 14)
        assert als.parse_signal_date("August 2026") == date(2026, 8, 1)
        assert als.parse_signal_date("April 27, 2023") == date(2023, 4, 27)
        assert als.parse_signal_date("27 April 2023") == date(2023, 4, 27)
        assert als.parse_signal_date("Aug. 2026") == date(2026, 8, 1)

    def test_reads_a_date_out_of_surrounding_prose(self):
        assert als.parse_signal_date("RFP posted 2026-03-04, closes soon") == date(2026, 3, 4)

    def test_blank_and_nonsense_are_none_not_an_error(self):
        for value in (None, "", "   ", "next quarter", "TBD", 12345):
            assert als.parse_signal_date(value) is None

    def test_an_impossible_date_is_refused(self):
        assert als.parse_signal_date("2026-13-01") is None
        assert als.parse_signal_date("2026-00-05") is None


class TestRecencyWindow:
    def test_a_current_signal_earns_the_base(self):
        rows = [{"signal_type": "HR Leadership Posting", "signal_date": "2026-07-08"}]
        assert _credit(rows) == 10 / 25

    def test_a_stale_signal_earns_nothing_at_all(self):
        """16 months before the scan date, against a 12-month window."""
        rows = [{"signal_type": "RFP/RFQ", "signal_date": "2025-03-13"}]
        assert _credit(rows) == 0.0

    def test_a_stale_signal_does_not_earn_the_bonus_either(self):
        """The failure mode that matters: a dead procurement notice topping the list."""
        rows = [{"signal_type": "RFP posted", "signal_date": "2024-05-07"}]
        assert _credit(rows) == 0.0

    def test_the_freshest_surviving_row_wins_and_stale_rows_are_dropped(self):
        rows = [
            {"signal_type": "RFP/RFQ", "signal_date": "2024-05-07"},          # stale
            {"signal_type": "Benefits Renewal", "signal_date": "2025-08-04"},  # current
        ]
        assert _credit(rows) == 10 / 25

    def test_a_future_date_is_current_not_out_of_window(self):
        """A scheduled renewal is a timing signal, not an expired one."""
        rows = [{"signal_type": "Benefits Renewal", "signal_date": "2026-11-01"}]
        assert _credit(rows) == 10 / 25

    def test_an_undated_row_earns_the_base_but_never_the_bonus(self):
        """Missing evidence must not shrink the result set, nor assert freshness."""
        assert _credit([{"signal_type": "Wellbeing Announcement"}]) == 10 / 25
        assert _credit([{"signal_type": "RFP issued"}]) == 10 / 25

    def test_a_bare_year_is_treated_as_undated_rather_than_placed_in_the_window(self):
        assert _credit([{"signal_type": "RFP issued", "signal_date": "2019"}]) == 10 / 25


class TestBonusKeywords:
    def test_a_current_procurement_notice_earns_base_plus_bonus(self):
        rows = [{"signal_type": "EAP RFP issued", "signal_date": "2026-05-01"}]
        assert _credit(rows) == 1.0

    def test_the_bonus_is_matched_anywhere_in_the_row(self):
        rows = [{"signal_type": "Notice", "detail": "invitation to bid on benefits",
                 "signal_date": "2026-05-01"}]
        assert _credit(rows) == 1.0

    def test_a_non_bonus_current_signal_earns_only_the_base(self):
        rows = [{"signal_type": "Wellbeing Announcement", "signal_date": "2026-04-04"}]
        assert _credit(rows) == 10 / 25

    def test_the_best_row_wins_and_signals_do_not_stack(self):
        """Two current events are not twice as in-market as one.

        ⚠️ **Row ORDER is deliberate and load-bearing in this test.** An earlier version
        listed the bonus-earning row LAST, which a mutation check exposed as vacuous: with
        the `max` removed entirely — last row wins — it still passed, because the best row
        happened to be last. The strongest row now comes FIRST in one case and in the
        MIDDLE in another, so any "take one particular row" implementation fails.
        """
        best_first = [
            {"signal_type": "RFQ published", "signal_date": "2026-06-02"},
            {"signal_type": "Benefits Renewal", "signal_date": "2026-05-11"},
        ]
        assert _credit(best_first) == 1.0

        best_in_the_middle = [
            {"signal_type": "Benefits Renewal", "signal_date": "2026-05-11"},
            {"signal_type": "RFQ published", "signal_date": "2026-06-02"},
            {"signal_type": "Wellbeing Announcement", "signal_date": "2026-04-04"},
        ]
        assert _credit(best_in_the_middle) == 1.0

        # And it is a MAX, not a sum: three current signals do not exceed one.
        assert _credit(best_in_the_middle) == _credit([best_first[0]])

    def test_with_no_recency_gate_the_bonus_is_unconditional(self):
        spec = {**SIGNAL_FACTOR}
        spec.pop("date_field")
        spec.pop("recency_months")
        assert _credit([{"signal_type": "RFP issued"}], spec) == 1.0


class TestShapesAndDegenerateTables:
    def test_a_single_dict_is_one_row(self):
        assert _credit({"signal_type": "RFP", "signal_date": "2026-05-01"}) == 1.0

    def test_a_bare_string_is_one_undated_row(self):
        assert _credit("RFP issued") == 10 / 25

    def test_no_rows_earns_nothing(self):
        assert _credit([]) == 0.0
        assert _credit(None) == 0.0

    def test_a_zero_budget_factor_earns_nothing_rather_than_dividing_by_zero(self):
        spec = {**SIGNAL_FACTOR, "base_points": 0, "bonus_points": 0}
        assert _credit([{"signal_type": "RFP", "signal_date": "2026-05-01"}], spec) == 0.0

    def test_base_only_with_no_bonus_configured(self):
        spec = {"key": "s", "source_field": "signals", "base_points": 10,
                "date_field": "signal_date", "recency_months": 12}
        assert _credit([{"signal_type": "x", "signal_date": "2026-05-01"}], spec) == 1.0


class TestThroughScoreProspects:
    """End to end, because a factor that scores right and reports wrong is still broken."""

    SCORING = {
        "score_cap": 100,
        "factors_max": 100,
        "completeness": {"max": 0},
        "fit": {"max": 0},
        "region_bonus": {"max": 0},
        "multi_source": {"max": 0},
        "pipeline": {"max": 0},
        "factors": [SIGNAL_FACTOR],
    }

    def _score(self, signals):
        out = als.score_prospects(
            [{"organization_name": "X", "signals": signals}],
            {"scoring": self.SCORING},
            today=SCAN_DATE,
        )
        return out[0]

    def test_recency_is_evaluated_against_the_runs_own_scan_date(self):
        current = self._score([{"signal_type": "Renewal", "signal_date": "2026-05-11"}])
        stale = self._score([{"signal_type": "Renewal", "signal_date": "2024-05-11"}])
        assert current["score"] == 40      # 10 of 25, scaled to the 100-point axis
        assert stale["score"] == 0
        assert current["score_factors"]["factors"]["in_market_signals"] == 0.4
        assert stale["score_factors"]["factors"]["in_market_signals"] == 0.0

    def test_a_current_procurement_notice_reaches_full_credit(self):
        got = self._score([{"signal_type": "EAP RFP", "signal_date": "2026-06-01"}])
        assert got["score"] == 100
        assert got["score_factors"]["factors"]["in_market_signals"] == 1.0
