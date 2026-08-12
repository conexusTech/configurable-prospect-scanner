"""Values the engine needs must come from the target organization, never a default.

PO ruling (2026-08-12): this engine must be fully flexible to whatever config the
Conversational Skill Builder produced — not static on any industry, organization or
geography — and anything missing must be sourced from the organization the scan runs for.

The two bindings this file covers were the last *active* geography/org-static harms:

* `region_bonus.regions` was `{}` and `state_aliases` covered Texas, Colorado and
  Indiana — the previous customer's states. A Tennessee org could not score region at
  all, no matter how good its location data was.
* `pipeline.decision_lead_months` was a hardcoded 13-month church-construction cycle,
  while the org's own `sales_cycle_months` was published by the gateway and read by
  nothing.
"""
from __future__ import annotations

from datetime import date

import av_lead_scanner as als

# The real org's service areas from run c214e3d5.
TN_MARKETS = ["Nashville, TN", "Murfreesboro, TN", "Franklin, TN", "Clarksville, TN"]


class TestRegionsFromMarkets:
    def test_builds_the_region_map_from_the_orgs_own_markets(self):
        regions, cities = als.regions_from_markets(TN_MARKETS)
        assert regions == {"tn": ["nashville", "murfreesboro", "franklin", "clarksville"]}
        assert cities == ["nashville", "murfreesboro", "franklin", "clarksville"]

    def test_scores_a_prospect_inside_the_orgs_markets(self):
        regions, cities = als.regions_from_markets(TN_MARKETS)
        cfg = {"max": 10, "regions": regions, "market_cities": cities, "state_aliases": {}}
        assert als.score_region({"city": "Nashville", "state": "TN"}, cfg) == 10

    def test_does_not_score_a_prospect_outside_them(self):
        """The old defaults made this unreachable in both directions — everything scored 0."""
        regions, cities = als.regions_from_markets(TN_MARKETS)
        cfg = {"max": 10, "regions": regions, "market_cities": cities, "state_aliases": {}}
        assert als.score_region({"city": "Austin", "state": "TX"}, cfg) == 0
        # Brentwood is a Nashville suburb the org did not list. 0 is correct: the org's
        # market list is the authority, not our idea of the metro.
        assert als.score_region({"city": "Brentwood", "state": "TN"}, cfg) == 0

    def test_a_market_written_with_a_full_state_name_still_matches_on_city(self):
        """No state-name table is introduced, so the city list carries this case."""
        regions, cities = als.regions_from_markets(["Nashville, Tennessee"])
        assert regions == {"tennessee": ["nashville"]}
        cfg = {"max": 10, "regions": regions, "market_cities": cities, "state_aliases": {}}
        assert als.score_region({"city": "Nashville", "state": "TN"}, cfg) == 10

    def test_no_markets_means_no_bonus_not_a_crash(self):
        regions, cities = als.regions_from_markets(None)
        assert (regions, cities) == ({}, [])
        assert als.score_region({"city": "x", "state": "TN"}, {"max": 10}) == 0


