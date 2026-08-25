"""Graded factor tables: `tiers` (numeric) and `keywords` (text).

Phase 2 of the EAP-parity work. Presence-only credit could not express what several live
factor descriptions already promise, and `max` made one of them actively inverted:

* A real production factor reads *"managing at least 2 types earns partial credit;
  managing 5 or more earns full credit"* while carrying `min: 2, max: 5`. Credit was
  binary, so partial credit was impossible — and a firm managing **8** types tripped the
  upper bound and scored **0**. Managing more earned less.
* Another reads *"matches qualifying commercial categories rather than disqualified
  types"*. Presence cannot tell those apart, so a disqualified type earned full credit.

The mode is derived from which table is authored, never from a separate `type` field, so a
table cannot be written and then silently ignored.

Scoring model, validated against a real expected output before it was built: a factor's
`weight` is its share of the axis and the table says how much of that share the value
earned — `credit = matched_points / max(points in table)`. That makes an existing config's
per-factor `max_points` map straight onto `weight`.
"""
from __future__ import annotations

from datetime import date

import av_lead_scanner as als

TODAY = date(2026, 7, 6)

#: A non-monotonic sweet-spot curve: the peak is in the MIDDLE, and very large
#: organizations score lower than mid-sized ones because they trend to national contracts.
SIZE_TIERS = [
    {"threshold": 5000, "points": 10},
    {"threshold": 1000, "points": 18},
    {"threshold": 500, "points": 20},
    {"threshold": 200, "points": 18},
    {"threshold": 100, "points": 10},
    {"threshold": 0, "points": 0},
]

#: Authored as a LIST of {keyword, points} — one of the three accepted shapes, and the one
#: that carries the substring hazard: "standalone" sits inside "standalone-recent".
INCUMBENT_KEYWORDS = [
    {"keyword": "bundled", "points": 20},
    {"keyword": "none", "points": 14},
    {"keyword": "unknown", "points": 8},
    {"keyword": "standalone", "points": 6},
    {"keyword": "standalone-recent", "points": 0},
]

#: Authored as BUCKETS — several spellings sharing one score, the shape a ranked industry
#: list takes.
VERTICAL_BUCKETS = [
    {"name": "Local Government", "points": 20,
     "keywords": ["county", "municipality", "city government", "school district"]},
    {"name": "Healthcare", "points": 18,
     "keywords": ["hospital", "health system", "healthcare"]},
    {"name": "Education", "points": 16, "keywords": ["university", "college", "education"]},
    {"name": "Manufacturing", "points": 14, "keywords": ["manufacturing", "industrial"]},
    {"name": "Professional Services", "points": 10,
     "keywords": ["professional services", "consulting"]},
]


class TestNumericTiers:
    def test_the_peak_is_in_the_middle_not_at_the_top(self):
        """A monotonic implementation passes the ends and fails here."""
        assert als._tier_credit("850", SIZE_TIERS) == 1.0          # 500 band, the peak
        assert als._tier_credit("3,100", SIZE_TIERS) == 18 / 20    # 1000 band
        assert als._tier_credit("22000", SIZE_TIERS) == 10 / 20    # 5000 band, lower
        assert als._tier_credit("230", SIZE_TIERS) == 18 / 20      # 200 band
        assert als._tier_credit("150", SIZE_TIERS) == 10 / 20      # 100 band
        assert als._tier_credit("50", SIZE_TIERS) == 0.0           # 0 band, no points

    def test_reads_a_number_out_of_free_text(self):
        assert als._tier_credit("approx. 1,850 employees", SIZE_TIERS) == 18 / 20

    def test_no_number_earns_nothing(self):
        assert als._tier_credit("several hundred", SIZE_TIERS) == 0.0

    def test_below_every_threshold_earns_nothing(self):
        assert als._tier_credit("-5", SIZE_TIERS) == 0.0

    def test_a_table_awarding_no_points_earns_nothing_rather_than_dividing_by_zero(self):
        assert als._tier_credit("900", [{"threshold": 0, "points": 0}]) == 0.0

    def test_malformed_entries_are_skipped_not_fatal(self):
        tiers = [{"threshold": "x", "points": 9}, {"points": 5}, {"threshold": 100, "points": 10}]
        assert als._tier_credit("500", tiers) == 1.0


