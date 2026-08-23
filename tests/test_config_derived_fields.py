"""The merge vocabulary must come from the skill config, never from a fixed list.

**Why this file exists.** `_merge_raw_rows` used to iterate a hardcoded 20-field map
written for one vertical (church/AV: `denomination`, `campaign_goal`, `amount_raised`,
`av_opportunity_notes`). Only those fields survived the merge. On the first real HVAC
production run it silently discarded 7 of the 11 fields the skill authored — including
`square_footage` (present on 17 of 36 prospects) and `portfolio_size` (10) — which were
exactly the fields the operator's ICP threshold and the authored scoring factors needed.

**Nothing failed.** The config validated, the scan completed, and the whole 203-test
suite passed both before and after the vocabulary was replaced. That is why these tests
are written against the real run's field names rather than invented ones: the defect was
invisible to every existing check.
"""
from __future__ import annotations

import av_lead_scanner as als


# The three sources the real `hvac-prospect-scanner` skill authored, verbatim.
HVAC_SOURCES = {
    "business_directories": {"fields": ["company_name", "industry", "contact_title", "address", "website"]},
    "building_permit_filings": {"fields": ["company_name", "permit_type", "permit_date", "property_address", "square_footage"]},
    "commercial_property_listings": {"fields": ["company_name", "property_type", "square_footage", "portfolio_size", "address", "website"]},
}


class TestCanonicalFieldsFromSources:
    def test_unions_every_authored_field_without_duplicates(self):
        got = als.canonical_fields_from_sources(HVAC_SOURCES)
        assert got.count("square_footage") == 1, "authored twice across sources, must appear once"
        for expected in ("company_name", "industry", "contact_title", "permit_type",
                         "permit_date", "property_address", "square_footage",
                         "property_type", "portfolio_size", "address", "website"):
            assert expected in got, f"{expected} was authored and must be in the vocabulary"

    def test_tolerates_missing_or_malformed_config(self):
        assert als.canonical_fields_from_sources(None) == ()
        assert als.canonical_fields_from_sources({}) == ()
        assert als.canonical_fields_from_sources({"s": {}}) == ()
        assert als.canonical_fields_from_sources({"s": {"fields": []}}) == ()


class TestMergeKeepsAuthoredFields:
    def test_icp_critical_fields_survive_the_merge(self):
        """The regression that motivated this file: square_footage and portfolio_size."""
        rows = [
            {"company_name": "Acme Holdings", "square_footage": "15273"},
            {"portfolio_size": "12", "property_type": "office"},
        ]
        out = als._merge_raw_rows(rows, als.canonical_fields_from_sources(HVAC_SOURCES))
        assert out["square_footage"] == "15273"
        assert out["portfolio_size"] == "12"
        assert out["property_type"] == "office"

    def test_industry_and_contact_title_survive(self):
        """Both were collected on the real run and both were dropped."""
        rows = [{"industry": "Property Management", "contact_title": "President"}]
        out = als._merge_raw_rows(rows, als.canonical_fields_from_sources(HVAC_SOURCES))
        assert out["industry"] == "Property Management"
        assert out["contact_title"] == "President"

    def test_no_config_falls_back_to_every_key_present(self):
        """An unknown key is data we were asked to collect — never drop it."""
        rows = [{"totally_novel_field": "value", "another_one": "x"}]
        out = als._merge_raw_rows(rows)
        assert out["totally_novel_field"] == "value"
        assert out["another_one"] == "x"

    def test_field_name_matching_is_punctuation_and_case_insensitive(self):
        """Replaces per-field synonym tables: one normalization rule, no maintenance."""
        rows = [{"Square Footage": "9000"}, {"squareFootage": "12500"}]
        out = als._merge_raw_rows(rows, ("square_footage",))
        assert out["square_footage"] == "12500", "longest value wins, as before"

    def test_longest_value_wins_across_sources(self):
        rows = [{"industry": "RE"}, {"industry": "Commercial Real Estate"}]
        out = als._merge_raw_rows(rows, ("industry",))
        assert out["industry"] == "Commercial Real Estate"