class TestOrgSourcedScoring:
    def _ctx(self, **org):
        return {
            "organization": {"name": "Lee Company", **org},
            "sources": {"s": {"fields": ["company_name", "square_footage"]}},
            "scoring": {"factors": [{"name": "square_footage", "weight": 1}]},
        }

    def test_region_is_derived_from_org_markets_without_config(self):
        ctx = self._ctx(markets=TN_MARKETS)
        prospects = [{
            "id": "p1", "company_name": "Southeast Venture",
            "city": "Nashville", "state": "TN",
            "discovery_data": {"by_source": {"s": {"square_footage": "15273"}}},
        }]
        out = als.score_prospects(prospects, ctx, today=date(2026, 8, 12))
        assert out[0]["score_factors"]["region_bonus"] > 0, "org markets must drive region"
        assert out[0]["score_factors"]["is_region"] is True

    def test_sales_cycle_comes_from_the_organization(self):
        """The engine's pipeline maths is entirely driven by this number.

        ⚠️ Note `estimated_timeline` must be AUTHORED for the pipeline axis to see it:
        `calculate_pipeline` still reads that one hardcoded field name, so a skill whose
        timing signal is `permit_date` or `replacement_due` cannot score timing at all.
        That is the next static binding to remove; this test authors the field so the
        sales-cycle assertion below is actually exercised.
        """
        ctx = self._ctx(markets=TN_MARKETS, sales_cycle_months=4)
        ctx["sources"]["s"]["fields"].append("estimated_timeline")
        prospects = [{"id": "p1", "company_name": "X", "city": "Nashville", "state": "TN",
                      "discovery_data": {"by_source": {"s": {"estimated_timeline": "2027-06"}}}}]
        out = als.score_prospects(prospects, ctx, today=date(2026, 8, 12))
        # Completion 2027-06 minus the org's 4-month cycle -> decision 2027-02, six
        # months out: "4 - Active Pursuit", the full 30 timing points.
        assert out[0]["months_to_decision"] == 6
        assert out[0]["score_factors"]["pipeline_timing"] == 30

        # 🔑 The same prospect under the old hardcoded 13-month church-construction
        # cycle: decision 2026-05, **three months in the PAST** -> "6 - Likely Awarded",
        # 8 points. A 22-point swing and the opposite sales conclusion — the engine
        # declared the deal already lost — from a number that had nothing to do with
        # this organization.
        del ctx["organization"]["sales_cycle_months"]
        old = als.score_prospects(prospects, ctx, today=date(2026, 8, 12))[0]
        assert old["months_to_decision"] == -3
        assert old["score_factors"]["pipeline_timing"] == 8

    def test_authored_factors_drive_the_score(self):
        ctx = self._ctx(markets=TN_MARKETS)
        rich = {"id": "a", "company_name": "A", "city": "Nashville", "state": "TN",
                "discovery_data": {"by_source": {"s": {"square_footage": "15273"}}}}
        bare = {"id": "b", "company_name": "B", "city": "Nashville", "state": "TN",
                "discovery_data": {"by_source": {"s": {}}}}
        out = {i["prospect_id"]: i for i in als.score_prospects(rich | {} and [rich, bare], ctx, today=date(2026, 8, 12))}
        assert out["a"]["score"] > out["b"]["score"], "the authored factor must discriminate"
        assert out["a"]["score_factors"]["factors"] == {"square_footage": 1.0}
        assert out["b"]["score_factors"]["factors"] == {"square_footage": 0.0}


class TestScorerAndRecordSeeTheSameLocality:
    """Regression for a defect that shipped and reached production data.

    `_parse_locality` was applied to the prospect *record* but not to `_internal`, and
    `_scoring_input` reads `_internal` whenever it is present — which is always, in a
    single-process run. So the database showed `city = Nashville` for 35 of 36 prospects
    while the scorer saw an empty city and gave **every one of them** `region_bonus: 0`
    — on the same run whose geo-fence had just reported them as in-area. Two views of
    one fact.

    🔑 **Why the original tests missed it:** they passed prospects with fields at the top
    level, which exercises `_scoring_input`'s *reconstruct* path. The real discovery path
    carries `_internal` and never reconstructs. These tests go through
    `_assemble_prospects` so they use the shape production uses.
    """

    def _assembled(self):
        groups = {
            als.normalize_name("Bass Berry"): [
                {
                    "raw": {
                        "organization_name": "Bass Berry",
                        "address": "150 3rd Ave S, Nashville, TN 37201",
                        "square_footage": "50000",
                    },
                    "source": "s",
                    "name_field": "organization_name",
                }
            ]
        }
        return als._assemble_prospects(
            groups=groups, scan_run_id="r1", canonical=("company_name", "square_footage")
        )

    def test_the_record_and_internal_agree(self):
        p = self._assembled()[0]
        assert p["city"] == "Nashville" and p["state"] == "TN"
        assert p["_internal"]["city"] == "Nashville", "the scorer reads this one"
        assert p["_internal"]["state"] == "TN"

    def test_region_scores_through_the_real_discovery_path(self):
        ctx = {
            "organization": {"name": "Lee Company", "markets": ["Nashville, TN"]},
            "sources": {"s": {"fields": ["company_name", "square_footage"]}},
            "scoring": {"factors": [{"name": "square_footage", "weight": 25}]},
        }
        out = als.score_prospects(self._assembled(), ctx, today=date(2026, 8, 12))[0]
        assert out["score_factors"]["region_bonus"] > 0
        assert out["score_factors"]["is_region"] is True

    def test_a_prospect_outside_the_markets_still_scores_no_region(self):
        """The bonus must remain a discriminator, not become universally true."""
        groups = {
            als.normalize_name("Far Co"): [
                {
                    "raw": {"organization_name": "Far Co", "address": "1 Main St, Memphis, TN 38103"},
                    "source": "s",
                    "name_field": "organization_name",
                }
            ]
        }
        pros = als._assemble_prospects(groups=groups, scan_run_id="r1", canonical=("company_name",))
        ctx = {
            "organization": {"name": "Lee Company", "markets": ["Nashville, TN"]},
            "sources": {"s": {"fields": ["company_name"]}},
            "scoring": {},
        }
        out = als.score_prospects(pros, ctx, today=date(2026, 8, 12))[0]
        assert out["score_factors"]["region_bonus"] == 0