class TestKeywordMap:
    def test_longest_match_wins_so_a_substring_cannot_shadow(self):
        """The hazard the source config warned about, in both orderings.

        `standalone-recent` contains `standalone`, which scores 6. Order-dependent
        matching returns the WRONG, HIGHER score — and reordering the table would appear
        to fix it, which is how this stays broken.
        """
        assert als._keyword_credit("standalone-recent", INCUMBENT_KEYWORDS) == 0.0
        assert als._keyword_credit("standalone", INCUMBENT_KEYWORDS) == 6 / 20
        # Same table, reversed. Order must not matter.
        assert als._keyword_credit("standalone-recent", list(reversed(INCUMBENT_KEYWORDS))) == 0.0

    def test_scores_each_incumbent_class_on_its_own_points(self):
        assert als._keyword_credit("bundled with carrier", INCUMBENT_KEYWORDS) == 1.0
        assert als._keyword_credit("none found", INCUMBENT_KEYWORDS) == 14 / 20
        assert als._keyword_credit("unknown", INCUMBENT_KEYWORDS) == 8 / 20

    def test_buckets_share_one_score_across_several_spellings(self):
        for text in ("Hall County", "Municipality of X", "Fulton School District"):
            assert als._keyword_credit(text, VERTICAL_BUCKETS) == 1.0
        assert als._keyword_credit("Healthcare Systems", VERTICAL_BUCKETS) == 18 / 20
        assert als._keyword_credit("Manufacturing", VERTICAL_BUCKETS) == 14 / 20
        assert als._keyword_credit("Professional Services", VERTICAL_BUCKETS) == 10 / 20

    def test_matching_is_case_insensitive(self):
        assert als._keyword_credit("HOSPITAL NETWORK", VERTICAL_BUCKETS) == 18 / 20

    def test_no_match_earns_nothing(self):
        assert als._keyword_credit("Cryptocurrency Exchange", VERTICAL_BUCKETS) == 0.0

    def test_the_concise_dict_shape_works_too(self):
        assert als._keyword_credit("bundled", {"bundled": 20, "standalone": 6}) == 1.0


class TestKeywordTableShapes:
    def test_all_three_authored_shapes_normalize_to_the_same_table(self):
        as_dict = als.normalize_keyword_table({"county": 20, "hospital": 18})
        as_list = als.normalize_keyword_table(
            [{"keyword": "county", "points": 20}, {"keyword": "hospital", "points": 18}]
        )
        as_buckets = als.normalize_keyword_table(
            [{"name": "Gov", "points": 20, "keywords": ["county"]},
             {"name": "Health", "points": 18, "keywords": ["hospital"]}]
        )
        assert sorted(as_dict) == sorted(as_list) == sorted(as_buckets)

    def test_fit_score_is_accepted_as_a_points_alias(self):
        """A ranked industry list names its score `fit_score`, not `points`."""
        assert als.normalize_keyword_table(
            [{"name": "Gov", "fit_score": 20, "keywords": ["county"]}]
        ) == [("county", 20.0)]

    def test_blank_and_unparseable_entries_are_dropped(self):
        assert als.normalize_keyword_table(
            {"": 5, "  ": 5, "county": "not-a-number", "city": 12}
        ) == [("city", 12.0)]

    def test_a_non_table_is_empty_rather_than_an_error(self):
        assert als.normalize_keyword_table(None) == []
        assert als.normalize_keyword_table("county") == []


