"""Tests for zip discovery (Phase 0). Ours — the engine has no zip support at all.

Each of these pins a place where the convenient behaviour makes the phase either
decorative (scope/exclusions ignored) or unsafe (a non-zip persisted as a zip).
"""

from __future__ import annotations

import json

from aeo.phases.zip_discovery import (
    DISCOVERY_METHOD,
    discover_zips,
    target_markets,
    zips_as_markets,
)


def _provider(rows):
    payload = json.dumps(rows)

    def call(prompt, **kwargs):
        call.prompts.append(prompt)
        return payload
    call.prompts = []
    return call


def _parse(text: str) -> list[dict]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [p for p in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(p, dict)]


def _run(geography, rows, **kw):
    return discover_zips(
        geography, provider=_provider(rows), provider_config={},
        parse_json_array=_parse, **kw,
    )


class TestScope:
    def test_home_secondary_expands_both_sets(self):
        markets = target_markets({
            "home_markets": ["Austin, TX"],
            "secondary_markets": ["Denver, CO"],
            "include_scope": "HOME_SECONDARY",
        })
        assert markets == ["Austin, TX", "Denver, CO"]

    def test_home_only_does_NOT_expand_secondary(self):
        # Expanding secondary under a home-only scope silently widens targeting, and
        # nothing downstream would flag it.
        markets = target_markets({
            "home_markets": ["Austin, TX"],
            "secondary_markets": ["Denver, CO"],
            "include_scope": "HOME_ONLY",
        })
        assert markets == ["Austin, TX"]

    def test_an_absent_or_unknown_scope_falls_back_to_home_only(self):
        # Conservative on purpose: guessing the other way scans markets the org
        # excluded itself from, which costs money and yields unwanted prospects.
        for scope in (None, "", "SOMETHING_NEW"):
            markets = target_markets({
                "home_markets": ["Austin, TX"],
                "secondary_markets": ["Denver, CO"],
                "include_scope": scope,
            })
            assert markets == ["Austin, TX"], scope

    def test_flattens_state_keyed_market_maps(self):
        markets = target_markets({"home_markets": {"TX": ["Austin", "Dallas"]}})
        assert markets == ["Austin, TX", "Dallas, TX"]


class TestValidation:
    def test_rejects_a_non_zip_rather_than_persisting_it(self):
        # AEO's DTO accepts any string up to 10 chars, so "Austin" would persist and
        # then be searched as a location.
        rows = _run(
            {"home_markets": ["Austin, TX"]},
            [{"zip_code": "Austin"}, {"zip_code": "78701 area"}, {"zip_code": "78701"}],
        )
        assert [r["zip_code"] for r in rows] == ["78701"]

    def test_accepts_zip_plus_four(self):
        rows = _run({"home_markets": ["Austin, TX"]}, [{"zip_code": "78701-1234"}])
        assert rows[0]["zip_code"] == "78701-1234"

    def test_stamps_the_discovery_method_on_every_row(self):
        # So a later radius- or census-based implementation is distinguishable in the
        # data rather than silently mixed in.
        rows = _run({"home_markets": ["Austin, TX"]}, [{"zip_code": "78701"}])
        assert rows[0]["discovery_method"] == DISCOVERY_METHOD

    def test_drops_placeholder_strings_instead_of_storing_them(self):
        rows = _run(
            {"home_markets": ["Austin, TX"]},
            [{"zip_code": "78701", "city": "Unknown", "county": "N/A", "state": "TX"}],
        )
        assert "city" not in rows[0] and "county" not in rows[0]
        assert rows[0]["state"] == "TX"

    def test_keeps_numeric_fields_and_ignores_unparseable_ones(self):
        rows = _run(
            {"home_markets": ["Austin, TX"]},
            [{"zip_code": "78701", "population": "12345", "latitude": "not a number"}],
        )
        assert rows[0]["population"] == 12345.0
        assert "latitude" not in rows[0]


class TestExclusions:
    def test_an_excluded_market_actually_excludes(self):
        # Nothing downstream re-checks this: the discovery sweep searches whatever
        # geography it is handed.
        rows = _run(
            {"home_markets": ["Austin, TX"], "excluded_markets": ["Round Rock"]},
            [
                {"zip_code": "78701", "city": "Austin", "state": "TX"},
                {"zip_code": "78664", "city": "Round Rock", "state": "TX"},
            ],
        )
        assert [r["zip_code"] for r in rows] == ["78701"]

    def test_no_exclusions_keeps_everything(self):
        rows = _run(
            {"home_markets": ["Austin, TX"]},
            [{"zip_code": "78701", "city": "Austin"}, {"zip_code": "78664", "city": "Round Rock"}],
        )
        assert len(rows) == 2


class TestDedupeAndCaps:
    def test_dedupes_zips_shared_between_adjacent_markets(self):
        # A duplicate is duplicated discovery spend, not a duplicated result.
        rows = _run(
            {"home_markets": ["Austin, TX"], "secondary_markets": ["Round Rock, TX"],
             "include_scope": "HOME_SECONDARY"},
            [{"zip_code": "78701"}],
        )
        assert [r["zip_code"] for r in rows] == ["78701"]

    def test_caps_rows_per_market(self):
        rows = _run(
            {"home_markets": ["Austin, TX"]},
            [{"zip_code": f"7870{i}"} for i in range(5)],
            max_per_market=2,
        )
        assert len(rows) == 2

    def test_no_markets_means_no_calls_and_no_rows(self):
        provider = _provider([{"zip_code": "78701"}])
        rows = discover_zips({}, provider=provider, provider_config={}, parse_json_array=_parse)
        assert rows == []
        assert provider.prompts == []


class TestZipsAsMarkets:
    def test_renders_zip_with_city_and_state_for_grounding(self):
        # A bare number grounds far worse than a zip with its city attached.
        out = zips_as_markets([{"zip_code": "78701", "city": "Austin", "state": "TX"}], 10)
        assert out == ["78701, Austin, TX"]

    def test_falls_back_to_the_bare_zip_when_city_is_unknown(self):
        assert zips_as_markets([{"zip_code": "78701"}], 10) == ["78701"]

    def test_respects_the_search_cap(self):
        rows = [{"zip_code": f"7870{i}"} for i in range(5)]
        assert len(zips_as_markets(rows, 2)) == 2
