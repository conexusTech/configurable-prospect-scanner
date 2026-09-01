"""The gated floor-plus-bonus model, and the invariants that make it auditable.

🔴 **The first test in this file is the one that matters most.** Five live skills are
scored by the legacy additive sum, and the gated model is opt-in. "Opt-in" is a claim
about a branch nobody re-reads; it is asserted here instead.

The §7 comparison lives in the verification harness, not here — a test that pins 24 real
company names to 24 numbers breaks on any data refresh and teaches nothing. What is pinned
here is the *arithmetic and the invariants*, which is what must not drift.
"""
from datetime import date

import pytest

import av_lead_scanner as als
from aeo.gated_score import FORBIDDEN_BAND, age_months, score, select_signal

TODAY = date(2026, 8, 27)

CFG = {
    "model": "gated",
    "floor": 80,
    "score_cap": 100,
    "gate": {
        "target_market": {
            "state_field": "state",
            "allowed_states": ["North Carolina"],
            "state_aliases": {"north carolina": "NC"},
            "exclude_rules": ["school", "government"],
        },
        "buying_window": {
            "window_stages": ["1 - Early Discovery", "4 - Active Pursuit"],
            "signal_freshness_months": 18,
        },
        "signal_freshness_months": 18,
    },
    "bonus": {
        "max": 20,
        "signal_strength": {"max": 8, "classes": {
            "rfp_active": 8, "broker_carrier_change": 6, "benefits_change": 5,
            "dissatisfaction": 4, "corporate_event": 2, "workforce_change": 2,
            "leadership_change": 1}},
        "company_size": {"field": "employee_count", "max": 4,
                         "tiers": [[400, 4], [200, 3], [100, 2], [50, 1]], "unknown": 2},
        "confirmed_contact": {"max": 4,
                              "rules": [["contact_name", 2], ["contact_email", 1],
                                        ["contact_phone", 1]]},
        "signal_recency": {"max": 4, "months": [[1, 4], [3, 3], [6, 2], [12, 1]]},
    },
    "partial": {
        "target_market_only": {"base": 25, "ceiling": 45},
        "signal_only": {"base": 10, "ceiling": 30},
        "neither": {"base": 0, "ceiling": 15},
    },
}
ALIASES = {"north carolina": "NC"}

NC = {"state": "NC", "employee_count": "250", "contact_name": "A", "contact_email": "a@b.c"}
FRESH_RFP = [{"signal_type": "rfp activity", "signal_class": "rfp_active",
              "signal_date": "2026-08-01"}]


def run(lead, signals, stage, cfg=None):
    return score(lead, signals, stage, cfg or CFG, ALIASES, TODAY)


class TestOptInIsRealNotClaimed:
    """The five live skills must not move until a config says so."""

    def test_no_model_key_means_the_legacy_sum_runs(self):
        assert als._gated_total({}, {}, {}, {"score_cap": 100}, 100, TODAY) is None

    @pytest.mark.parametrize("value", ["legacy", "", None, "GATED_", "additive"])
    def test_only_the_exact_word_gated_opts_in(self, value):
        assert (
            als._gated_total({}, {}, {}, {"model": value, "score_cap": 100}, 100, TODAY)
            is None
        )

    @pytest.mark.parametrize("value", ["gated", "GATED", " Gated "])
    def test_the_word_gated_opts_in_regardless_of_case_or_padding(self, value):
        got = als._gated_total(
            NC, {"validation_data": {"switching_signal": FRESH_RFP}},
            {"pipeline_status": "4 - Active Pursuit"},
            {**CFG, "model": value}, 100, TODAY,
        )
        assert got is not None


class TestTheFloor:
    def test_clearing_both_gates_lands_at_or_above_the_floor(self):
        out = run(NC, FRESH_RFP, "4 - Active Pursuit")
        assert out["lane"] == "qualified"
        assert out["total"] >= 80

    def test_a_bare_qualified_lead_still_clears_80(self):
        # The whole point: qualification is worth something ON ITS OWN. Under the legacy
        # sum this lead scored from zero and had to earn its way up from five axes that
        # do not measure whether it is qualified.
        bare = {"state": "NC"}
        out = run(bare, [{"signal_type": "?", "signal_date": "2026-08-01"}],
                  "4 - Active Pursuit")
        assert out["total"] >= 80

    def test_nothing_can_drag_a_qualified_lead_below_the_floor(self):
        # Every band is non-negative and ai_adjustment does not enter the total.
        for lead in ({"state": "NC"}, NC, {**NC, "employee_count": "1"}):
            out = run(lead, FRESH_RFP, "4 - Active Pursuit")
            assert out["total"] >= 80


