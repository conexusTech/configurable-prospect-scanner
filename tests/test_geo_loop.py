"""Tests for the search → verify → re-search loop. Ours.

Exists because prompting is not a reliable geographic control — the same prompt gave
in-area results standalone and out-of-area results in-container. These pin the loop's
stopping conditions and its cost controls, which are the difference between a fix and
an unbounded bill.
"""

from __future__ import annotations

import json

from aeo.phases.geo_filter import build_target_area
from aeo.phases.geo_loop import (
    discover_in_area,
    needs_verification,
    verify_locations,
)

AREA = build_target_area([{"zip_code": "78701", "city": "Austin", "state": "TX"}], None)


def _parse(text: str) -> list[dict]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [p for p in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(p, dict)]


def _verifier(by_name: dict[str, dict]):
    """Provider that answers location questions from a lookup table."""
    def call(prompt, **kwargs):
        for name, answer in by_name.items():
            if name in prompt:
                return json.dumps([answer])
        return "[]"
    call.calls = []
    return call


def _p(pid: str, name: str, **over) -> dict:
    return {"id": pid, "company_name": name, **over}


class TestNeedsVerification:
    def test_a_prospect_CLAIMING_to_be_in_area_is_still_verified(self):
        # The hole an earlier version had. The reported city comes from the same model
        # whose geography we do not trust — so a model returning Dallas firms labelled
        # "Austin" would walk straight past enforcement. Verify everything.
        assert needs_verification(_p("1", "X", city="Austin", state="TX"), AREA) is True

    def test_trust_reported_restores_the_cheaper_behaviour_explicitly(self):
        # Halves the call count, and is an opt-in rather than a default.
        assert (
            needs_verification(
                _p("1", "X", city="Austin", state="TX"), AREA, trust_reported=True
            )
            is False
        )

    def test_an_out_of_area_looking_prospect_is_verified(self):
        # Discovery rows are frequently wrong about location, and a wrong rejection
        # costs as much as a wrong keep.
        assert needs_verification(_p("1", "X", city="Dallas", state="TX"), AREA) is True

    def test_a_prospect_with_no_location_is_verified(self):
        assert needs_verification(_p("1", "X"), AREA) is True


class TestVerifyLocations:
    def test_returns_the_established_address_per_prospect(self):
        provider = _verifier({"Acme": {"city": "Austin", "state": "TX", "zip_code": "78701"}})
        out = verify_locations(
            [_p("1", "Acme")], provider=provider, provider_config={}, parse_json_array=_parse
        )
        assert out["1"]["city"] == "Austin"

    def test_an_unanswerable_verification_yields_no_entry(self):
        # The caller then falls back to what discovery reported and KEEPS the
        # prospect — unverifiable must not mean rejected.
        out = verify_locations(
            [_p("1", "Ghost")], provider=_verifier({}), provider_config={}, parse_json_array=_parse
        )
        assert out == {}


class TestLoop:
    def test_stops_as_soon_as_the_target_is_met(self):
        # Cost control: a second sweep must not run when the first sufficed.
        rounds = []

        def discover(ctx):
            rounds.append(ctx)
            return [_p("1", "A", city="Austin", state="TX"), _p("2", "B", city="Austin", state="TX")]

        in_area, rejects = discover_in_area(
            tool_context={"sources": {"s": {}}}, area=AREA, target_count=2,
            discover=discover, provider=_verifier({}), provider_config={},
            parse_json_array=_parse,
        )
        assert len(rounds) == 1
        assert len(in_area) == 2 and rejects == []

    def test_re_searches_when_short_and_EXCLUDES_what_it_already_found(self):
        # Without the exclusion every round returns the same firms. The engine's own
        # `seed_firms` channel renders as "do NOT return these".
        seen_seed_firms = []

        def discover(ctx):
            seen_seed_firms.append(list(ctx["sources"]["s"].get("seed_firms") or []))
            if len(seen_seed_firms) == 1:
                return [_p("1", "Dallas Co", city="Dallas", state="TX")]
            return [_p("2", "Austin Co", city="Austin", state="TX")]

        in_area, rejects = discover_in_area(
            tool_context={"sources": {"s": {}}}, area=AREA, target_count=1,
            discover=discover, provider=_verifier({"Dallas Co": {"city": "Dallas", "state": "TX"}}),
            provider_config={}, parse_json_array=_parse,
        )
        assert seen_seed_firms[0] == []
        assert "Dallas Co" in seen_seed_firms[1]
        assert [p["id"] for p in in_area] == ["2"]
        assert [r["prospect_id"] for r in rejects] == ["1"]

    def test_a_round_that_finds_nothing_new_ends_the_loop(self):
        # An exhausted search must not be retried into a bill.
        calls = []

        def discover(ctx):
            calls.append(1)
            return [_p("1", "A", city="Austin", state="TX")]

        in_area, _ = discover_in_area(
            tool_context={"sources": {"s": {}}}, area=AREA, target_count=99,
            discover=discover, provider=_verifier({}), provider_config={},
            parse_json_array=_parse, max_rounds=5,
        )
        # Round 2 returns the same id → no fresh candidates → stop.
        assert len(calls) == 2
        assert len(in_area) == 1

    def test_rejection_records_the_VERIFIED_address_not_the_reported_one(self):
        # AEO writes prospects ON CONFLICT DO NOTHING, so the stored city can never be
        # corrected. The true address has to live in the verdict or it is lost.
        def discover(ctx):
            return [_p("1", "Mislabelled", city="Austin", state="TX", zip_code="99999")]

        # Reported as Austin, actually in Denver.
        _, rejects = discover_in_area(
            tool_context={"sources": {"s": {}}}, area=AREA, target_count=1,
            discover=discover,
            provider=_verifier({"Mislabelled": {"city": "Denver", "state": "CO", "confidence": "high"}}),
            provider_config={}, parse_json_array=_parse, max_rounds=1,
        )
        assert len(rejects) == 1
        data = rejects[0]["validation_data"]
        assert data["verified_location"]["city"] == "Denver"
        assert "Denver" in data["reasoning"]

    def test_an_unverifiable_prospect_is_KEPT(self):
        def discover(ctx):
            return [_p("1", "Ghost")]

        in_area, rejects = discover_in_area(
            tool_context={"sources": {"s": {}}}, area=AREA, target_count=1,
            discover=discover, provider=_verifier({}), provider_config={},
            parse_json_array=_parse, max_rounds=1,
        )
        assert [p["id"] for p in in_area] == ["1"]
        assert rejects == []

    def test_does_not_mutate_the_caller_context(self):
        # Exclusions are applied to a deep copy; a leaked seed_firms list would grow
        # across runs and eventually exclude the whole market.
        ctx = {"sources": {"s": {"seed_firms": ["Original"]}}}

        def discover(c):
            return [_p(str(len(c["sources"]["s"]["seed_firms"])), "X", city="Dallas", state="TX")]

        discover_in_area(
            tool_context=ctx, area=AREA, target_count=99, discover=discover,
            provider=_verifier({"X": {"city": "Dallas", "state": "TX"}}),
            provider_config={}, parse_json_array=_parse, max_rounds=2,
        )
        assert ctx["sources"]["s"]["seed_firms"] == ["Original"]
