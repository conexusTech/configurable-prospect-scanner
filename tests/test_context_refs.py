"""Tests for R12 context-ref resolution. Ours.

The mapping was read off a live runtime-context response. If it drifts, bindings
resolve to nothing and every phase silently searches for the wrong thing.
"""

from __future__ import annotations

import pytest

from aeo.context_refs import (
    CONTEXT_PATHS,
    UnresolvedRefError,
    resolve,
    resolve_ref,
)

CONTEXT = {
    "geography": {
        "home_markets": ["Austin, TX"],
        "secondary_markets": ["Denver, CO"],
        "excluded_markets": [],
        "include_scope": "HOME_SECONDARY",
    },
    "decision_makers": {
        "decision_titles": ["Facilities Director"],
        "seniorities": ["director", "vp"],
    },
    "icp": {
        "icp_attributes": ["multi-site"],
        "in_market_signals": ["capital campaign announced"],
        "disqualifiers": ["under 200 seats"],
        "lookalike_sources": ["existing customers"],
    },
    "lead_type": "MIXED",
    "organization": {"industry": "Worship", "competitors": ["Acme AV"]},
}


class TestVocabulary:
    def test_covers_all_thirteen_published_keys(self):
        assert len(CONTEXT_PATHS) == 13

    def test_every_path_resolves_against_a_real_context_shape(self):
        # Guards the whole mapping against drift in one assertion: if aeo-backend
        # renames a context section, this fails rather than every phase quietly
        # searching for nothing.
        for key in CONTEXT_PATHS:
            value = resolve_ref({"context_ref": key}, CONTEXT)
            assert value is not None or key == "excluded_markets", key

    def test_pins_the_one_key_that_is_NOT_name_derivable(self):
        # `decision_seniorities` lives at decision_makers.SENIORITIES. The other
        # twelve match their published names, so this is the single place a
        # reasonable person guesses wrong — and guessing wrong yields an empty
        # seniority list, which silently widens contact search to every seniority.
        assert CONTEXT_PATHS["decision_seniorities"] == "decision_makers.seniorities"
        assert resolve_ref({"context_ref": "decision_seniorities"}, CONTEXT) == [
            "director",
            "vp",
        ]


class TestResolution:
    def test_resolves_a_binding_to_the_org_value(self):
        assert resolve_ref({"context_ref": "home_markets"}, CONTEXT) == ["Austin, TX"]

    def test_falls_back_to_default_when_the_org_has_nothing(self):
        # A `default` is the only position a literal is permitted in an org-specific
        # field — it exists so a skill still runs for a half-onboarded org.
        binding = {"context_ref": "excluded_markets", "default": ["Springfield"]}
        assert resolve_ref(binding, CONTEXT) == ["Springfield"]

    def test_returns_none_when_neither_org_value_nor_default_exists(self):
        assert resolve_ref({"context_ref": "excluded_markets"}, CONTEXT) is None

    def test_raises_on_a_key_outside_the_published_vocabulary(self):
        # aeo-backend's R12 lint rejects these at finalize, so one arriving here
        # means the lint was bypassed — papering over it would hide that.
        with pytest.raises(UnresolvedRefError):
            resolve_ref({"context_ref": "titles"}, CONTEXT)

    def test_walks_nested_structures_and_lists(self):
        config = {
            "contacts": {
                "titles": {"context_ref": "decision_titles"},
                "seniorities": {"context_ref": "decision_seniorities"},
            },
            "discovery": {"sources": {"s1": {"queries": ["find in {market}"]}}},
            "nested": [{"deep": {"context_ref": "industry"}}],
        }
        out = resolve(config, CONTEXT)
        assert out["contacts"]["titles"] == ["Facilities Director"]
        assert out["contacts"]["seniorities"] == ["director", "vp"]
        assert out["nested"][0]["deep"] == "Worship"
        # Untouched literals survive verbatim.
        assert out["discovery"]["sources"]["s1"]["queries"] == ["find in {market}"]

    def test_leaves_a_config_with_no_bindings_unchanged(self):
        config = {"scoring": {"score_cap": 100}}
        assert resolve(config, CONTEXT) == config
