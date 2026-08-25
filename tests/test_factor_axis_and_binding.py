"""Authored factors can own their own axis, and a factor's binding is declarable.

Phase 1 of the EAP-parity work (see aeo-backend/docs/plans/configurable-scoring-parity.md).
Both halves exist because a real config could not be expressed:

* **Own axis.** A skill whose five factors sum to 100 was being compressed into 40 — the
  `fit` axis — while the other 60 came from axes it never authored, including a geography
  bonus that one delivery model explicitly forbids (services delivered remotely, so
  location must never affect fit). There was no way to say "this axis contributes
  nothing", because `fit.max: 0` was read as *unset*.

* **Declarable binding.** A factor's `name` was also its field lookup. Measured on a real
  run: a skill authored `multi_site_portfolio` / `recent_mechanical_permit` /
  `building_age` while collecting `portfolio_size`, `permit_date` and nothing at all — so
  three of four factors scored zero for every prospect, forever, with no error anywhere.

Every test here asserts against `score_prospects`' real output rather than a helper, so a
change that scores correctly but reports wrongly still fails.
"""
from __future__ import annotations

from datetime import date

import av_lead_scanner as als

TODAY = date(2026, 7, 6)

#: One prospect carrying the field names a first discovery sweep would return.
LANE_A = {
    "organization_name": "Lane A Co",
    "estimated_headcount": "850",
    "industry": "Healthcare Systems",
    "contact_title": "Vice President, Human Resources",
}

#: The same quantity under a SECOND sweep's spelling. This is the cross-lane case.
LANE_B = {
    "organization_name": "Lane B County",
    "employee_estimate": "1850",
    "entity_type": "County",
}


def _zeroed_axes() -> dict:
    """Every axis the config did not author, set to 0."""
    return {
        "completeness": {"max": 0},
        "fit": {"max": 0},
        "region_bonus": {"max": 0},
        "multi_source": {"max": 0},
        "pipeline": {"max": 0},
    }


def _score(prospects: list[dict], scoring: dict) -> list[dict]:
    return als.score_prospects(prospects, {"scoring": scoring}, today=TODAY)


class TestOwnAxis:
    def test_factors_can_carry_the_whole_score_cap(self):
        """The headline: a score composed ONLY of the criteria the skill authored."""
        out = _score(
            [LANE_A],
            {
                "score_cap": 100,
                "factors_max": 100,
                **_zeroed_axes(),
                "factors": [
                    {"key": "size", "source_field": "estimated_headcount", "weight": 1},
                    {"key": "vertical", "source_field": "industry", "weight": 1},
                    {"key": "dm", "source_field": "contact_title", "weight": 1},
                ],
            },
        )
        f = out[0]["score_factors"]
        assert out[0]["score"] == 100
        assert f["factors_score"] == 100
        # Nothing leaked in from an axis this config did not author.
        assert (f["completeness"], f["fit"], f["region_bonus"], f["multi_source"]) == (
            0, 0, 0, 0,
        )
        assert f["pipeline_timing"] == 0

    def test_partial_match_scales_within_the_axis(self):
        out = _score(
            [LANE_A],
            {
                "score_cap": 100,
                "factors_max": 100,
                **_zeroed_axes(),
                "factors": [
                    {"key": "size", "source_field": "estimated_headcount", "weight": 1},
                    {"key": "absent", "source_field": "nothing_collects_this", "weight": 1},
                ],
            },
        )
        assert out[0]["score"] == 50

    def test_zeroed_fit_axis_is_honoured_when_factors_are_authored(self):
        """Regression for a falsy guard.

        `fit.max: 0` is the only way to say "this axis contributes nothing". The override
        tested truthiness, so 0 was read as *unset* and silently restored to 40 — the
        exact opposite of what was asked for. Under that bug `fit` here reports 40.
        """
        out = _score(
            [LANE_A],
            {
                "score_cap": 100,
                "factors_max": 100,
                **_zeroed_axes(),
                "factors": [{"key": "size", "source_field": "estimated_headcount", "weight": 1}],
            },
        )
        assert out[0]["score_factors"]["fit"] == 0

    def test_fit_is_scored_independently_of_factors_in_own_axis_mode(self):
        """Own-axis mode means both axes are live, not that one replaces the other."""
        out = _score(
            [{"organization_name": "X", "project_type": "new build", "size": "9"}],
            {
                "score_cap": 100,
                "factors_max": 20,
                "completeness": {"max": 0},
                "region_bonus": {"max": 0},
                "multi_source": {"max": 0},
                "pipeline": {"max": 0},
                "fit": {"max": 25, "text_fields": ["project_type"],
                        "keyword_scores": {"new build": 25}},
                "factors": [{"key": "size", "source_field": "size", "weight": 1}],
            },
        )
        f = out[0]["score_factors"]
        assert (f["fit"], f["factors_score"]) == (25, 20)
        assert out[0]["score"] == 45


