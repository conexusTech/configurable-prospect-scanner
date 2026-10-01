"""An org's free-text markets become the US states its target-market gate admits.

**Why this exists.** A gated skill can bind `allowed_states` to the org's `home_markets`. Those are
free text ("Concord, NC", "Southeast US", "Atlanta"), and `in_target_market` compares the WHOLE
lead state to the WHOLE entry, expanding only through the skill's own aliases -- so "Concord, NC"
could never match a lead in NC, and a skill without full aliases could not match a state name at
all. `aeo.market_states` places an entry in a state only where the text itself names one.

Leads are built THROUGH the real producer and scored by the real scorer, for the reason
`test_verified_location` records: a hand-written dict without `_internal` would pass while the
score read something else.
"""

from __future__ import annotations

import copy

from aeo.market_states import apply_market_binding, resolve_market_states
from aeo.runner import apply_market_resolution, resolve_context_refs
from tests.test_gated_score import CFG
from tests.test_verified_location import _produce, _score

BINDING = {"context_ref": "home_markets"}


def _lead(state: str) -> dict:
    return _produce({"company_name": f"Acme {state}", "state": state})


def _raw_context(
    allowed, *, home, secondary=(), scope="HOME_ONLY", aliases: dict | None = None
) -> dict:
    """The context as the backend hands it over: the binding still in `skill.config`."""
    target = {k: v for k, v in CFG["gate"]["target_market"].items() if k != "state_aliases"}
    target["allowed_states"] = allowed
    if aliases:
        target["state_aliases"] = aliases
    scoring = {**CFG, "score_cap": 100, "gate": {**CFG["gate"], "target_market": target}}
    return {
        "skill": {"config": {"scoring": scoring}},
        "geography": {
            "home_markets": list(home),
            "secondary_markets": list(secondary),
            "include_scope": scope,
        },
    }


def _resolved(raw: dict) -> dict:
    """What the runner hands the engine: bindings resolved, then markets resolved to states."""
    return apply_market_resolution(raw, resolve_context_refs(raw))


def _passes_gate(context: dict, state: str) -> bool:
    scoring = context["skill"]["config"]["scoring"]
    scored = _score([_lead(state)], scoring)
    assert len(scored) == 1
    return scored[0]["score_factors"]["gated"]["gates"]["target_market"]


class TestResolveMarketStates:
    def test_codes_names_and_city_state_forms_resolve(self):
        codes, unresolved = resolve_market_states(
            ["NC", "north carolina", "Concord, NC", "Charlotte, North Carolina", "DC"]
        )
        assert codes == ["DC", "NC"]
        assert unresolved == []

        # One entry at a time, so the mapping itself is pinned and not only the sorted union.
        assert resolve_market_states(["NC"]) == (["NC"], [])
        assert resolve_market_states(["north carolina"]) == (["NC"], [])
        assert resolve_market_states(["Concord, NC"]) == (["NC"], [])
        assert resolve_market_states(["Charlotte, North Carolina"]) == (["NC"], [])
        assert resolve_market_states(["DC"]) == (["DC"], [])
        # The part after the LAST comma names the state.
        assert resolve_market_states(["Charlotte, Mecklenburg County, NC"]) == (["NC"], [])

    def test_regions_bare_cities_countries_and_sentences_are_not_guessed(self):
        entries = [
            "Southeast US",
            "Mountain West",
            "Atlanta",
            "Portland",
            "Canada",
            "Primarily the Carolinas, expanding into Georgia and Canada",
            "Charlotte, Southeast",
        ]
        codes, unresolved = resolve_market_states(entries)
        assert codes == []
        assert unresolved == entries, "every entry reported, in the original order"

    def test_non_strings_are_ignored_and_the_result_is_sorted_and_unique(self):
        codes, unresolved = resolve_market_states(
            ["Utah", None, 7, "Concord, NC", "nc", "Atlanta", {"x": 1}, "Georgia", "NC"]
        )
        assert codes == ["GA", "NC", "UT"]
        assert unresolved == ["Atlanta"], "non-strings are neither resolved nor reported"
        assert resolve_market_states([]) == ([], [])


