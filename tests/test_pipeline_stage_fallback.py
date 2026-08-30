"""The WIRED stage fallback — `av_lead_scanner._pipeline_from_stage_resolver`.

🔴 **This file exists because its absence let a NameError ship green.** `tests/` had 662
passing tests and not one of them called this function, so an unresolved import inside it
was invisible: the module imported, every other test passed, and the only thing that
caught it was running the function by hand. A unit-tested module reached through an
untested call site is an untested feature.

`test_customer_stage.py` covers the resolver's rules. This covers the wiring: the tier
order, the two fall-throughs, the score lookup, and the evidence field the frontend needs.
"""
from datetime import date

import pytest

import av_lead_scanner as als

TODAY = date(2026, 8, 27)

LADDER = [
    {"key": "1 - Early Discovery", "min_months": 18, "max_months": 999, "kind": "timing"},
    {"key": "4 - Active Pursuit", "min_months": 4, "max_months": 8, "kind": "timing"},
    {"key": "5 - Decision Imminent", "min_months": 0, "max_months": 4, "kind": "timing"},
    {"key": "7 - Too Late", "min_months": -999, "max_months": -4, "kind": "timing"},
]
CTX = {"pipeline": {"stages": LADDER}, "skill_type": "customer"}


def cfg():
    """The engine config MYgroup actually has: a bare budget, so the default `statuses`
    table is inherited. That inheritance is defect D3 and is deliberately not touched
    here — this test pins the fallback, not the budget."""
    return als._deep_get({"pipeline": {"max": 30}}, "pipeline")


def lead(dates=None, **contact):
    by_source = {"s": {"trigger_date": d} for d in ([dates] if dates else [])}
    return {"discovery_data": {"by_source": by_source} if dates else {}, **contact}


class TestTierOrder:
    def test_a_judged_prospect_never_reaches_the_resolver(self):
        # Tier 1 wins. The judge read the event AND its type; this resolver only has a
        # date, so overriding a verdict with it would be a downgrade.
        p = {"_ai_judgment": {"pipeline_status": "4 - Active Pursuit"}}
        judged = als._pipeline_from_judgment(p, cfg())
        assert judged and judged["pipeline_status"] == "4 - Active Pursuit"
        assert judged["pipeline_source"] == "ai"

    def test_a_project_skill_falls_through_to_the_date_ladder(self):
        # AEO keeps a project skill's engine-derived stage, so re-deriving here would
        # change a stage AEO would have accepted.
        got = als._pipeline_from_stage_resolver(
            lead("2026-08-12"), {**CTX, "skill_type": "project"}, cfg(), TODAY
        )
        assert got is None

    def test_an_absent_skill_type_is_treated_as_customer(self):
        # Every live skill is `customer`, and an older gateway sends no type at all.
        got = als._pipeline_from_stage_resolver(
            lead("2026-08-12"), {"pipeline": {"stages": LADDER}}, cfg(), TODAY
        )
        assert got is not None

    def test_no_vocabulary_falls_through(self):
        assert (
            als._pipeline_from_stage_resolver(
                lead("2026-08-12"), {"skill_type": "customer"}, cfg(), TODAY
            )
            is None
        )


class TestEvidence:
    def test_a_banded_date_is_reported_as_evidence(self):
        # 🔑 The discriminator aeo-frontend needs. Their `derived` chip reads "Placed by
        # measuring how long ago the event was" — true only when a date decided it.
        got = als._pipeline_from_stage_resolver(
            lead("2026-09-10"), CTX, cfg(), TODAY
        )
        assert got["pipeline_status"] == "5 - Decision Imminent"
        assert got["signal_date"] == "2026-09-10"
        assert "dated discovery signal" in got["pipeline_detail"]

    def test_resting_reports_NO_date_and_says_so(self):
        # MYgroup's real shape: `discovery_data` carries no timing fields at all, so
        # nothing is measured and the prospect rests. Claiming a measurement here is the
        # over-claim this field exists to prevent.
        got = als._pipeline_from_stage_resolver(lead(), CTX, cfg(), TODAY)
        assert got["pipeline_status"] == "1 - Early Discovery"
        assert "signal_date" not in got
        assert "Nothing was measured" in got["pipeline_detail"]

    def test_an_unparseable_date_rests_rather_than_claiming_one(self):
        # A bare year is refused by the parser, so this is the no-date path even though
        # discovery_data holds a value.
        got = als._pipeline_from_stage_resolver(lead("2026"), CTX, cfg(), TODAY)
        assert "signal_date" not in got
        assert got["pipeline_status"] == "1 - Early Discovery"