class TestLegacyAxisUnchanged:
    def test_factors_still_borrow_the_fit_axis_at_40_when_factors_max_is_absent(self):
        out = _score(
            [LANE_A],
            {
                "score_cap": 100,
                "completeness": {"max": 0},
                "region_bonus": {"max": 0},
                "multi_source": {"max": 0},
                "pipeline": {"max": 0},
                "factors": [{"name": "estimated_headcount", "weight": 1}],
            },
        )
        f = out[0]["score_factors"]
        # Reported under `fit`, as it always was — own-axis mode ADDS a key, it does not
        # move this one, because existing operator surfaces read `fit`.
        assert f["fit"] == 40
        assert "factors_score" not in f
        assert out[0]["score"] == 40

    def test_a_config_with_no_factors_gains_no_new_key(self):
        """Shape stability: every existing consumer sees exactly the keys it saw before."""
        out = _score(
            [{"organization_name": "X", "project_type": "renovation"}],
            {
                "score_cap": 100,
                "completeness": {"max": 0},
                "region_bonus": {"max": 0},
                "multi_source": {"max": 0},
                "pipeline": {"max": 0},
                "fit": {"max": 25, "text_fields": ["project_type"],
                        "keyword_scores": {"renovation": 12}},
            },
        )
        f = out[0]["score_factors"]
        assert "factors_score" not in f
        assert "factors" not in f
        assert f["fit"] == 12


class TestDeclarableBinding:
    def test_source_field_reads_a_field_the_name_does_not_match(self):
        """`key`/`name` label the criterion; `source_field` does the reading.

        Without `source_field` the lookup normalizes "Size Fit" to `sizefit`, finds
        nothing, and scores 0 — which is the production defect this splits apart.
        """
        factors = [{"key": "size_fit", "name": "Size Fit",
                    "source_field": "estimated_headcount", "weight": 1}]
        scoring = {"score_cap": 100, "factors_max": 100, **_zeroed_axes(), "factors": factors}
        assert _score([LANE_A], scoring)[0]["score"] == 100

        # Same factor, binding removed: the label alone matches no collected field.
        bare = [{"name": "Size Fit", "weight": 1}]
        scoring_bare = {"score_cap": 100, "factors_max": 100, **_zeroed_axes(),
                        "factors": bare}
        assert _score([LANE_A], scoring_bare)[0]["score"] == 0

    def test_source_field_list_falls_through_to_the_second_lane(self):
        """One factor, two sweeps that named the same quantity differently."""
        scoring = {
            "score_cap": 100,
            "factors_max": 100,
            **_zeroed_axes(),
            "factors": [{
                "key": "size_fit",
                "source_field": ["estimated_headcount", "employee_estimate"],
                "weight": 1,
            }],
        }
        out = _score([LANE_A, LANE_B], scoring)
        # Both lanes score, each via a DIFFERENT candidate in the same list — LANE_A on
        # `estimated_headcount`, LANE_B on `employee_estimate`.
        assert len(out) == 2
        assert {o["score"] for o in out} == {100}
        for o in out:
            assert o["score_factors"]["factors"]["size_fit"] == 1

    def test_min_bound_still_applies_through_an_explicit_binding(self):
        scoring = {
            "score_cap": 100,
            "factors_max": 100,
            **_zeroed_axes(),
            "factors": [{"key": "size_fit", "source_field": "estimated_headcount",
                         "weight": 1, "min": 1000}],
        }
        # 850 is below the floor; 1850 arrives under the other lane's name and is absent
        # from this binding, so it earns nothing either.
        out = _score([LANE_A], scoring)
        assert out[0]["score"] == 0
        assert out[0]["score_factors"]["factors"]["size_fit"] == 0

    def test_key_labels_the_breakdown_and_name_alone_still_does(self):
        keyed = _score(
            [LANE_A],
            {"score_cap": 100, "factors_max": 100, **_zeroed_axes(),
             "factors": [{"key": "size_fit", "name": "Size Fit",
                          "source_field": "estimated_headcount", "weight": 1}]},
        )
        assert set(keyed[0]["score_factors"]["factors"]) == {"size_fit"}

        unkeyed = _score(
            [LANE_A],
            {"score_cap": 100, "factors_max": 100, **_zeroed_axes(),
             "factors": [{"name": "estimated_headcount", "weight": 1}]},
        )
        assert set(unkeyed[0]["score_factors"]["factors"]) == {"estimated_headcount"}