class TestTheGateUsesResolvedMarkets:
    def test_a_city_state_home_market_admits_its_state(self):
        raw = _raw_context(BINDING, home=["Concord, NC"])
        resolved = _resolved(raw)

        assert _passes_gate(resolved, "NC") is True
        # A state the markets do not name stays out.
        assert _passes_gate(resolved, "TX") is False
        # The control: the same list left unresolved never admits NC, because the whole-string
        # comparison cannot see the state inside "Concord, NC".
        assert _passes_gate(resolve_context_refs(raw), "NC") is False

    def test_secondary_markets_count_with_home_secondary_scope(self):
        raw = _raw_context(
            BINDING, home=["Utah"], secondary=["Atlanta, GA"], scope="HOME_SECONDARY"
        )
        resolved = _resolved(raw)
        assert _passes_gate(resolved, "GA") is True
        assert _passes_gate(resolved, "UT") is True

    def test_secondary_markets_do_not_count_with_home_scope(self):
        for scope in ("HOME_ONLY", "HOME"):
            raw = _raw_context(BINDING, home=["Utah"], secondary=["Atlanta, GA"], scope=scope)
            resolved = _resolved(raw)
            assert _passes_gate(resolved, "GA") is False, scope
            assert _passes_gate(resolved, "UT") is True, scope

    def test_unresolved_entries_are_logged_once(self, capsys):
        raw = _raw_context(
            BINDING,
            home=["Concord, NC", "Southeast US", "Atlanta"],
            secondary=["Canada"],
            scope="HOME_SECONDARY",
        )
        _resolved(raw)
        lines = [
            line
            for line in capsys.readouterr().err.splitlines()
            if "WARNING target-market gate:" in line
        ]
        assert len(lines) == 1, lines
        for entry in ("Southeast US", "Atlanta", "Canada"):
            assert entry in lines[0]
        assert "Concord, NC" not in lines[0], "a resolved entry is not reported"

    def test_nothing_is_logged_when_every_entry_resolves(self, capsys):
        _resolved(_raw_context(BINDING, home=["North Carolina", "Concord, SC"]))
        assert "WARNING target-market gate:" not in capsys.readouterr().err

    def test_a_literal_allowed_list_is_used_as_authored(self):
        literal = ["SC", "Charlotte"]
        raw = _raw_context(
            literal,
            home=["Concord, NC"],
            secondary=["Atlanta, GA"],
            scope="HOME_SECONDARY",
        )
        before = copy.deepcopy(raw["skill"]["config"]["scoring"]["gate"]["target_market"])
        resolved = _resolved(raw)
        after = resolved["skill"]["config"]["scoring"]["gate"]["target_market"]
        assert after == before
        assert after["allowed_states"] == ["SC", "Charlotte"]
        assert _passes_gate(resolved, "GA") is False
        assert _passes_gate(resolved, "NC") is False

    def test_an_original_entry_still_matches_its_own_text(self):
        # Byte-for-byte what the gate did before: the entries are KEPT beside their codes, so a
        # lead whose state is stored as the entry's own text still passes -- with no alias table
        # for the whole-string comparison to lean on, and for an entry that names no state.
        raw = _raw_context(BINDING, home=["North Carolina", "Ontario"])
        resolved = _resolved(raw)
        allowed = resolved["skill"]["config"]["scoring"]["gate"]["target_market"]["allowed_states"]
        assert allowed[:2] == ["North Carolina", "Ontario"]
        assert "NC" in allowed

        assert _passes_gate(resolved, "North Carolina") is True
        assert _passes_gate(resolved, "NC") is True
        assert _passes_gate(resolved, "Ontario") is True
        assert _passes_gate(resolved, "TX") is False

    def test_a_skills_own_aliases_still_apply(self):
        # The allowed list is ["NC"] only, and the lead's state is stored as the full name: the
        # only way through is the skill's alias table expanding "nc" back to "north carolina".
        raw = _raw_context(BINDING, home=["NC"], aliases={"north carolina": "NC"})
        resolved = _resolved(raw)
        allowed = resolved["skill"]["config"]["scoring"]["gate"]["target_market"]["allowed_states"]
        assert allowed == ["NC"]
        assert _passes_gate(resolved, "North Carolina") is True
        # The control: the same list without the alias table cannot match the full name.
        bare = _resolved(_raw_context(BINDING, home=["NC"]))
        assert _passes_gate(bare, "North Carolina") is False

    def test_scope_is_the_one_zip_discovery_uses(self):
        from aeo.phases.zip_discovery import target_markets

        for scope, adds_secondary in (
            ("HOME_SECONDARY", True),
            (" home_secondary ", True),
            ("HOME_ONLY", False),
            ("HOME", False),
            ("NO_SECONDARY", False),
            ("HOME_ONLY_EXCL_SECONDARY", False),
            ("", False),
            (None, False),
        ):
            raw = _raw_context(BINDING, home=["Utah"], secondary=["Atlanta, GA"], scope=scope)
            resolved = _resolved(raw)
            assert _passes_gate(resolved, "GA") is adds_secondary, scope
            # The search and the gate agree on whether the secondary market is in play.
            searched = "Atlanta, GA" in target_markets(raw["geography"])
            assert searched is adds_secondary, scope

    def test_a_dict_shaped_home_markets_is_left_exactly_as_before(self):
        # The engine flattens `{state: [cities]}` itself, and before this change the gate passed a
        # lead on the dict's keys. Resolution must not turn that into an empty allow-list.
        raw = _raw_context(
            BINDING, home=[], secondary=["Atlanta, GA"], scope="HOME_SECONDARY"
        )
        raw["geography"]["home_markets"] = {"NC": ["Concord", "Raleigh"]}
        before = resolve_context_refs(raw)["skill"]["config"]["scoring"]["gate"]["target_market"]
        assert before["allowed_states"] == {"NC": ["Concord", "Raleigh"]}

        resolved = _resolved(raw)
        after = resolved["skill"]["config"]["scoring"]["gate"]["target_market"]
        assert after["allowed_states"] == before["allowed_states"]
        # A lead in NC passed on the dict's keys before, and still does.
        assert _passes_gate(resolved, "NC") is True
        assert apply_market_binding(
            raw["skill"]["config"], resolved["skill"]["config"], raw["geography"]
        ) == []

    def test_empty_home_markets_with_secondary_scope_admits_secondary_states(self):
        # The org asked to prospect there; today every lead fails closed on an empty list.
        raw = _raw_context(BINDING, home=[], secondary=["Atlanta, GA"], scope="HOME_SECONDARY")
        resolved = _resolved(raw)
        assert _passes_gate(resolved, "GA") is True
        assert _passes_gate(resolved, "TX") is False

        # With home scope and nothing at home there is still nothing to admit.
        home_only = _resolved(
            _raw_context(BINDING, home=[], secondary=["Atlanta, GA"], scope="HOME_ONLY")
        )
        assert _passes_gate(home_only, "GA") is False

    def test_a_binding_with_a_default_is_the_skills_own_and_untouched(self):
        raw = _raw_context(BINDING, home=[], secondary=["Atlanta, GA"], scope="HOME_ONLY")
        target = raw["skill"]["config"]["scoring"]["gate"]["target_market"]
        target["allowed_states"] = {"context_ref": "home_markets", "default": ["TX"]}
        resolved = _resolved(raw)
        # The binding is not EXACTLY the bare one, so it is the skill's own and is untouched.
        assert resolved["skill"]["config"]["scoring"]["gate"]["target_market"][
            "allowed_states"
        ] == ["TX"]

    def test_the_unresolved_log_line_is_capped(self, capsys):
        junk = [f"Somewhere {i}" for i in range(14)]
        _resolved(_raw_context(BINDING, home=junk))
        lines = [
            line
            for line in capsys.readouterr().err.splitlines()
            if "WARNING target-market gate:" in line
        ]
        assert len(lines) == 1, lines
        assert "'Somewhere 9'" in lines[0]
        assert "'Somewhere 10'" not in lines[0]
        assert "(+4 more)" in lines[0]

    def test_the_resolved_list_is_deduplicated_in_order(self):
        raw = _raw_context(
            BINDING,
            home=["NC", "Concord, NC", "Utah"],
            secondary=["Raleigh, NC", "Atlanta, GA"],
            scope="HOME_SECONDARY",
        )
        allowed = _resolved(raw)["skill"]["config"]["scoring"]["gate"]["target_market"][
            "allowed_states"
        ]
        assert allowed == ["NC", "Concord, NC", "Utah", "UT", "GA"]

    def test_the_raw_context_is_not_mutated(self):
        raw = _raw_context(BINDING, home=["Concord, NC"])
        snapshot = copy.deepcopy(raw)
        _resolved(raw)
        assert raw == snapshot

    def test_a_malformed_geography_skips_the_resolution_and_never_raises(self):
        for geography in (
            None,
            "nonsense",
            {"home_markets": {"NC": ["Concord"]}, "secondary_markets": 5, "include_scope": 3},
            {"home_markets": "NC", "secondary_markets": ["Atlanta, GA"]},
            # A non-list secondary adds nothing, and non-string home entries are dropped.
            {"home_markets": ["NC"], "secondary_markets": "Atlanta, GA",
             "include_scope": "HOME_SECONDARY"},
            {"home_markets": ["NC", None, 7, ["x"]], "secondary_markets": {"GA": ["Atlanta"]},
             "include_scope": "HOME_SECONDARY"},
        ):
            raw = _raw_context(BINDING, home=[])
            raw["geography"] = geography
            bound = resolve_context_refs(raw)
            before = copy.deepcopy(
                bound["skill"]["config"]["scoring"]["gate"]["target_market"]["allowed_states"]
            )
            resolved = apply_market_resolution(raw, bound)
            after = resolved["skill"]["config"]["scoring"]["gate"]["target_market"][
                "allowed_states"
            ]
            assert after == before, geography

        # And the config-level function returns nothing to report rather than raising.
        assert apply_market_binding({}, {}, None) == []
        assert apply_market_binding({"scoring": 1}, {"scoring": 1}, {"home_markets": "x"}) == []

    def test_only_the_home_markets_binding_is_touched(self):
        other = {"context_ref": "secondary_markets"}
        raw = _raw_context(other, home=["Concord, NC"], secondary=["Atlanta, GA"])
        resolved = _resolved(raw)
        target = resolved["skill"]["config"]["scoring"]["gate"]["target_market"]
        assert target["allowed_states"] == ["Atlanta, GA"], "resolved as before, nothing added"

        absent = _raw_context(BINDING, home=["Concord, NC"])
        del absent["skill"]["config"]["scoring"]["gate"]["target_market"]["allowed_states"]
        out = _resolved(absent)
        assert "allowed_states" not in out["skill"]["config"]["scoring"]["gate"]["target_market"]