class TestTheGate:
    def test_wrong_state_is_signal_only(self):
        out = run({**NC, "state": "VA"}, FRESH_RFP, "4 - Active Pursuit")
        assert out["lane"] == "signal_only" and out["total"] <= 30

    def test_a_disqualifier_closes_G1_even_in_the_right_state(self):
        out = run({**NC, "company_name": "Wake County School District"}, FRESH_RFP,
                  "4 - Active Pursuit")
        assert out["gates"]["target_market"] is False

    def test_a_stage_outside_the_window_is_nurture(self):
        out = run(NC, FRESH_RFP, "7 - Too Late")
        assert out["lane"] == "target_market_only" and out["total"] <= 45

    def test_ANY_fresh_signal_opens_G2_not_the_strongest(self):
        # 🔴 The mirror-image bug: a 31-month-old RFP beside a one-week-old broker change
        # would fail the gate outright if the SELECTOR decided admission. Filter first.
        signals = [
            {"signal_class": "rfp_active", "signal_date": "2024-01-01"},      # stale, strong
            {"signal_class": "broker_carrier_change", "signal_date": "2026-08-20"},  # fresh
        ]
        assert run(NC, signals, "4 - Active Pursuit")["gates"]["buying_window"] is True

    def test_an_empty_allow_list_is_a_dead_gate_that_fails_CLOSED(self):
        # Strictly worse than the dead bonus it replaces, which is why the config lint
        # must refuse it. Pinned so the behaviour is not mistaken for an accident.
        cfg = {**CFG, "gate": {**CFG["gate"],
                               "target_market": {"state_field": "state", "allowed_states": []}}}
        assert run(NC, FRESH_RFP, "4 - Active Pursuit", cfg)["gates"]["target_market"] is False


class TestBoundaryAsymmetry:
    def test_the_gate_admits_STRICTLY_under_18_months(self):
        # Smart Wires sits exactly on this boundary at 18 whole months.
        at_18 = [{"signal_class": "dissatisfaction", "signal_date": "2025-02-27"}]
        assert age_months("2025-02-27", TODAY) == 18
        assert run(NC, at_18, "4 - Active Pursuit")["gates"]["buying_window"] is False

    def test_just_inside_the_boundary_admits(self):
        at_17 = [{"signal_class": "dissatisfaction", "signal_date": "2025-03-27"}]
        assert age_months("2025-03-27", TODAY) == 17
        assert run(NC, at_17, "4 - Active Pursuit")["gates"]["buying_window"] is True

    def test_recency_awards_on_INCLUSIVE_months(self):
        # Strict about admitting, generous about crediting — deliberate, and the two
        # must not be unified.
        out = run(NC, [{"signal_class": "rfp_active", "signal_date": "2026-05-27"}],
                  "4 - Active Pursuit")
        assert age_months("2026-05-27", TODAY) == 3
        assert out["bands"]["signal_recency"] == 3

    def test_a_future_date_clamps_to_zero_not_negative(self):
        assert age_months("2026-12-01", TODAY) == 0

    def test_a_partial_date_resolves_to_the_EARLIEST_instant(self):
        # It can only age a lead, so an imprecise date can close the gate but never open
        # one. `2026` is 2026-01-01, not mid-year.
        assert age_months("2026", TODAY) == age_months("2026-01-01", TODAY) == 7
        assert age_months("2026-08", TODAY) == age_months("2026-08-01", TODAY) == 0


class TestBands:
    def test_an_unrecognised_class_scores_the_MIDPOINT_never_zero(self):
        # "We could not classify this" is not evidence of weakness. Scoring it 0 bends a
        # data rule to protect the exactly-80 invariant, which was dropped instead.
        out = run(NC, [{"signal_class": None, "signal_date": "2026-08-01"}],
                  "4 - Active Pursuit")
        assert out["bands"]["signal_strength"] == 4

    def test_unknown_size_scores_the_MIDPOINT_never_zero(self):
        # Revision 1 treated "we don't know" as "under 50 employees" — data hygiene
        # leaking into the score, on 7 of the 17 qualified leads.
        out = run({"state": "NC"}, FRESH_RFP, "4 - Active Pursuit")
        assert out["bands"]["company_size"] == 2

    def test_no_signal_at_all_scores_zero_strength_not_the_midpoint(self):
        # A real absence is different from an unusable class, and conflating them would
        # hand free points to a lead carrying no evidence.
        out = run(NC, [], "4 - Active Pursuit")
        assert out["bands"]["signal_strength"] == 0


