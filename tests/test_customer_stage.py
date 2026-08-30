"""The ported stage resolver must reproduce AEO's, rung for rung.

🔴 **AEO's `customer-pipeline-stage.util.ts` is the reference.** Every expectation here
is what that file produces; if one fails, the PORT is wrong. Do not adjust an expectation
to match this module's behaviour.

The band cases are pinned because they are silent when wrong: an off-by-one on an
exclusive bound moves a prospect one rung without erroring, and under the gated scoring
model a rung is the difference between clearing the 80 floor and capping at 45.
"""
from datetime import date

import pytest

from aeo.customer_stage import (
    collect_signal_dates,
    extract_customer_stage_defs,
    extract_signal_fields,
    months_from_today,
    parse_signal_date,
    resolve_customer_stage,
    resting_stage,
)

TODAY = date(2026, 8, 27)

# The shared TIMELINE ladder, in the snake_case shape the gateway puts on the wire.
# All rungs are banded, so none is a resting rung — which is why `resting_stage` falls
# through to the FIRST rung, and why an unjudged prospect lands on Early Discovery.
LADDER = [
    {"key": "1 - Early Discovery", "min_months": 18, "max_months": 999, "kind": "timing"},
    {"key": "2 - Relationship Building", "min_months": 12, "max_months": 18, "kind": "timing"},
    {"key": "3 - Design Influence", "min_months": 8, "max_months": 12, "kind": "timing"},
    {"key": "4 - Active Pursuit", "min_months": 4, "max_months": 8, "kind": "timing"},
    {"key": "5 - Decision Imminent", "min_months": 0, "max_months": 4, "kind": "timing"},
    {"key": "6 - Likely Awarded", "min_months": -4, "max_months": 0, "kind": "timing"},
    {"key": "7 - Too Late", "min_months": -999, "max_months": -4, "kind": "timing"},
]


def defs():
    return extract_customer_stage_defs({"stages": LADDER})


class TestWireShape:
    def test_reads_the_snake_case_shape_the_gateway_actually_sends(self):
        # The first cut of this module parsed camelCase and resolved None for every
        # prospect on a real run. That is the regression this test exists for.
        got = extract_customer_stage_defs({"stages": LADDER})
        assert got is not None and len(got) == 7
        assert got[0].is_timing and got[0].min_months == 18

    def test_also_tolerates_the_raw_config_camel_case(self):
        got = extract_customer_stage_defs(
            {"stages": [{"key": "a", "minMonths": 1, "maxMonths": 2}]}
        )
        assert got is not None and got[0].is_timing

    def test_bare_strings_become_no_signal_rungs(self):
        got = extract_customer_stage_defs({"stages": ["only-rung"]})
        assert got is not None and not got[0].is_timing

    def test_absent_vocabulary_is_none_not_empty(self):
        # "No vocabulary" must be distinguishable from "a vocabulary with no rungs";
        # the caller falls through to calculate_pipeline on None.
        assert extract_customer_stage_defs({"stages": []}) is None
        assert extract_customer_stage_defs({}) is None
        assert extract_customer_stage_defs(None) is None

    def test_a_boolean_is_not_a_band(self):
        # Python makes True an int; the gateway's `typeof === 'number'` rejects it. If
        # these disagreed, `minMonths: true` would band here and not there.
        got = extract_customer_stage_defs(
            {"stages": [{"key": "a", "min_months": True, "max_months": 2}]}
        )
        assert got is not None and not got[0].is_timing

    def test_explicit_no_signal_kind_beats_stray_bands(self):
        got = extract_customer_stage_defs(
            {"stages": [{"key": "a", "min_months": 1, "max_months": 2, "kind": "no_signal"}]}
        )
        assert got is not None and not got[0].is_timing


class TestDateParsing:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("2026-01-16", date(2026, 1, 16)),
            ("02/21/2026", date(2026, 2, 21)),
            ("April 27, 2023", date(2023, 4, 27)),
            # Month + year: mid-month, NOT the 1st. The 1st biases every such row half a
            # month early, which can tip it across a band boundary.
            ("August 2026", date(2026, 8, 15)),
        ],
    )
    def test_parses_the_spellings_real_runs_contain(self, raw, expected):
        assert parse_signal_date(raw) == expected

    def test_a_bare_year_is_refused(self):
        # Deliberate: "2026" carries no month, and inventing one is what manufactured
        # fabricated dates upstream. Refusing costs a stage on a few rows.
        assert parse_signal_date("2026") is None

    def test_a_rolled_over_date_is_refused(self):
        assert parse_signal_date("02/31/2026") is None

    def test_junk_and_blanks_are_refused(self):
        for raw in ("", "   ", None, "next spring", "2026-13-01"):
            assert parse_signal_date(raw) is None

    def test_months_ignores_the_day(self):
        assert months_from_today(date(2026, 10, 1), TODAY) == 2
        assert months_from_today(date(2026, 10, 31), TODAY) == 2
        assert months_from_today(date(2026, 6, 1), TODAY) == -2