class TestGradedFactorsEndToEnd:
    """Through `score_prospects`, so a change that scores right but reports wrong fails."""

    #: The five factors of a real EAP config, ported to our shape: each `max_points`
    #: becomes a `weight`, each table stays as authored.
    FACTORS = [
        {"key": "size_fit", "name": "Size Fit", "weight": 20,
         "source_field": ["estimated_headcount", "employee_estimate"], "tiers": SIZE_TIERS},
        {"key": "vertical_fit", "name": "Vertical Fit", "weight": 20,
         "source_field": ["industry", "entity_type"], "keywords": VERTICAL_BUCKETS},
        {"key": "in_market_signals", "name": "In-Market Signals", "weight": 25,
         "source_field": "signal_type",
         "keywords": {"rfp": 25, "renewal": 10, "hr leadership posting": 10,
                      "wellbeing announcement": 10}},
        {"key": "incumbent_status", "name": "Incumbent", "weight": 20,
         "source_field": "incumbent_type", "keywords": INCUMBENT_KEYWORDS},
        {"key": "decision_maker", "name": "Decision Maker", "weight": 15,
         "source_field": "contact_title",
         "keywords": {"vice president, human resources": 15, "human resources director": 15,
                      "benefits manager": 13, "hr manager": 8, "hr analyst": 3}},
    ]

    SCORING = {
        "score_cap": 100,
        "factors_max": 100,
        "completeness": {"max": 0},
        "fit": {"max": 0},
        "region_bonus": {"max": 0},
        "multi_source": {"max": 0},
        "pipeline": {"max": 0},
        "factors": FACTORS,
    }

    def _score(self, prospects: list[dict]) -> list[dict]:
        return als.score_prospects(prospects, {"scoring": self.SCORING}, today=TODAY)

    def test_reproduces_the_expected_totals_from_a_real_run(self):
        """The numbers are the published expected output of the source config's v2 run."""
        prospects = [
            {"organization_name": "EyeSouth Partners", "estimated_headcount": "850",
             "industry": "Healthcare Systems", "signal_type": "HR Leadership Posting",
             "incumbent_type": "standalone",
             "contact_title": "Vice President, Human Resources"},
            {"organization_name": "Hall County", "employee_estimate": "1850",
             "entity_type": "County", "signal_type": "Benefits Renewal",
             "incumbent_type": "standalone",
             "contact_title": "Human Resources Director"},
            {"organization_name": "Mueller Water Products", "estimated_headcount": "3,100",
             "industry": "Manufacturing", "signal_type": "HR Leadership Posting",
             "incumbent_type": "standalone",
             "contact_title": "Vice President, Human Resources"},
            {"organization_name": "Beazer Homes", "estimated_headcount": "1,054",
             "industry": "Professional Services", "signal_type": "Benefits Renewal",
             "incumbent_type": "standalone",
             "contact_title": "Vice President, Human Resources"},
            {"organization_name": "City of Alpharetta", "employee_estimate": "450",
             "entity_type": "Municipality", "incumbent_type": "standalone",
             "contact_title": "Human Resources Director"},
            {"organization_name": "The Mount Vernon School", "estimated_headcount": "230",
             "industry": "Education", "signal_type": "Wellbeing Announcement",
             "incumbent_type": "unknown"},
        ]
        got = {o["company_name"]: o["score"] for o in self._score(prospects)}
        assert got == {
            "EyeSouth Partners": 69,
            "Hall County": 69,
            "Mueller Water Products": 63,
            "Beazer Homes": 59,
            "City of Alpharetta": 59,
            "The Mount Vernon School": 52,
        }

    def test_the_per_factor_breakdown_shows_graded_credit_not_just_met_or_not(self):
        out = self._score([{
            "organization_name": "Mueller Water Products", "estimated_headcount": "3,100",
            "industry": "Manufacturing", "signal_type": "HR Leadership Posting",
            "incumbent_type": "standalone",
            "contact_title": "Vice President, Human Resources",
        }])
        detail = out[0]["score_factors"]["factors"]
        # Graded: three distinct fractional values. Presence mode could only emit 0 or 1.
        assert detail["size_fit"] == 0.9
        assert detail["vertical_fit"] == 0.7
        assert detail["incumbent_status"] == 0.3
        assert detail["decision_maker"] == 1.0

    def test_a_partial_credit_promise_is_now_expressible(self):
        """The inverted live factor, fixed.

        Its description promises "at least 2 earns partial credit; 5 or more earns full".
        As presence with `min: 2, max: 5` that was impossible AND inverted — 8 scored 0.
        """
        tiers = [{"threshold": 5, "points": 10}, {"threshold": 2, "points": 5},
                 {"threshold": 0, "points": 0}]
        scoring = {
            "score_cap": 100, "factors_max": 100,
            "completeness": {"max": 0}, "fit": {"max": 0}, "region_bonus": {"max": 0},
            "multi_source": {"max": 0}, "pipeline": {"max": 0},
            "factors": [{"key": "managed_property_types", "weight": 10,
                         "source_field": "managed_property_types", "tiers": tiers}],
        }
        def score(n):
            return als.score_prospects(
                [{"organization_name": "F", "managed_property_types": str(n)}],
                {"scoring": scoring}, today=TODAY,
            )[0]["score"]

        assert score(1) == 0     # below the floor
        assert score(3) == 50    # partial credit, previously impossible
        assert score(6) == 100   # full credit
        assert score(8) == 100   # and MORE no longer earns LESS

    def test_tiers_and_keywords_bypass_the_min_max_bound(self):
        """`min`/`max` turn presence into a threshold; a table already states the curve."""
        scoring = {
            "score_cap": 100, "factors_max": 100,
            "completeness": {"max": 0}, "fit": {"max": 0}, "region_bonus": {"max": 0},
            "multi_source": {"max": 0}, "pipeline": {"max": 0},
            "factors": [{"key": "size", "weight": 1, "source_field": "headcount",
                         "min": 10_000, "tiers": SIZE_TIERS}],
        }
        out = als.score_prospects(
            [{"organization_name": "X", "headcount": "850"}], {"scoring": scoring}, today=TODAY
        )
        # `min: 10000` would zero an 850 in presence mode; the tier table governs instead.
        assert out[0]["score"] == 100

    def test_presence_mode_is_untouched_when_no_table_is_authored(self):
        scoring = {
            "score_cap": 100, "factors_max": 100,
            "completeness": {"max": 0}, "fit": {"max": 0}, "region_bonus": {"max": 0},
            "multi_source": {"max": 0}, "pipeline": {"max": 0},
            "factors": [{"key": "has_site", "weight": 1, "source_field": "website_url"}],
        }
        out = als.score_prospects(
            [{"organization_name": "X", "website_url": "https://x.example"}],
            {"scoring": scoring}, today=TODAY,
        )
        assert out[0]["score"] == 100
        assert out[0]["score_factors"]["factors"]["has_site"] == 1

    def test_both_tables_authored_resolves_to_tiers_and_is_warned_about(self):
        logged: list[str] = []
        scoring = {
            "score_cap": 100, "factors_max": 100,
            "completeness": {"max": 0}, "fit": {"max": 0}, "region_bonus": {"max": 0},
            "multi_source": {"max": 0}, "pipeline": {"max": 0},
            "factors": [{"key": "ambiguous", "weight": 1, "source_field": "v",
                         "tiers": SIZE_TIERS, "keywords": {"anything": 10}}],
        }
        out = als.score_prospects(
            [{"organization_name": "X", "v": "850"}],
            {"scoring": scoring, "_log": logged.append}, today=TODAY,
        )
        assert out[0]["score"] == 100                       # tiers won
        assert any("ambiguous" in m for m in logged)        # and it was not silent
