"""Neutrality gate: a DIFFERENT vertical, the same primitives, no engine change.

Phase 6.3 of the parity work, and the invariant it enforces is I6: **a primitive exercised
only by the EAP fixture is EAP-shaped and fails review.** So this file re-expresses a real
production skill — `commercial-flooring-prospect-scanner`, live, 7 authored factors — using
every primitive the parity work added, and asserts the engine behaves for it.

The field vocabulary below is that skill's ACTUAL collected fields, read from its live
config: `square_footage`, `ownership_status`, `property_type`, `trigger_type`,
`trigger_date`, `transaction_type`, `transaction_date`, `managed_property_types`,
`portfolio_size`, `related_property_owner`, `firm_type`. Nothing here is invented to fit.

The point is not merely that it runs. **Two of its seven live factors are provably wrong
today**, and both are wrong in ways only a graded table can fix:

* `managed_property_types` carries `min: 2, max: 5` and a description promising
  "managing at least 2 types earns partial credit; managing 5 or more earns full credit".
  Credit was binary, so partial credit was impossible — and `max: 5` INVERTED it: a firm
  managing 8 types tripped the bound and scored **zero**, while one managing 4 scored full.
* `property_type` promises "matches qualifying commercial categories … rather than
  disqualified types". Presence cannot tell those apart, so a disqualified type earned
  full credit.

So this vertical is not a second copy of the first test. It is the case that proves the
primitives were not built to flatter one config.
"""
from __future__ import annotations

from datetime import date

import av_lead_scanner as als

TODAY = date(2026, 7, 20)

# ── the flooring vertical, in the new vocabulary ──────────────────────────────

#: Bigger IS better here — unlike the other vertical's mid-peaking size band. Same
#: primitive, opposite shape, which is the neutrality point in one table.
FOOTAGE_TIERS = [
    {"threshold": 250_000, "points": 20},
    {"threshold": 100_000, "points": 18},
    {"threshold": 25_000, "points": 14},
    {"threshold": 10_000, "points": 10},
    {"threshold": 0, "points": 0},
]

#: The promise the live factor's own description makes and could not keep.
MANAGED_TYPES_TIERS = [
    {"threshold": 5, "points": 10},
    {"threshold": 2, "points": 5},
    {"threshold": 0, "points": 0},
]

OWNERSHIP_KEYWORDS = {"owner-occupied": 15, "owns": 15, "owned": 15, "leases": 4, "tenant": 4}

#: Qualifying categories score; disqualified ones score nothing. Expressible only because
#: `keywords` grades — presence gave a warehouse the same credit as a hospital.
PROPERTY_TYPE_KEYWORDS = {
    "healthcare": 10, "medical office": 10, "office": 9, "retail": 8,
    "hospitality": 8, "education": 8, "multifamily": 6,
    "warehouse": 0, "industrial storage": 0, "single-family": 0,
}

FACTORS = [
    {"key": "square_footage", "name": "Square Footage", "weight": 20,
     "source_field": "square_footage", "tiers": FOOTAGE_TIERS},
    {"key": "ownership_status", "name": "Ownership", "weight": 15,
     "source_field": "ownership_status", "keywords": OWNERSHIP_KEYWORDS},
    {"key": "property_type", "name": "Property Type", "weight": 10,
     "source_field": "property_type", "keywords": PROPERTY_TYPE_KEYWORDS},
    # Event mode over SIBLING flat fields — `trigger_type` and `trigger_date` are two
    # discovery fields side by side, not lane rows.
    {"key": "trigger_type", "name": "In-Market Trigger", "weight": 15,
     "source_field": "trigger_type", "base_points": 8, "bonus_points": 7,
     "bonus_keywords": ["permit", "relocation", "build-out"],
     "date_field": "trigger_date", "recency_months": 18},
    {"key": "transaction_type", "name": "CRE Transaction", "weight": 10,
     "source_field": "transaction_type", "base_points": 6, "bonus_points": 4,
     "bonus_keywords": ["acquisition", "sale"],
     "date_field": "transaction_date", "recency_months": 12},
    {"key": "managed_property_types", "name": "Managed Portfolio", "weight": 10,
     "source_field": ["managed_property_types", "portfolio_size"],
     "tiers": MANAGED_TYPES_TIERS},
    # Presence is genuinely right here: a referral contact either resolves to an owner or
    # it does not. Left ungraded ON PURPOSE, to prove presence survives as the default.
    {"key": "related_property_owner", "name": "Referral Owner", "weight": 10,
     "source_field": "related_property_owner"},
]