class TestStrengthMatchesTheSignalTypeToo:
    """The band must work for a vertical the classifier's enum was not written for.

    `SIGNAL_CLASSES` is a closed seven-value enum in the EAP/benefits vocabulary. Measured
    on real books: consulting (property development) classifies 37 of 46 signals to
    `None`, and MYgroup — whose vertical the enum WAS written for — had 196 of 204 signals
    fall to the midpoint because its config keyed the band on names absent from the enum.
    Both are the same defect wearing different clothes: a band that reads as configured
    and pays a flat midpoint.
    """

    CFG = {"max": 8, "classes": {"rfp_active": 8, "groundbreaking_announcement": 6}}

    def test_a_config_class_matching_the_signal_type_is_honoured(self):
        from aeo.gated_score import band_signal_strength

        assert band_signal_strength({"signal_type": "groundbreaking_announcement"}, self.CFG) == 6

    def test_underscores_and_spaces_are_the_same_signal(self):
        # The model writes "groundbreaking announcement"; a config declares the
        # underscored form. An underscore must not decide a score — the same reasoning
        # that put `normalize` in `signal_class`.
        from aeo.gated_score import band_signal_strength

        assert band_signal_strength({"signal_type": "groundbreaking announcement"}, self.CFG) == 6

    def test_the_canonical_class_still_wins_over_the_type(self):
        # Backward compatibility, and the reason this can only ever raise a midpoint to a
        # weight the config asked for: every existing config keeps its exact behaviour.
        from aeo.gated_score import band_signal_strength

        sig = {"signal_class": "rfp_active", "signal_type": "groundbreaking_announcement"}
        assert band_signal_strength(sig, self.CFG) == 8

    def test_an_empty_class_key_never_matches_an_unclassified_signal(self):
        """A pre-existing hole, found by the audit pass rather than by a failure.

        `signal_class` is "" for every row written before it was attached at enrichment
        (2026-08-31). An empty key in a config would match all of them through the
        `cls in classes` lookup and pay its weight to signals that were never classified
        at all — and to nothing else, so it would look like a working band.
        """
        from aeo.gated_score import band_signal_strength

        cfg = {"max": 8, "classes": {"": 7, "rfp_active": 8}}
        assert band_signal_strength({"signal_type": ""}, cfg) == 4  # midpoint, not 7
        assert band_signal_strength({"signal_type": "rfp_active"}, cfg) == 8
        assert band_signal_strength({"signal_class": "rfp_active"}, cfg) == 8

    def test_a_type_the_config_never_named_still_scores_the_midpoint(self):
        # The discriminator. Without this the fallback could match anything and the two
        # tests above would pass on a band that pays 6 for every signal on earth.
        from aeo.gated_score import band_signal_strength

        assert band_signal_strength({"signal_type": "zoning_approval"}, self.CFG) == 4
        assert band_signal_strength({"signal_type": ""}, self.CFG) == 4

    def test_contact_points_are_additive_and_capped(self):
        full = {**NC, "contact_phone": "1"}
        assert run(full, FRESH_RFP, "4 - Active Pursuit")["bands"]["confirmed_contact"] == 4


class TestSelection:
    def test_the_strongest_AMONG_THE_FRESH_is_selected(self):
        signals = [
            {"signal_class": "rfp_active", "signal_date": "2024-01-01"},
            {"signal_class": "leadership_change", "signal_date": "2026-08-20"},
        ]
        sel = select_signal(signals, CFG["bonus"]["signal_strength"]["classes"], 18, TODAY)
        assert sel["signal_class"] == "leadership_change"

    def test_with_nothing_fresh_the_strongest_OVERALL_is_described(self):
        # So a gated-out lead's analysis still names its best evidence and says it is
        # stale. Scoring stale evidence as 0 makes every no-fresh lead indistinguishable.
        signals = [
            {"signal_class": "rfp_active", "signal_date": "2020-01-01"},
            {"signal_class": "leadership_change", "signal_date": "2021-01-01"},
        ]
        sel = select_signal(signals, CFG["bonus"]["signal_strength"]["classes"], 18, TODAY)
        assert sel["signal_class"] == "rfp_active"

    def test_selected_from_fresh_is_reported_not_inferred(self):
        out = run(NC, [{"signal_class": "rfp_active", "signal_date": "2020-01-01"}],
                  "4 - Active Pursuit")
        assert out["selected_from_fresh"] is False