class TestRunLimitsComeFromConfig:
    """An environment variable is static configuration too.

    `SCANNER_TOP_N` alone drove three unrelated limits — how many in-area prospects
    discovery keeps looking for, how many get contact enrichment, and the output cut. Set
    to 1 in a real environment it capped a 15-market scan to one round of discovery and
    enriched 1 prospect of 14, which reads as "the model found little" rather than "we
    stopped looking". The config now wins; the env is the deployment default only.
    """

    def test_config_beats_the_environment(self, monkeypatch):
        from aeo import runner

        monkeypatch.setenv("SCANNER_TOP_N", "1")
        assert runner._config_limit({"discovery": {"target_prospects": 40}}, "discovery", "target_prospects") == 40
        assert runner._config_limit({"contacts": {"max_prospects": 25}}, "contacts", "max_prospects") == 25

    def test_environment_is_the_fallback_when_config_is_silent(self, monkeypatch):
        from aeo import runner

        monkeypatch.setenv("SCANNER_TOP_N", "7")
        assert runner._config_limit({}, "discovery", "target_prospects") == 7

    def test_falls_back_to_the_engine_default_with_neither(self, monkeypatch):
        from aeo import runner

        monkeypatch.delenv("SCANNER_TOP_N", raising=False)
        assert runner._config_limit({}, "discovery", "target_prospects") == 50

    def test_junk_config_value_does_not_win(self, monkeypatch):
        from aeo import runner

        monkeypatch.delenv("SCANNER_TOP_N", raising=False)
        for junk in ("abc", None, 0, -5, {}):
            assert runner._config_limit({"discovery": {"target_prospects": junk}}, "discovery", "target_prospects") == 50


class TestAuthoredFactorsAreTheDominantAxis:
    def _ctx(self, **scoring):
        return {
            "organization": {"name": "Lee Company", "markets": TN_MARKETS},
            "sources": {"s": {"fields": ["company_name", "square_footage", "portfolio_size"]}},
            "scoring": scoring,
        }

    FACTORS = [{"name": "square_footage", "weight": 50}, {"name": "portfolio_size", "weight": 50}]

    def _prospects(self):
        return [
            {"id": "a", "company_name": "A", "city": "Nashville", "state": "TN",
             "discovery_data": {"by_source": {"s": {"square_footage": "15273", "portfolio_size": "12"}}}},
            {"id": "b", "company_name": "B", "city": "Nashville", "state": "TN",
             "discovery_data": {"by_source": {"s": {"square_footage": "9000"}}}},
        ]

    def test_the_operators_own_criteria_carry_the_most_weight(self):
        out = als.score_prospects(self._prospects(), self._ctx(factors=self.FACTORS), today=date(2026, 8, 12))
        by_id = {i["prospect_id"]: i for i in out}
        assert by_id["a"]["score_factors"]["fit"] == 40, "larger than pipeline timing's 30"
        assert by_id["a"]["score"] > by_id["b"]["score"], "must discriminate between prospects"

    def test_an_explicit_fit_max_still_wins(self):
        ctx = self._ctx(factors=self.FACTORS, fit={"max": 10})
        out = als.score_prospects(self._prospects(), ctx, today=date(2026, 8, 12))
        assert out[0]["score_factors"]["fit"] <= 10

    def test_no_factors_leaves_the_legacy_axis_weight_alone(self):
        out = als.score_prospects(self._prospects(), self._ctx(), today=date(2026, 8, 12))
        assert out[0]["score_factors"]["fit"] == 0  # nothing authored, nothing scored