class TestBands:
    @pytest.mark.parametrize(
        "months,expected",
        [
            (0, "5 - Decision Imminent"),    # inclusive LOW bound
            (3, "5 - Decision Imminent"),
            (4, "4 - Active Pursuit"),       # exclusive HIGH bound moves the rung
            (18, "1 - Early Discovery"),
            (17, "2 - Relationship Building"),
            # -4 is the INCLUSIVE-low bound of "6 - Likely Awarded", declared first, so
            # it wins. `find` takes the first match in declared order, not the tightest.
            (-4, "6 - Likely Awarded"),
            (-5, "7 - Too Late"),
            (-1, "6 - Likely Awarded"),
        ],
    )
    def test_bands_are_inclusive_low_exclusive_high(self, months, expected):
        # Build a date landing exactly `months` from TODAY.
        y, m = divmod((TODAY.year * 12 + TODAY.month - 1) + months, 12)
        assert resolve_customer_stage(
            defs(), [f"{y:04d}-{m + 1:02d}-10"], False, TODAY
        ) == expected

    def test_the_date_closest_to_now_decides(self):
        # A decade-old transaction_date beside a fresh trigger_date must not drag the
        # prospect backwards.
        stage = resolve_customer_stage(
            defs(), ["2016-01-01", "2026-09-10"], False, TODAY
        )
        assert stage == "5 - Decision Imminent"

    def test_a_tie_keeps_the_earlier_value(self):
        # Mirrors the TS reduce, which replaces only on strictly-less.
        assert resolve_customer_stage(
            defs(), ["2026-10-10", "2026-06-10"], False, TODAY
        ) == "5 - Decision Imminent"


class TestFallback:
    def test_no_parseable_date_lands_on_the_resting_rung(self):
        # groninger's case on run 741b7b3b, and the reason the port exists. Every
        # TIMELINE rung is banded, so resting falls through to the FIRST rung.
        assert resolve_customer_stage(defs(), [], True, TODAY) is None
        assert resting_stage(defs()) == "1 - Early Discovery"

    def test_a_bare_year_only_prospect_also_rests(self):
        # "2026" is refused, so this is the no-date path even though a value exists.
        assert resolve_customer_stage(defs(), ["2026"], False, TODAY) is None

    def test_resting_rung_is_never_a_banded_one(self):
        # The bug AEO shipped once: an ungated-only rule picked a BANDED rung and filed
        # every signal-less prospect under the most actionable stage on the board.
        rungs = extract_customer_stage_defs(
            {"stages": [
                {"key": "banded", "min_months": 0, "max_months": 4},
                {"key": "unbanded"},
            ]}
        )
        assert resting_stage(rungs) == "unbanded"

    def test_contact_gated_rung_wins_only_when_reachable(self):
        rungs = extract_customer_stage_defs(
            {"stages": [
                {"key": "gated", "requires_contact": True},
                {"key": "ungated"},
            ]}
        )
        assert resolve_customer_stage(rungs, [], True, TODAY) == "gated"
        assert resolve_customer_stage(rungs, [], False, TODAY) == "ungated"


class TestSignalCollection:
    def test_reads_every_source_and_only_the_declared_fields(self):
        discovery = {
            "by_source": {
                "a": {"trigger_date": "2026-01-01", "noise": "2020-01-01"},
                "b": {"transaction_date": "2026-02-01"},
                "c": {"trigger_date": "   "},
            }
        }
        got = collect_signal_dates(discovery, ("trigger_date", "transaction_date"))
        assert sorted(got) == ["2026-01-01", "2026-02-01"]

    def test_survives_a_malformed_discovery_blob(self):
        for blob in (None, {}, {"by_source": None}, {"by_source": {"a": "nope"}}):
            assert collect_signal_dates(blob, ("trigger_date",)) == []

    def test_signal_fields_read_the_wire_spelling_from_the_pipeline_block(self):
        # The scanner passes ctx["pipeline"], whose wire spelling is `signal_fields`.
        # Reading the outer config or camelCase here silently discards a skill's
        # declared fields and falls back to the default pair — which is the exact
        # defect the gateway's own hardcoded copy had.
        assert extract_signal_fields({"signal_fields": ["x"]}) == ("x",)
        assert extract_signal_fields({"signalFields": ["y"]}) == ("y",)
        assert extract_signal_fields({"pipeline": {"signal_fields": ["z"]}}) == ("z",)

    def test_signal_fields_default_when_undeclared_or_empty(self):
        for blob in ({}, None, {"signal_fields": []}, {"signal_fields": "nope"}):
            assert extract_signal_fields(blob) == ("trigger_date", "transaction_date")