class TestInvariants:
    """The self-audit: one assertion replaces reasoning about weights."""

    def test_the_forbidden_band_is_unreachable_across_the_whole_input_space(self):
        seen = set()
        for state in ("NC", "VA"):
            for stage in ("4 - Active Pursuit", "7 - Too Late"):
                for size in (None, "10", "150", "500"):
                    for sig in ([], FRESH_RFP,
                                [{"signal_class": "leadership_change", "signal_date": "2019-01-01"}]):
                        for contacts in ({}, {"contact_name": "A"},
                                         {"contact_name": "A", "contact_email": "e",
                                          "contact_phone": "p"}):
                            lead = {"state": state, **contacts}
                            if size:
                                lead["employee_count"] = size
                            seen.add(run(lead, sig, stage)["total"])
        offenders = [t for t in seen if FORBIDDEN_BAND[0] <= t <= FORBIDDEN_BAND[1]]
        assert not offenders, f"scores landed in the structurally-empty band: {offenders}"
        assert seen, "the sweep produced no scores"

    def test_a_gated_out_lead_can_never_reach_the_floor(self):
        best_nurture = run(
            {**NC, "employee_count": "999", "contact_phone": "p"}, FRESH_RFP, "7 - Too Late"
        )
        assert best_nurture["total"] <= 45

    def test_the_neither_lane_can_only_earn_size_and_contact(self):
        # Both signal bands need a signal, so a lead with none tops out at 8 — well
        # under the 15 ceiling. The ceiling is derived, not an independent number.
        out = run({"state": "VA", "employee_count": "999", "contact_name": "A",
                   "contact_email": "e", "contact_phone": "p"}, [], "7 - Too Late")
        assert out["lane"] == "neither"
        assert out["total"] == 8


class TestBreakdownPersistence:
    """`score_factors.gated` is what step 6 reads and the renderer explains from."""

    def _scored(self, cfg_extra):
        prospect = {
            "id": "p1", "company_name": "Acme", "state": "NC",
            "validation_data": {"switching_signal": [
                {"signal_type": "rfp activity", "signal_date": "2026-08-01"}]},
            "discovery_data": {"by_source": {"s": {"employee_count": "250"}}},
            "contact_name": "A", "contact_email": "a@b.c",
            "_ai_judgment": {"pipeline_status": "4 - Active Pursuit"},
        }
        ctx = {
            "scoring": {**cfg_extra, "score_cap": 100},
            "pipeline": {"stages": [
                {"key": "4 - Active Pursuit", "min_months": 4, "max_months": 8,
                 "kind": "timing"}]},
            "skill_type": "customer",
        }
        return als.score_prospects([prospect], ctx, today=TODAY)[0]

    def test_a_legacy_row_gains_NO_gated_key(self):
        # 🔴 The shape guarantee. AEO's assessFactorHealth reads score_factors.factors;
        # a new key on the legacy path would change the shape for all five live skills.
        out = self._scored({})
        assert "gated" not in out["score_factors"]

    def test_a_gated_row_carries_the_full_breakdown(self):
        out = self._scored(CFG)
        g = out["score_factors"]["gated"]
        assert set(g) >= {"total", "lane", "gates", "bonus", "bands", "selected_from_fresh"}
        assert g["gates"] == {"target_market": True, "buying_window": True}
        assert g["lane"] == "qualified"
        assert g["total"] == out["score"] >= 80

    def test_the_legacy_axis_keys_survive_alongside_it(self):
        # Kept deliberately so a run can be compared against its own legacy scoring
        # during the cutover. They no longer sum to the total.
        out = self._scored(CFG)
        sf = out["score_factors"]
        assert "completeness" in sf and "pipeline_timing" in sf
        assert sf["gated"]["total"] != sum(
            v for k, v in sf.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        )


class TestAliasSourcing:
    """Where the gate's state normalisation comes from — D1's actual fix."""

    def test_the_gate_reads_its_OWN_alias_table(self):
        out = als._gated_total(
            {"state": "NC"},
            {"validation_data": {"switching_signal": FRESH_RFP}},
            {"pipeline_status": "4 - Active Pursuit"},
            {**CFG, "score_cap": 100}, 100, TODAY,
        )
        assert out[1]["gates"]["target_market"] is True

    def test_it_falls_back_to_region_bonus_aliases(self):
        # Every live skill has the table there today, and a gate that fails CLOSED is
        # the worst failure this model has — so the fallback is deliberate.
        cfg = {**CFG, "gate": {**CFG["gate"], "target_market": {
            "state_field": "state", "allowed_states": ["North Carolina"]}},
            "region_bonus": {"state_aliases": {"north carolina": "NC"}}}
        out = als._gated_total(
            {"state": "NC"},
            {"validation_data": {"switching_signal": FRESH_RFP}},
            {"pipeline_status": "4 - Active Pursuit"},
            {**cfg, "score_cap": 100}, 100, TODAY,
        )
        assert out[1]["gates"]["target_market"] is True

    def test_no_alias_table_anywhere_fails_the_gate_closed(self):
        # Documented, not silent: an unresolvable allow-list admits nobody. The config
        # lint is what must catch this before a run.
        cfg = {**CFG, "gate": {**CFG["gate"], "target_market": {
            "state_field": "state", "allowed_states": ["North Carolina"]}}}
        out = als._gated_total(
            {"state": "NC"},
            {"validation_data": {"switching_signal": FRESH_RFP}},
            {"pipeline_status": "4 - Active Pursuit"},
            {**cfg, "score_cap": 100}, 100, TODAY,
        )
        assert out[1]["gates"]["target_market"] is False