class TestIdentityStillResolves:
    """The only hardcoded names left are vertical-neutral identity fields, and the
    engine's own internals read them — so they must keep working."""

    def test_company_name_populates_organization_name(self):
        out = als._merge_raw_rows([{"company_name": "Southeast Venture"}], ("company_name",))
        assert out["organization_name"] == "Southeast Venture"
        assert out["company_name"] == "Southeast Venture", "authored name is kept too"

    def test_address_variants_populate_location_address(self):
        for key in ("address", "location", "street_address", "property_address"):
            out = als._merge_raw_rows([{key: "1 Main St, Nashville, TN 37204"}])
            assert out["location_address"] == "1 Main St, Nashville, TN 37204", key

    def test_website_variants_populate_website_url(self):
        for key in ("website", "url", "domain"):
            out = als._merge_raw_rows([{key: "https://hbre.us"}])
            assert out["website_url"] == "https://hbre.us", key

    def test_identity_keys_always_present_even_when_absent_from_data(self):
        out = als._merge_raw_rows([{"square_footage": "1"}], ("square_footage",))
        for ident in ("organization_name", "city", "state", "location_address", "website_url"):
            assert ident in out, f"{ident} must exist (downstream reads it unconditionally)"
            assert out[ident] == ""


class TestNoVerticalVocabularyRemains:
    def test_church_av_field_names_are_gone_from_the_engine(self):
        """A mutation guard: if anyone reintroduces a domain vocabulary, this fails.

        Deliberately asserts on the *absence of a static map*, not on behaviour —
        behaviour tests passed for the entire life of the defect.
        """
        assert not hasattr(als, "_FIELD_ALIASES"), "the hardcoded vocabulary must not return"
        neutral = set(als._IDENTITY_ALIASES)
        for domain_specific in ("denomination", "campaign_goal", "amount_raised",
                                "av_opportunity_notes", "project_phase", "consultant_firm"):
            assert domain_specific not in neutral

    def test_descriptions_are_collected_generically(self):
        """Was hardcoded to the single church-AV field `project_description`."""
        rows = [{"service_description": "HVAC retrofit"}, {"project_description": "roof"}]
        out = als._merge_raw_rows(rows)
        assert "HVAC retrofit" in out["all_descriptions"]
        assert "roof" in out["all_descriptions"]


class TestLocalityParsing:
    """Every prospect on the real run had a street address and NULL city/state/zip,
    which also killed region scoring (it reads city/state)."""

    # Verbatim from run c214e3d5.
    REAL = [
        ("4521 Trousdale Drive, Nashville, TN 37204", ("Nashville", "TN", "37204")),
        ("4322 Harding Pike Ste 429, Nashville, TN 37205", ("Nashville", "TN", "37205")),
        ("750 Old Hickory Blvd Building ONE, Suite 125, Brentwood, TN 37027", ("Brentwood", "TN", "37027")),
    ]

    def test_parses_the_addresses_the_sources_actually_returned(self):
        for address, expected in self.REAL:
            assert als._parse_locality(address) == expected, address

    def test_never_overwrites_a_supplied_value(self):
        """A source that answered explicitly is more trustworthy than our regex."""
        assert als._parse_locality("1 Main St, Nashville, TN 37204", city="Franklin") == (
            "Franklin", "TN", "37204",
        )

    def test_fills_only_what_is_missing(self):
        assert als._parse_locality("1 Main St, Nashville, TN") == ("Nashville", "TN", None)

    def test_leaves_ambiguous_input_empty_rather_than_guessing(self):
        """A wrong city mis-scores region AND mis-routes the lead to a sales territory,
        which is worse than an empty field."""
        for junk in ("Somewhere vague", "", None, "12 Rue de Rivoli, Paris, France"):
            assert als._parse_locality(junk) == (None, None, None), junk