BANDS = [
    {"range": [75, 100], "label": "Hot", "action": "Quote now"},
    {"range": [50, 74], "label": "Warm", "action": "Qualify"},
    {"range": [25, 49], "label": "Cool", "action": "Nurture"},
    {"range": [0, 24], "label": "Cold", "action": "Park"},
]

SCORING = {
    "score_cap": 100,
    "factors_max": 90,
    # This vertical DOES care about geography — the opposite of the other one, which
    # forbids it. Same knob, opposite setting, no engine change.
    "region_bonus": {"max": 10},
    "completeness": {"max": 0},
    "fit": {"max": 0},
    "multi_source": {"max": 0},
    "pipeline": {"max": 0},
    "priority_bands": BANDS,
    "disqualify_rules": [
        {"key": "too_small", "source_field": "square_footage", "below": 10_000,
         "reason": "Below the 10,000 sq ft minimum for a commercial flooring project"},
        {"key": "wrong_asset", "source_field": "property_type",
         "keywords": ["single-family", "warehouse shell"],
         "reason": "Asset class outside the commercial flooring ICP"},
    ],
    "factors": FACTORS,
}

CONTEXT = {
    "organization": {"name": "Flooring Co", "markets": ["Nashville, TN"]},
    "scoring": SCORING,
}


def _score(prospect: dict) -> dict:
    return als.score_prospects([prospect], CONTEXT, today=TODAY)[0]


def _credit(prospect: dict, key: str) -> float:
    return _score(prospect)["score_factors"]["factors"][key]


BASE = {
    "id": "p", "company_name": "Meridian Property Group",
    "city": "Nashville", "state": "TN",
    "square_footage": "120,000 sq ft", "ownership_status": "owner-occupied",
    "property_type": "Medical office", "related_property_owner": "Dana Reyes",
}


class TestTheTwoBrokenLiveFactorsAreNowCorrect:
    def test_managed_portfolio_earns_partial_credit_as_its_description_promises(self):
        # Was impossible: credit was binary, so "partial" could not be expressed.
        assert _credit({**BASE, "managed_property_types": "3"}, "managed_property_types") == 0.5

    def test_managing_more_no_longer_earns_less(self):
        """The inversion. `max: 5` made 8 score ZERO and 4 score full."""
        assert _credit({**BASE, "managed_property_types": "8"}, "managed_property_types") == 1.0
        assert _credit({**BASE, "managed_property_types": "6"}, "managed_property_types") == 1.0
        assert _credit({**BASE, "managed_property_types": "1"}, "managed_property_types") == 0.0

    def test_a_disqualified_property_type_no_longer_earns_full_credit(self):
        """Presence could not distinguish "qualifying" from "disqualified"."""
        assert _credit({**BASE, "property_type": "Healthcare"}, "property_type") == 1.0
        assert _credit({**BASE, "property_type": "Warehouse"}, "property_type") == 0.0
        # And a mid-tier category lands in between, which presence also could not do.
        assert _credit({**BASE, "property_type": "Multifamily"}, "property_type") == 0.6