class TestProvenanceAndScore:
    def test_never_claims_ai_provenance(self):
        # Absent `pipeline_source` == derived. Marking this `ai` would tell an operator a
        # stage was judged when it was computed, and `derived` is the flag that marks
        # rows worth re-running — so the mislabel would also hide them.
        got = als._pipeline_from_stage_resolver(lead("2026-09-10"), CTX, cfg(), TODAY)
        assert "pipeline_source" not in got

    def test_the_score_comes_from_the_rung_it_resolved(self):
        # The D5 repair, asserted: the axis must follow the stage, not a second
        # computation. On run 741b7b3b groninger displayed Early Discovery while
        # carrying the 2-point Too Late weight.
        got = als._pipeline_from_stage_resolver(lead(), CTX, cfg(), TODAY)
        assert got["pipeline_status"] == "1 - Early Discovery"
        assert got["score"] == 20
        assert got["score"] != 2

    def test_the_score_is_clamped_to_the_axis_budget(self):
        tight = {**cfg(), "max": 5}
        got = als._pipeline_from_stage_resolver(lead(), CTX, tight, TODAY)
        assert got["score"] == 5

    @pytest.mark.parametrize(
        "field", ["contact_email", "contact_phone", "contact_linkedin"]
    )
    def test_reachability_uses_the_same_three_fields_as_the_gateway(self, field):
        # NOT contact_name — a name is not a way to reach anyone, and the gateway's
        # `loadStageSignals` used exactly these three. A contact-gated rung is the only
        # thing this changes, and the shared ladder declares none, so nothing today
        # would catch a divergence.
        gated = [{"key": "reachable", "requires_contact": True}, {"key": "rest"}]
        ctx = {"pipeline": {"stages": gated}, "skill_type": "customer"}
        assert (
            als._pipeline_from_stage_resolver(
                lead(**{field: "x"}), ctx, cfg(), TODAY
            )["pipeline_status"]
            == "reachable"
        )
        assert (
            als._pipeline_from_stage_resolver(
                lead(contact_name="Only A Name"), ctx, cfg(), TODAY
            )["pipeline_status"]
            == "rest"
        )


class TestTierThreeEvidence:
    """`calculate_pipeline` is the tier the emission change did not originally reach.

    aeo-frontend asked the question that found it: if tier 3 can place a stage off a date
    it read and not emit one, then "nothing was measured" is a lie on those rows.
    """

    def test_the_dated_branch_now_reports_its_evidence(self):
        got = als._dated_calculate_pipeline(
            {"estimated_timeline": "2027-06-15"}, cfg(), TODAY
        )
        assert got["months_to_decision"] is not None, "expected the dated branch"
        assert got.get("signal_date") == got["estimated_completion"]
        assert got["signal_date"] not in (None, "", "Unknown")

    def test_an_undated_placement_reports_nothing(self):
        # The other three branches return months_to_decision None and "Unknown"
        # estimates. Emitting a date here would recreate the over-claim in reverse.
        got = als._dated_calculate_pipeline({}, cfg(), TODAY)
        assert got["months_to_decision"] is None
        assert "signal_date" not in got

    def test_the_wrapper_does_not_alter_the_placement_itself(self):
        # It adds evidence; it must not change which rung tier 3 chose, or it stops
        # being a wrapper and becomes a second resolver.
        lead_in = {"estimated_timeline": "2027-06-15"}
        assert als._dated_calculate_pipeline(lead_in, cfg(), TODAY)[
            "pipeline_status"
        ] == als.calculate_pipeline(lead_in, cfg(), TODAY)["pipeline_status"]