class TestCollectedFieldsReachTheirColumns:
    def test_passthrough_carries_industry_and_zip(self):
        """A field absent from PROSPECT_PASSTHROUGH can never reach its column, however
        well it was collected — `industry` was collected on 12 of 36 and stranded."""
        from aeo.event_mapping import PROSPECT_PASSTHROUGH

        for required in ("industry", "zip_code", "city", "state", "address", "website"):
            assert required in PROSPECT_PASSTHROUGH, required

    def test_passthrough_carries_discovery_contacts_and_provenance(self):
        """Added 2026-08-12. 12 of 36 prospects on the real run carried a contact title
        from a business directory; `sources` was never populated at all, so nothing could
        say where a prospect came from without opening the JSONB."""
        from aeo.event_mapping import PROSPECT_PASSTHROUGH

        for required in ("contact_name", "contact_title", "sources"):
            assert required in PROSPECT_PASSTHROUGH, required

    def test_empty_contact_values_are_omitted_from_the_wire(self):
        """The engine emitted `''` for most contact names, which reads as "present" to a
        count or a truthiness check — the trap that made a 1-of-36 result look like 14."""
        from aeo.event_mapping import map_prospects_event

        payloads = map_prospects_event(
            {
                "phase": "discover",
                "items": [{"id": "p1", "company_name": "A", "contact_title": None, "sources": None}],
            }
        )
        item = payloads[0]["data"][0]
        assert "contact_title" not in item
        assert "sources" not in item

    def test_populated_contact_values_do_reach_the_wire(self):
        from aeo.event_mapping import map_prospects_event

        payloads = map_prospects_event(
            {
                "phase": "discover",
                "items": [{"id": "p1", "company_name": "A", "contact_title": "President",
                           "sources": "business_directories"}],
            }
        )
        item = payloads[0]["data"][0]
        assert item["contact_title"] == "President"
        assert item["sources"] == "business_directories"


class TestUniversalTimingFieldsDoNotDockCompleteness:
    """`fields` does double duty, and the second job punishes the first.

    It is the merge vocabulary AND the completeness denominator
    (`filled / len(fields)`). Appending `event_date`/`event_type` to every source — so
    that every vertical is asked for a timing signal — would therefore dock every
    prospect discovered from a source that legitimately has no dated event: a property
    directory, a firm register. Penalising a record for honestly lacking a field we
    added on its behalf is the opposite of what the append is for.

    The pair must nevertheless STAY in `sources[*].fields`: that is the merge
    vocabulary, and dropping it there is exactly the `industry` defect of `acafb67` —
    asked for, answered, then silently discarded.
    """

    SOURCES = {
        "permits": {"fields": ["company_name", "permit_status", "event_date", "event_type"]},
        "directories": {"fields": ["company_name", "portfolio_size", "event_date", "event_type"]},
    }

    def _pinned(self, scoring=None):
        from aeo.runner import _pin_completeness_fields

        ctx = {"sources": self.SOURCES, "scoring": scoring if scoring is not None else {}}
        _pin_completeness_fields(ctx)
        return ctx

    def test_the_timing_pair_is_out_of_the_denominator(self):
        fields = self._pinned()["scoring"]["completeness"]["fields"]
        assert "event_date" not in fields and "event_type" not in fields
        assert "permit_status" in fields and "portfolio_size" in fields

    def test_the_pair_stays_in_the_merge_vocabulary(self):
        # The half that must NOT be dropped. `canonical_fields_from_sources` reads
        # `sources[*].fields`, and `_merge_raw_rows` keeps only what it names.
        self._pinned()
        assert "event_date" in als.canonical_fields_from_sources(self.SOURCES)

    def test_a_skill_that_declared_its_own_completeness_fields_is_untouched(self):
        authored = {"completeness": {"fields": ["company_name", "event_date"]}}
        got = self._pinned(authored)["scoring"]["completeness"]["fields"]
        assert got == ["company_name", "event_date"], "an operator's declaration wins"

    def test_a_source_of_nothing_but_the_pair_does_not_score_everyone_zero(self):
        from aeo.runner import _pin_completeness_fields

        ctx = {"sources": {"s": {"fields": ["event_date", "event_type"]}}, "scoring": {}}
        _pin_completeness_fields(ctx)
        # An empty denominator makes `score_completeness` return 0 for every prospect,
        # silently and forever. Falling back to the full list is wrong-but-visible.
        assert ctx["scoring"]["completeness"]["fields"]