class TestDisqualifyBelowFlagsRatherThanFilters:
    """Authored since day one, read by nothing until 2026-08-12.

    Implemented as a flag: the operator sets the threshold before ever seeing a score
    distribution, and on the first real run every prospect scored 11-12 against a
    threshold of 20 — filtering would have deleted the entire yield with no error.
    """

    def _ctx(self, **scoring):
        return {
            "organization": {"name": "Lee", "markets": TN_MARKETS},
            "sources": {"s": {"fields": ["company_name", "square_footage"]}},
            "scoring": scoring,
        }

    P = [{"id": "a", "company_name": "A", "city": "Nashville", "state": "TN",
          "discovery_data": {"by_source": {"s": {"square_footage": "15273"}}}}]

    def test_prospects_below_the_threshold_are_flagged_not_dropped(self):
        out = als.score_prospects(self.P, self._ctx(disqualify_below=99), today=date(2026, 8, 12))
        assert len(out) == 1, "must not delete the prospect"
        assert out[0]["disqualified"] is True

    def test_prospects_above_the_threshold_are_flagged_false(self):
        out = als.score_prospects(self.P, self._ctx(disqualify_below=1), today=date(2026, 8, 12))
        assert out[0]["disqualified"] is False

    def test_no_threshold_means_no_flag_at_all(self):
        out = als.score_prospects(self.P, self._ctx(), today=date(2026, 8, 12))
        assert "disqualified" not in out[0]

    def test_junk_threshold_does_not_flag(self):
        out = als.score_prospects(self.P, self._ctx(disqualify_below="soon"), today=date(2026, 8, 12))
        assert "disqualified" not in out[0]


class TestTimingFieldIsConfigDerived:
    """`calculate_pipeline` used to read one hardcoded field name, `estimated_timeline`,
    so 30 of the 100 available points were unreachable for any skill whose timing signal
    is called something else — silently, with no error anywhere."""

    def test_derives_timing_candidates_from_the_authored_fields(self):
        got = als.timing_fields_from_authored(
            ["company_name", "permit_type", "permit_date", "square_footage"]
        )
        assert got == ("permit_date",)

    def test_no_date_like_field_derives_nothing(self):
        assert als.timing_fields_from_authored(["company_name", "industry"]) == ()

    def test_a_skill_using_permit_date_now_scores_the_timing_axis(self):
        ctx = {
            "organization": {"name": "Lee Company", "markets": TN_MARKETS, "sales_cycle_months": 4},
            "sources": {"s": {"fields": ["company_name", "permit_date"]}},
            "scoring": {},
        }
        prospects = [{"id": "p1", "company_name": "X", "city": "Nashville", "state": "TN",
                      "discovery_data": {"by_source": {"s": {"permit_date": "2027-06"}}}}]
        out = als.score_prospects(prospects, ctx, today=date(2026, 8, 12))[0]
        assert out["score_factors"]["pipeline_timing"] == 30, "was 0 before: wrong field name"
        assert out["months_to_decision"] == 6

    def test_an_explicit_config_declaration_wins_over_derivation(self):
        cfg = {**als._DEFAULT_SCORING["pipeline"], "timing_fields": ("replacement_due",)}
        out = als.calculate_pipeline({"replacement_due": "2027-06"}, cfg, date(2026, 8, 12))
        assert out["months_to_decision"] is not None


class TestPipelineAbstains:
    def test_no_timing_evidence_seeds_no_sales_stage_and_no_points(self):
        """`pipeline_status` drives operator kanbans in AEO — inventing one puts every
        prospect in a column nobody moved it to, and 10 free points made a real run's
        scores cluster at 11-12 regardless of the prospect."""
        result = als.calculate_pipeline({}, als._DEFAULT_SCORING["pipeline"], date(2026, 8, 12))
        assert result["pipeline_status"] is None
        assert result["score"] == 0
