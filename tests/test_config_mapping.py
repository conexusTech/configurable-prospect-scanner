"""Tests for the AEO→engine config mapping. Ours, not vendored.

These pin the behaviour that makes this repo worth existing: a config that cannot
drive a real scan is **refused**, and one that can is translated exactly. The
failure this guards against is not a crash — it is a scan that runs on defaults,
returns plausible prospects, and reports success.
"""

from __future__ import annotations

import pytest

from aeo.config_mapping import (
    UnmappedConfigError,
    build_tool_context,
    unsupported_authored_sections,
    with_universal_fields,
    _with_geo_strict_prompt,
)


def _context(**config_overrides) -> dict:
    """A minimally complete, mappable AEO runtime context."""
    config = {
        "product_description": "Professional AV integration",
        "discovery": {
            "sources": {
                "church_architects": {
                    "name_field": "firm_name",
                    "fields": ["firm_name", "city"],
                    "queries": ["church architects in {market}"],
                    "seed_firms": [],
                }
            }
        },
        "scoring": {"region_bonus": {"max": 10}},
    }
    config.update(config_overrides)
    return {
        "organization": {"name": "Acme AV"},
        "geography": {"home_markets": {"TX": ["Austin", "Dallas"]}},
        "skill": {"config": config},
    }


class TestRefusal:
    def test_names_every_fault_at_once(self):
        # One pass of repairs, not one fault discovered per run.
        with pytest.raises(UnmappedConfigError) as exc:
            build_tool_context({"organization": {"name": "Acme"}, "skill": {"config": {}}})
        assert len(exc.value.problems) >= 4

    def test_refuses_missing_discovery_sources(self):
        # The single most important refusal: queries decide who gets scanned, so a
        # default here is a wrong answer rather than a reduced one.
        ctx = _context(discovery={})
        with pytest.raises(UnmappedConfigError) as exc:
            build_tool_context(ctx)
        assert any("discovery.sources" in p for p in exc.value.problems)

    def test_refuses_a_source_missing_queries(self):
        ctx = _context(
            discovery={"sources": {"s1": {"name_field": "n", "fields": ["a"]}}}
        )
        with pytest.raises(UnmappedConfigError) as exc:
            build_tool_context(ctx)
        assert any("queries" in p for p in exc.value.problems)

    def test_refuses_missing_scoring(self):
        # An unranked list sliced by top_n is indistinguishable from a shortlist.
        ctx = _context(scoring={})
        with pytest.raises(UnmappedConfigError) as exc:
            build_tool_context(ctx)
        assert any("scoring" in p for p in exc.value.problems)

    def test_refuses_when_geography_resolved_to_nothing(self):
        ctx = _context()
        ctx["geography"] = {}
        with pytest.raises(UnmappedConfigError) as exc:
            build_tool_context(ctx)
        # The message must point at the ORG's geography, not the skill — the
        # binding is fine, the org data it resolved against is not.
        assert any("org's onboarding geography" in p for p in exc.value.problems)


class TestMapping:
    def test_produces_exactly_the_engine_top_level_keys(self):
        out = build_tool_context(_context())
        assert set(out) == {
            "organization",
            "product_description",
            "gemini",
            "output",
            "sources",
            "scoring",
        }

    def test_flattens_state_keyed_markets(self):
        out = build_tool_context(_context())
        assert out["organization"]["markets"] == ["Austin, TX", "Dallas, TX"]

    def test_merges_secondary_markets_and_dedupes_in_order(self):
        # A duplicate market is duplicated model spend, not a duplicated result.
        ctx = _context()
        ctx["geography"] = {
            "home_markets": ["Austin, TX"],
            "secondary_markets": ["Denver, CO", "Austin, TX"],
        }
        out = build_tool_context(ctx)
        assert out["organization"]["markets"] == ["Austin, TX", "Denver, CO"]

    def test_prefers_authored_description_over_org_products(self):
        # The config is what an operator accepted; org data is the substrate.
        ctx = _context()
        ctx["products_services"] = [{"description": "from onboarding"}]
        assert build_tool_context(ctx)["product_description"] == (
            "Professional AV integration"
        )

    def test_falls_back_to_org_products_when_config_has_none(self):
        ctx = _context(product_description=None)
        ctx["products_services"] = [{"description": "from onboarding"}]
        assert build_tool_context(ctx)["product_description"] == "from onboarding"

    def test_provider_config_is_deployment_not_authoring(self):
        # Model choice must not be settable from a chat-authored config: it is an
        # infrastructure concern that changes on our schedule, not the operator's.
        ctx = _context(gemini={"model": "authored-by-a-model"})
        out = build_tool_context(ctx)
        assert out["gemini"]["model"] == "gemini-3-flash-preview"

    def test_passes_scoring_through_untranslated(self):
        scoring = {"region_bonus": {"max": 7}, "pipeline": {"decision_lead_months": 13}}
        out = build_tool_context(_context(scoring=scoring))
        assert out["scoring"] == scoring


