"""Phase 2b planning — what gets written, and the three things that refuse to write.

🔴 **The refusals are the point of this file.** A re-score is the one operation that
changes numbers a customer has already seen, so the failure mode is not "it errors" but
"it confidently rewrites 306 leads into the nurture lane and the operator sees 306 changed
numbers rather than a message".
"""
from datetime import date

import pytest

from aeo.rescore import RescoreRefused, can_gate, plan_rescore, summarise
from tests.test_gated_score import ALIASES, CFG

TODAY = date(2026, 8, 27)


def row(name, *, score=40, state="NC", stage="4 - Active Pursuit",
        signal_date="2026-08-01", signal_type="rfp activity", employees="250"):
    return {
        "id": f"id-{name}",
        "company_name": name,
        "score": score,
        "state": state,
        "pipeline_status": stage,
        "contact_name": "A",
        "contact_email": "a@b.c",
        "validation_data": {
            "switching_signal": (
                [{"signal_type": signal_type, "signal_date": signal_date}]
                if signal_date
                else []
            )
        },
        "discovery_data": {"by_source": {"s": {"employee_count": employees}}},
    }


class TestItRefusesRatherThanRewrites:
    def test_refuses_a_skill_that_never_opted_in(self):
        cfg = {**CFG, "model": "legacy"}
        with pytest.raises(RescoreRefused, match="not configured for the gated model"):
            plan_rescore([row("A")], cfg, ALIASES, TODAY)

    def test_refuses_a_vertical_whose_rows_carry_NO_dated_signals(self):
        # 🔴 The four non-health verticals: 550 scored prospects carrying `signals_found`,
        # plain strings with no dates. Re-scoring them would fail every lead at G2 and cap
        # it at the nurture ceiling — a silent, total, confident regression.
        rows = [row("A", signal_date=None), row("B", signal_date=None)]
        with pytest.raises(RescoreRefused, match="dated signal"):
            plan_rescore(rows, CFG, ALIASES, TODAY)

    def test_the_gate_check_reads_the_DATA_not_the_config(self):
        # A config can declare a perfect gate over a field its vertical never produces,
        # which is exactly the state of those four skills.
        assert can_gate(CFG, [row("A")], "switching_signal") is True
        assert can_gate(CFG, [row("A", signal_date=None)], "switching_signal") is False

    def test_a_score_in_the_forbidden_band_refuses_the_WHOLE_batch(self):
        # One impossible score is proof of a defect in the model. Writing even one
        # destroys the invariant that makes it auditable by a single query, so the batch
        # dies rather than the row being skipped.
        bad = {**CFG, "floor": 60, "partial": {"target_market_only": {"base": 50, "ceiling": 70}}}
        with pytest.raises(RescoreRefused, match="structurally-empty band"):
            plan_rescore([row("A", stage="7 - Too Late")], bad, ALIASES, TODAY)


class TestThePlan:
    def test_it_reports_old_and_new_together(self):
        [p] = plan_rescore([row("Acme", score=40)], CFG, ALIASES, TODAY)
        assert p["old_score"] == 40
        assert p["score"] >= 80
        assert p["lane"] == "qualified"
        assert p["company_name"] == "Acme"

    def test_it_reads_employee_count_out_of_discovery_data(self):
        # 🔑 `employee_count` is not a column. Without the flatten every prospect scores
        # the unknown midpoint, silently costing or granting 2 points on every lead.
        big = plan_rescore([row("Big", employees="500")], CFG, ALIASES, TODAY)[0]
        small = plan_rescore([row("Small", employees="10")], CFG, ALIASES, TODAY)[0]
        assert big["breakdown"]["bands"]["company_size"] == 4
        assert small["breakdown"]["bands"]["company_size"] == 0
        assert big["score"] > small["score"]

    def test_today_is_injected_so_a_rescore_is_reproducible(self):
        # Scored as of the RUN's date, not the date someone happened to re-score. Using
        # "now" would age every signal by however long the row has been sitting there and
        # silently gate out leads that were fresh when found.
        r = row("A", signal_date="2025-06-01")
        early = plan_rescore([r], CFG, ALIASES, date(2025, 7, 1))[0]
        late = plan_rescore([r], CFG, ALIASES, date(2027, 7, 1))[0]
        assert early["breakdown"]["gates"]["buying_window"] is True
        assert late["breakdown"]["gates"]["buying_window"] is False
        assert early["score"] != late["score"]

    def test_it_performs_no_discovery(self):
        # Nothing in the module may reach the network. Asserted structurally: the plan is
        # computed from the row alone, so a row with no extra fields still scores.
        bare = {
            "id": "x", "company_name": "X", "score": 1, "state": "NC",
            "pipeline_status": "4 - Active Pursuit",
            "validation_data": {"switching_signal": [
                {"signal_type": "rfp activity", "signal_date": "2026-08-01"}]},
        }
        [p] = plan_rescore([bare], CFG, ALIASES, TODAY)
        assert p["score"] >= 80


class TestTheSummary:
    def test_it_says_what_moved_before_anything_is_written(self):
        plans = plan_rescore(
            [row("A", score=40), row("B", score=52), row("C", score=38)],
            CFG, ALIASES, TODAY,
        )
        s = summarise(plans)
        assert s["count"] == 3
        assert s["moved"] == 3
        assert s["qualified"] == 3
        assert s["in_forbidden_band"] == 0
        assert s["biggest_gain"] > 0

    def test_an_empty_batch_summarises_without_exploding(self):
        assert summarise([]) == {"count": 0}