class TestRankIsReproducible:
    """Step 4: `rank` is where call order lives, so it must not depend on discovery order.

    🔴 The defect this replaces: `sort(key=score)` alone. Python's sort is STABLE, so
    tied prospects kept whatever order discovery produced — which changes between runs of
    the same data. Measured on run 741b7b3b: 4 ties covering 8 of 24 prospects.

    It gets worse under the gated model, not better: the qualified set compresses into a
    20-point band by design, and 17 prospects cannot be pairwise separated in 20 points.
    """

    def _prospect(self, name, strength, recency, size, contact, score=90):
        return {
            "company_name": name,
            "score": score,
            "score_factors": {"gated": {"bands": {
                "signal_strength": strength, "signal_recency": recency,
                "company_size": size, "confirmed_contact": contact}}},
        }

    def _ranked(self, items):
        out = list(items)
        out.sort(key=als._rank_key, reverse=True)
        return [i["company_name"] for i in out]

    def test_shuffling_the_input_does_not_change_the_order(self):
        # The whole property. Four prospects tied on score, separable only by the cascade.
        items = [
            self._prospect("Alpha", 8, 4, 2, 2),
            self._prospect("Bravo", 8, 4, 2, 2),   # identical to Alpha except the name
            self._prospect("Charlie", 8, 3, 4, 4),
            self._prospect("Delta", 4, 4, 4, 4),
        ]
        first = self._ranked(items)
        for rotation in range(1, len(items)):
            shuffled = items[rotation:] + items[:rotation]
            assert self._ranked(shuffled) == first, f"order changed at rotation {rotation}"

    def test_the_cascade_prefers_strength_then_recency_then_size_then_contact(self):
        assert self._ranked([
            self._prospect("weak-strength", 4, 4, 4, 4),
            self._prospect("strong", 8, 0, 0, 0),
        ]) == ["strong", "weak-strength"]
        assert self._ranked([
            self._prospect("older", 8, 1, 4, 4),
            self._prospect("fresher", 8, 4, 0, 0),
        ]) == ["fresher", "older"]
        assert self._ranked([
            self._prospect("smaller", 8, 4, 1, 4),
            self._prospect("bigger", 8, 4, 4, 0),
        ]) == ["bigger", "smaller"]
        assert self._ranked([
            self._prospect("unreachable", 8, 4, 4, 0),
            self._prospect("reachable", 8, 4, 4, 4),
        ]) == ["reachable", "unreachable"]

    def test_the_final_tiebreak_reads_alphabetically(self):
        # Descending sort with an ASCENDING name, so two identical prospects appear in
        # an order a user can explain.
        identical = [self._prospect(n, 8, 4, 4, 4) for n in ("Zulu", "Alpha", "Mike")]
        assert self._ranked(identical) == ["Alpha", "Mike", "Zulu"]

    def test_score_still_dominates_every_other_term(self):
        assert self._ranked([
            self._prospect("low-score-perfect-bands", 8, 4, 4, 4, score=81),
            self._prospect("high-score-no-bands", 0, 0, 0, 0, score=98),
        ]) == ["high-score-no-bands", "low-score-perfect-bands"]

    def test_a_legacy_row_with_no_breakdown_still_ranks_totally(self):
        # Under the legacy model the band terms are all absent, collapsing the cascade to
        # score -> name. Still total, still reproducible, and unchanged in ordering for
        # anything that was not already tied.
        legacy = [{"company_name": n, "score": 50} for n in ("Zulu", "Alpha", "Mike")]
        assert self._ranked(legacy) == ["Alpha", "Mike", "Zulu"]
        assert self._ranked(legacy[::-1]) == ["Alpha", "Mike", "Zulu"]
