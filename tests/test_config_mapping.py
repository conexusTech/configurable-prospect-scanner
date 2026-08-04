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
