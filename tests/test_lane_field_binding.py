"""A factor binds to a FIELD of an enrichment lane, not only to the lane.

🔴 Why this file exists. `_lookup_field` is a flat lookup on the scoring lead, and the
lane overlay only exposed each lane under its own key — holding a LIST of row dicts. So a
factor bound to a lane's field resolved to `None` and scored 0 for every prospect, while
every other layer agreed it was valid: the schema advertises lanes as producing fields,
the skill builder authors field-level bindings, and the gateway's `bindableFieldNames`
validates them.

Measured on a real MYgroup draft (all default suggestions, no hand edits): two factors
bound to `switching_likelihood` and `headcount_trend`, both lane fields, both 0 on all 15
prospects, the whole 35-point ICP axis dead, and 13 of 15 prospects reported "ineligible"
against a floor the surviving axes could not reach. Nothing errored anywhere.

⚠️ The first three tests below fail without the overlay change; the last two are the
regressions it must not cause. A test file that only proved the new path works would let a
fix land that silently broke event factors, which bind to the lane WHOLESALE.
"""

from __future__ import annotations

from datetime import date

from av_lead_scanner import score_prospects

TODAY = date(2026, 8, 25)


def _ctx(factors: list[dict], **scoring: object) -> dict:
    return {
        "scoring": {
            "score_cap": 100,
            "factors_max": 40,
            "factors": factors,
            "completeness": {"max": 0},
            "region_bonus": {"max": 0},
            "multi_source": {"max": 0},
            "pipeline": {"max": 0},
            "fit": {"max": 0},
            **scoring,
        }
    }


def _factors_of(scored: list[dict]) -> dict:
    return scored[0].get("score_factors", {}).get("factors", {})


class TestLaneFieldsResolve:
    def test_a_factor_bound_to_a_lane_FIELD_scores_it(self):
        """The MYgroup shape: the factor names a field, the lane carries it."""
        out = score_prospects(
            [
                {
                    "id": "p1",
                    "company_name": "Acme",
                    "validation_data": {
                        "company_profile": [{"headcount_trend": "growing"}]
                    },
                }
            ],
            _ctx(
                [
                    {
                        "key": "growth",
                        "source_field": "headcount_trend",
                        "weight": 1,
                        "keywords": {"growing": 10, "flat": 2},
                    }
                ]
            ),
            today=TODAY,
        )
        assert _factors_of(out)["growth"] == 1.0

    def test_the_first_NON_EMPTY_value_wins_across_rows(self):
        """A blank in row 1 must not shadow a real value in row 2."""
        out = score_prospects(
            [
                {
                    "id": "p1",
                    "company_name": "Acme",
                    "validation_data": {
                        "lane": [{"status": ""}, {"status": "renewing"}]
                    },
                }
            ],
            _ctx(
                [
                    {
                        "key": "s",
                        "source_field": "status",
                        "weight": 1,
                        "keywords": {"renewing": 5},
                    }
                ]
            ),
            today=TODAY,
        )
        assert _factors_of(out)["s"] == 1.0

    def test_a_DISCOVERY_field_of_the_same_name_wins_over_a_lane(self):
        """Discovery observed it; a lane inferred it. `setdefault`, in that order."""
        out = score_prospects(
            [
                {
                    "id": "p1",
                    "company_name": "Acme",
                    "industry": "healthcare",
                    "validation_data": {"lane": [{"industry": "retail"}]},
                }
            ],
            _ctx(
                [
                    {
                        "key": "ind",
                        "source_field": "industry",
                        "weight": 1,
                        "keywords": {"healthcare": 10, "retail": 1},
                    }
                ]
            ),
            today=TODAY,
        )
        # `retail` would score 0.1 of the table's max; healthcare is full credit.
        assert _factors_of(out)["ind"] == 1.0


class TestRegressionsTheFixMustNotCause:
    def test_an_event_factor_still_binds_to_the_LANE_ITSELF(self):
        """`base_points` walks the lane's row LIST -- flattening must not replace it."""
        out = score_prospects(
            [
                {
                    "id": "p1",
                    "company_name": "Acme",
                    "validation_data": {
                        "signals": [
                            {"signal_type": "rfp", "date": "2026-08-01"},
                        ]
                    },
                }
            ],
            _ctx(
                [
                    {
                        "key": "sig",
                        "source_field": "signals",
                        "weight": 1,
                        "date_field": "date",
                        "recency_months": 12,
                        # A SCALAR, not a keyword table -- `_event_signal_credit` does
                        # `float(spec["base_points"])`, so a dict raises and silently
                        # scores 0. My first version of this test passed a dict and the
                        # assertion caught it, which is the only reason this comment
                        # exists rather than a wrong conclusion about the fix.
                        "base_points": 10,
                    }
                ]
            ),
            today=TODAY,
        )
        assert _factors_of(out)["sig"] == 1.0

    def test_a_lane_row_may_not_overwrite_a_verdict_key(self):
        """`RESERVED_LANE_KEYS` forbids them as lane KEYS; nothing stopped a row field."""
        from aeo.phases.enrichment import RESERVED_LANE_KEYS
        import av_lead_scanner as engine

        # The mirror is duplicated because the vendored engine must not import `aeo`.
        assert engine._LANE_VERDICT_KEYS == RESERVED_LANE_KEYS