class TestEveryPrimitiveIsExercisedByThisVertical:
    """I6: a primitive only the first fixture uses is shaped by that fixture."""

    def test_numeric_tiers_with_the_OPPOSITE_curve_to_the_other_vertical(self):
        # Bigger is better here; the other vertical peaks in the middle. One primitive.
        assert _credit({**BASE, "square_footage": "300,000"}, "square_footage") == 1.0
        assert _credit({**BASE, "square_footage": "120,000"}, "square_footage") == 0.9
        assert _credit({**BASE, "square_footage": "30,000"}, "square_footage") == 0.7
        assert _credit({**BASE, "square_footage": "12,000"}, "square_footage") == 0.5

    def test_keyword_map_on_a_status_field(self):
        assert _credit({**BASE, "ownership_status": "Owns the building"}, "ownership_status") == 1.0
        assert _credit({**BASE, "ownership_status": "Leases from a REIT"}, "ownership_status") == round(4 / 15, 3)

    def test_event_mode_over_SIBLING_FLAT_FIELDS_not_lane_rows(self):
        """The gap this vertical exposed.

        `trigger_type` and `trigger_date` are two discovery fields side by side. Before
        the sibling-field fallback, the date was unreachable, so every such config
        degraded silently to "undated" — base credit, never the bonus, recency never
        applied, and nothing said so.
        """
        current = {**BASE, "trigger_type": "Commercial building permit filed",
                   "trigger_date": "2026-02-20"}
        assert _credit(current, "trigger_type") == 1.0          # in window + bonus word
        stale = {**BASE, "trigger_type": "Commercial building permit filed",
                 "trigger_date": "2019-12-01"}
        assert _credit(stale, "trigger_type") == 0.0            # 18-month window
        undated = {**BASE, "trigger_type": "Commercial building permit filed"}
        assert _credit(undated, "trigger_type") == round(8 / 15, 3)  # base, never the bonus

    def test_a_second_event_factor_with_a_different_window_and_bonus_set(self):
        assert _credit(
            {**BASE, "transaction_type": "Building acquisition", "transaction_date": "2026-05-01"},
            "transaction_type",
        ) == 1.0
        # Inside the trigger factor's 18 months but outside this one's 12.
        assert _credit(
            {**BASE, "transaction_type": "Building acquisition", "transaction_date": "2025-01-01"},
            "transaction_type",
        ) == 0.0

    def test_a_source_field_list_falls_through(self):
        assert _credit({**BASE, "portfolio_size": "6"}, "managed_property_types") == 1.0

    def test_presence_survives_as_the_default_for_a_factor_that_wants_it(self):
        assert _credit(BASE, "related_property_owner") == 1.0
        assert _credit({**BASE, "related_property_owner": ""}, "related_property_owner") == 0.0

    def test_hard_rules_and_bands_work_for_this_vertical_too(self):
        small = _score({**BASE, "square_footage": "4,000"})
        assert small["score"] == 0
        assert small["priority_band"] == "Cold"
        assert "10,000 sq ft minimum" in small["disqualifier_reason"]

        wrong = _score({**BASE, "property_type": "Single-family rental"})
        assert wrong["disqualified"] is True
        assert "outside the commercial flooring ICP" in wrong["disqualifier_reason"]

    def test_this_vertical_KEEPS_the_geography_axis_the_other_one_forbids(self):
        """Same knob, opposite setting, no engine change — the neutrality point."""
        local = _score(BASE)
        assert local["score_factors"]["region_bonus"] == 10
        away = _score({**BASE, "city": "Boise", "state": "ID"})
        assert away["score_factors"]["region_bonus"] == 0


class TestAFullyQualifiedProspectScoresCoherently:
    def test_the_best_case_reaches_the_top_band(self):
        best = {
            **BASE,
            "square_footage": "300,000",
            "ownership_status": "owner-occupied",
            "property_type": "Healthcare",
            "trigger_type": "Commercial building permit filed",
            "trigger_date": "2026-03-01",
            "transaction_type": "Building acquisition",
            "transaction_date": "2026-04-01",
            "managed_property_types": "7",
        }
        item = _score(best)
        # 90 authored + 10 region = 100.
        assert item["score"] == 100
        assert item["priority_band"] == "Hot"
        # No `disqualified` key at all: no rule fired and this config authors no score
        # floor, so there is nothing to report. A `False` here would be a key the config
        # never asked for — see the gateway's COALESCE, which relies on absence.
        assert "disqualified" not in item
        assert all(v == 1.0 for v in item["score_factors"]["factors"].values())

    def test_a_thin_but_valid_prospect_lands_mid_band_rather_than_at_an_extreme(self):
        """The failure this whole axis rework exists to prevent: every prospect the same.

        A real run once scored 11-12 for everything regardless of the prospect. A thin
        record must land BELOW a strong one and ABOVE a disqualified one, on its own
        evidence.
        """
        thin = {"id": "t", "company_name": "Thin Co", "city": "Boise", "state": "ID",
                "square_footage": "15,000", "property_type": "Retail"}
        strong = _score({**BASE, "square_footage": "300,000",
                         "managed_property_types": "7",
                         "trigger_type": "Permit", "trigger_date": "2026-03-01"})
        disqualified = _score({**BASE, "square_footage": "4,000"})
        item = _score(thin)
        # Ordered on its own evidence: above the ruled-out row, below the strong one.
        assert disqualified["score"] == 0 < item["score"] < strong["score"]
        # 15,000 sq ft (10/20) + Retail (8/10) and nothing else = 18 of 90, out of market.
        # That IS the bottom band, and asserting a friendlier one would have been me
        # deciding the answer rather than reading it.
        assert item["priority_band"] == "Cold"
        assert strong["priority_band"] != item["priority_band"]
