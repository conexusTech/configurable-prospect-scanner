"""The 54-row signal_class fixture, and the properties that make it worth having.

**Regenerate the fixture, never hand-edit it.** It is derived from run 741b7b3b:

    select lower(trim(sig->>'signal_type')), count(*)
      from prospects p, jsonb_array_elements(p.validation_data->'switching_signal') sig
     where p.scan_run_id = '741b7b3b-c3ed-40bc-8d84-725de4986f94'
     group by 1 order by 2 desc, 1;

54 distinct phrasings across 72 signal rows — every spelling the model actually produced.
The count matters: the spec said "28+ distinct phrasings" for two revisions, taken from a
truncated query result nobody re-checked. Deriving it is how that stopped being a guess.

🔴 **The fixture must be sensitive to RULE ORDER, not just to the rule set.** A classifier
built from unordered keywords passes a per-class test and still misroutes every phrasing
that matches two rules — which is most of the interesting ones. The order tests below are
the real content; the 54-row sweep is the regression net under them.
"""
import json
from pathlib import Path

import pytest

from aeo.signal_class import SIGNAL_CLASSES, classify, normalize

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "signal-class-54.json").read_text("utf-8")
)
PHRASINGS = FIXTURE["phrasings"]


class TestFixtureIntegrity:
    def test_the_fixture_is_the_whole_run_not_a_sample(self):
        assert FIXTURE["distinct_phrasings"] == 54
        assert FIXTURE["signal_rows"] == 72
        assert len(PHRASINGS) == 54
        assert sum(p["rows"] for p in PHRASINGS) == 72

    def test_every_phrasing_is_distinct_and_already_normalised(self):
        seen = [p["signal_type"] for p in PHRASINGS]
        assert len(set(seen)) == len(seen)
        # Stored lowercased+trimmed, so a case-only difference cannot masquerade as a
        # new phrasing and inflate the count.
        assert all(s == s.lower().strip() for s in seen)

    def test_every_expected_class_is_in_the_closed_enum(self):
        for p in PHRASINGS:
            assert p["expected_class"] in SIGNAL_CLASSES or p["expected_class"] is None


class TestTheFiftyFour:
    @pytest.mark.parametrize(
        "phrasing", PHRASINGS, ids=[p["signal_type"] for p in PHRASINGS]
    )
    def test_every_observed_phrasing_classifies_as_pinned(self, phrasing):
        assert classify(phrasing["signal_type"]) == phrasing["expected_class"]

    def test_coverage_is_71_of_72_rows(self):
        # Not 72: `cost_reduction_initiative` is deliberately unrecognised. Forcing a
        # class onto it would be the D2 mistake again — hand-guessing a keyword to make
        # a number look complete. `None` is a real answer that the caller logs.
        covered = sum(p["rows"] for p in PHRASINGS if p["expected_class"])
        assert covered == 71
        unrecognised = [p["signal_type"] for p in PHRASINGS if not p["expected_class"]]
        assert unrecognised == ["cost_reduction_initiative"]


class TestNormalisation:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("workforce_stress", "workforce stress"),
            ("workforce_stress_event", "workforce stress event"),
            ("broker_relationship_change", "broker relationship change"),
            ("employee_sentiment", "employee sentiment"),
            ("leadership_change", "leadership change"),
        ],
    )
    def test_an_underscore_is_no_longer_a_different_signal(self, a, b):
        # 🔴 The measured D2 defect: `workforce stress event` matched the old table and
        # `workforce_stress` did not, so the same signal scored 0.40 or 0 depending on
        # which spelling the model happened to choose that row.
        assert normalize(a) == normalize(b)
        assert classify(a) == classify(b) is not None

    def test_punctuation_and_case_do_not_change_the_class(self):
        for raw in ("M&A Activity", "m&a activity", "  M&A   ACTIVITY  ", "m/a-activity"):
            assert classify(raw) == "corporate_event"

    def test_blank_and_missing_are_none_not_a_class(self):
        for raw in (None, "", "   ", "\t"):
            assert classify(raw) is None


class TestRuleOrder:
    """Each of these fails if the rule table is reordered. That is the point."""

    def test_dissatisfaction_beats_benefits(self):
        # `low benefit satisfaction` contains "benefit" but is a COMPLAINT about
        # benefits, not a change to them. Ranked the other way the dissatisfaction
        # signal disappears into the benefits band and nothing looks wrong.
        assert classify("low benefit satisfaction") == "dissatisfaction"
        assert classify("low utilization / employee dissatisfaction") == "dissatisfaction"

    def test_benefits_beats_leadership(self):
        # The spec's explicit ruling: the phrase contains "benefits" BECAUSE the model
        # identified the function, so it marks a leadership change inside the buying
        # centre — a forward-looking broker trigger, not generic churn.
        assert classify("key personnel change (benefits leadership)") == "benefits_change"
        # ...and generic churn stays put, which is what makes the distinction real.
        assert classify("senior leadership appointment") == "leadership_change"

    def test_benefits_beats_corporate_and_workforce(self):
        assert classify("acquisition and benefit consolidation") == "benefits_change"
        assert classify("recruitment-driven benefit focus") == "benefits_change"
        assert classify("benefit strategy shift") == "benefits_change"
        # The same phrasing WITHOUT the benefit word must not follow it.
        assert classify("strategy shift") == "corporate_event"

    def test_rfp_outranks_everything(self):
        # The strongest signal in the vocabulary; nothing may shadow it.
        assert classify("rfp activity") == "rfp_active"
        assert classify("benefits rfp") == "rfp_active"
        assert classify("broker-led rfp") == "rfp_active"

    def test_broker_outranks_benefits_and_below(self):
        assert classify("broker change") == "broker_carrier_change"
        assert classify("carrier change") == "broker_carrier_change"
        assert classify("broker benefit review") == "broker_carrier_change"