class TestBindingHelpers:
    def test_source_fields_prefers_source_field_over_name(self):
        assert als.factor_source_fields(
            {"name": "Label", "source_field": "real_field"}
        ) == ("real_field",)

    def test_source_fields_falls_back_to_name(self):
        assert als.factor_source_fields({"name": "real_field"}) == ("real_field",)

    def test_source_fields_keeps_list_order_and_drops_blanks(self):
        assert als.factor_source_fields(
            {"name": "n", "source_field": ["first", "  ", "second"]}
        ) == ("first", "second")

    def test_label_prefers_key(self):
        assert als.factor_label({"key": "k", "name": "n"}) == "k"
        assert als.factor_label({"name": "n"}) == "n"


class TestTheDefaultKeywordTableNeverLeaksIntoAnotherVertical:
    """🔴 A regression I introduced with own-axis mode, and the PO's standing rule.

    `_DEFAULT_SCORING["fit"]["keyword_scores"]` is the ORIGINAL customer's church-AV
    vocabulary (`new construction: 25`, `renovation: 12`). Until factors could own their
    own axis it was unreachable for any skill with authored factors, because factors
    REPLACED the fit axis. Own-axis mode made it reachable again — and a flooring prospect
    whose text says "office renovation" was measured collecting **12 points from a church
    keyword list**, which is precisely the static-industry coupling the engine is supposed
    to have none of.

    Nothing errored, and the points landed in a plausible range. That is the whole danger.
    """

    FACTORS = [
        {
            "key": "sf",
            "source_field": "square_footage",
            "weight": 1,
            "tiers": [{"threshold": 10_000, "points": 10}, {"threshold": 0, "points": 0}],
        }
    ]
    ZEROED = {
        "completeness": {"max": 0},
        "region_bonus": {"max": 0},
        "multi_source": {"max": 0},
        "pipeline": {"max": 0},
    }
    #: Text from a DIFFERENT vertical that happens to hit the default church table.
    PROSPECT = {
        "organization_name": "Meridian Property Group",
        "square_footage": "50000",
        "project_description": "office renovation and tenant build-out",
    }

    def _fit(self, fit_cfg: dict) -> int:
        out = als.score_prospects(
            [dict(self.PROSPECT)],
            {
                "scoring": {
                    "score_cap": 100,
                    "factors_max": 45,
                    "fit": fit_cfg,
                    **self.ZEROED,
                    "factors": self.FACTORS,
                }
            },
            today=TODAY,
        )
        return out[0]["score_factors"]["fit"]

    def test_a_skill_with_factors_gets_no_keyword_axis_it_did_not_author(self):
        # `fit.max` is authored, `fit.keyword_scores` is NOT. Before the fix this was 12.
        assert self._fit({"max": 20}) == 0

    def test_a_skill_that_authors_its_own_keywords_still_gets_them(self):
        # The axis is not disabled — only the inherited vocabulary is.
        assert (
            self._fit(
                {
                    "max": 20,
                    "text_fields": ["project_description"],
                    "keyword_scores": {"tenant build-out": 20},
                }
            )
            == 20
        )

    def test_a_config_with_NO_factors_keeps_the_legacy_default(self):
        """The legacy shape the default exists for is untouched.

        Narrowing this any further would silently zero the fit axis for every
        pre-factors config, which is a much larger blast radius than the leak.
        """
        out = als.score_prospects(
            [{"organization_name": "Church", "project_type": "renovation"}],
            {"scoring": {"score_cap": 100, **self.ZEROED}},
            today=TODAY,
        )
        assert out[0]["score_factors"]["fit"] == 12