class TestPhaseCoverage:
    def test_reports_authored_sections_the_engine_cannot_run(self):
        # The PRD wants five phases; this engine has discovery and scoring. An
        # operator must hear that before the run, not infer it from thin output.
        ctx = _context(contacts={"titles": {"context_ref": "decision_titles"}})
        assert unsupported_authored_sections(ctx) == ["contacts"]

    def test_silent_when_only_supported_sections_are_authored(self):
        assert unsupported_authored_sections(_context()) == []

    def test_ignores_an_empty_unsupported_section(self):
        # An empty section is an artefact of drafting, not an authored intent.
        assert unsupported_authored_sections(_context(validation={})) == []


class TestGeoStrictPrompt:
    """The soft half of the geography fix — see GEO_STRICT_PROMPT."""

    def test_injects_a_strict_prompt_when_the_source_has_none(self):
        # The engine's own default mentions the market in one line among several,
        # and measured live that lost to big-metro search ranking three runs running.
        out = build_tool_context(_context())
        prompt = out["sources"]["church_architects"]["prompt"]
        assert "STRICT LOCATION REQUIREMENT" in prompt

    def test_leaves_the_engines_placeholders_intact(self):
        # The engine formats with query/n/seed_context. Substituting those here, or
        # leaving a {keys} behind, is a KeyError at format time.
        prompt = build_tool_context(_context())["sources"]["church_architects"]["prompt"]
        for placeholder in ("{query}", "{n}", "{seed_context}"):
            assert placeholder in prompt
        assert "{keys}" not in prompt and "{product_description}" not in prompt
        assert prompt.format(query="q", n=3, seed_context="")

    def test_permits_returning_FEWER_results(self):
        # As load-bearing as the instruction: "return EXACTLY n" pressures a model to
        # pad a thin area with plausible out-of-area names.
        prompt = build_tool_context(_context())["sources"]["church_architects"]["prompt"]
        assert "FEWER" in prompt

    def test_never_overrides_an_authored_prompt(self):
        ctx = _context()
        ctx["skill"]["config"]["discovery"]["sources"]["church_architects"]["prompt"] = "MINE {query}{n}{seed_context}"
        out = build_tool_context(ctx)
        assert out["sources"]["church_architects"]["prompt"].startswith("MINE")


class TestSeedFirmFanOut:
    """Closes the library-invariant hole agent-service found (thread #17)."""

    def test_fans_the_bound_lookalike_sources_into_every_source(self):
        # Seed firms are the org's own customers. Authored as a literal inside
        # discovery.sources they passed schema + lint + finalize, and the NEXT org to
        # connect the skill would have searched using the FIRST org's customer list.
        ctx = _context()
        ctx["skill"]["config"]["discovery"]["lookalike_sources"] = ["First Baptist", "Grace"]
        ctx["skill"]["config"]["discovery"]["sources"]["second"] = {
            "name_field": "f", "fields": ["f"], "queries": ["q"],
        }
        out = build_tool_context(ctx)
        for name in ("church_architects", "second"):
            assert out["sources"][name]["seed_firms"] == ["First Baptist", "Grace"], name

    def test_unions_rather_than_overwrites_existing_seeds(self):
        ctx = _context()
        ctx["skill"]["config"]["discovery"]["lookalike_sources"] = ["New Co"]
        ctx["skill"]["config"]["discovery"]["sources"]["church_architects"]["seed_firms"] = ["Kept"]
        out = build_tool_context(ctx)
        assert out["sources"]["church_architects"]["seed_firms"] == ["Kept", "New Co"]

    def test_no_lookalike_sources_leaves_sources_alone(self):
        out = build_tool_context(_context())
        assert out["sources"]["church_architects"].get("seed_firms") == []

    def test_accepts_a_scalar_lookalike_value(self):
        # The org's onboarding blob is free-form; a single string is a real shape.
        ctx = _context()
        ctx["skill"]["config"]["discovery"]["lookalike_sources"] = "Only One"
        out = build_tool_context(ctx)
        assert out["sources"]["church_architects"]["seed_firms"] == ["Only One"]


class TestUniversalRecordFields:
    """Regression: `website` and `industry` reached no column on two consecutive skills.

    Everything downstream was already wired (PROSPECT_PASSTHROUGH names both, the engine
    resolves website via _IDENTITY_ALIASES) — a source only receives what it ASKS for, and
    neither skill asked. Appended by the mapper so it cannot be forgotten per skill.
    """

    @staticmethod
    def _prompt_for(fields):
        out = _with_geo_strict_prompt(
            {"permits": {"name_field": "company_name", "fields": fields, "queries": ["q"]}},
            "commercial flooring",
        )
        return out["permits"]["prompt"]

    def test_appends_website_and_industry_to_the_prompt_keys(self):
        prompt = self._prompt_for(["company_name", "location"])
        assert "website" in prompt, "website was never asked for"
        assert "industry" in prompt, "industry was never asked for"

    def test_preserves_the_authored_fields_and_their_order(self):
        got = with_universal_fields(["company_name", "signal_type", "location"])
        assert got[:3] == ["company_name", "signal_type", "location"]
        assert got[3:] == ["website", "industry"]

    def test_does_not_duplicate_an_alias_the_engine_already_folds(self):
        # `website_url` / `url` / `domain` all resolve to website_url upstream, so asking
        # again as `website` would put two equivalent keys in one prompt.
        for spelling in ("website_url", "url", "domain", "Website"):
            got = with_universal_fields(["company_name", spelling])
            assert got.count("website") == 0, f"{spelling} should suppress the append"
            assert "industry" in got

    def test_does_not_duplicate_an_authored_industry(self):
        got = with_universal_fields(["company_name", "industry"])
        assert got.count("industry") == 1

    def test_covers_the_absent_fields_fallback_too(self):
        # The `or [...]` fallback had the identical gap.
        prompt = self._prompt_for(None)
        assert "website" in prompt and "industry" in prompt

    def _source_for(self, fields):
        out = _with_geo_strict_prompt(
            {"permits": {"name_field": "company_name", "fields": fields, "queries": ["q"]}},
            "commercial flooring",
        )
        return out["permits"]

    def test_the_appended_fields_reach_the_merge_vocabulary_not_only_the_prompt(self):
        # 🔑 The regression that shipped `industry` NULL on every production run.
        #
        # Every test above this one asserts the PROMPT. None asserted `fields`, so none
        # could see that the two had diverged: the model was asked for `industry`,
        # answered with it, and `_merge_raw_rows` dropped it because
        # `canonical_fields_from_sources` reads `fields` and `fields` never gained it.
        #
        # Asserting the prompt and the vocabulary together is the point — a fix that
        # updates one and not the other is the original defect.
        src = self._source_for(["company_name", "location"])
        assert "industry" in src["fields"], "industry asked for but not in the vocabulary"
        assert "website" in src["fields"], "website asked for but not in the vocabulary"
        for key in src["fields"]:
            assert key in src["prompt"], f"{key} is in the vocabulary but never asked for"

    def test_the_vocabulary_survives_canonical_extraction(self):
        # One hop further out, because `fields` being right is only useful if the
        # engine's own extractor agrees. This is the function `_merge_raw_rows` gates on.
        from av_lead_scanner import canonical_fields_from_sources

        sources = _with_geo_strict_prompt(
            {"permits": {"name_field": "company_name", "fields": ["company_name"], "queries": ["q"]}},
            "commercial flooring",
        )
        canonical = canonical_fields_from_sources(sources)
        assert "industry" in canonical
        assert "website" in canonical

    def test_industry_survives_the_merge_and_lands_on_the_record(self):
        # End to end over the actual merge, with a row shaped like a model response.
        # `industry` has no `_IDENTITY_ALIASES` entry, which is the whole reason it
        # needed the vocabulary — `website` would have survived either way.
        from av_lead_scanner import _merge_for_scoring, canonical_fields_from_sources

        sources = _with_geo_strict_prompt(
            {"permits": {"name_field": "company_name", "fields": ["company_name"], "queries": ["q"]}},
            "commercial flooring",
        )
        canonical = list(canonical_fields_from_sources(sources))
        merged = _merge_for_scoring(
            [{"raw": {"company_name": "Acme Floors", "industry": "Flooring Contractor",
                      "website": "acme.example"}}],
            canonical,
        )
        assert merged.get("industry") == "Flooring Contractor"
        # The record reads website through the folded alias, not the authored spelling.
        assert merged.get("website_url") == "acme.example"

    def test_the_authored_prompt_path_does_not_gain_an_unasked_vocabulary(self):
        # The deliberate asymmetry. We never injected keys into an operator's own
        # prompt, so claiming the fields would enlarge the `completeness` denominator
        # (`filled / len(fields)`) with names that can never be filled — dragging every
        # prospect's score down to record a field nobody asked for.
        out = _with_geo_strict_prompt(
            {
                "custom": {
                    "name_field": "company_name",
                    "fields": ["company_name"],
                    "queries": ["q"],
                    "prompt": "my own prompt",
                }
            },
            "x",
        )
        assert out["custom"]["fields"] == ["company_name"]

    def test_an_authored_prompt_is_left_alone(self):
        # A source that ships its own prompt is passed through untouched — we must not
        # rewrite an operator's literal prompt to inject keys.
        out = _with_geo_strict_prompt(
            {
                "custom": {
                    "name_field": "company_name",
                    "fields": ["company_name"],
                    "queries": ["q"],
                    "prompt": "my own prompt",
                }
            },
            "x",
        )
        assert out["custom"]["prompt"] == "my own prompt"
