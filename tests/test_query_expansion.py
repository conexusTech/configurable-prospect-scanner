"""Tests for market expansion — the actual root cause of the geography drift.

The engine hands `queries` to the model verbatim and never substitutes `{market}`.
An unexpanded query carries NO geography and the model answers with the state's most
prominent firms, which is exactly the Austin→Dallas bug. These pin the fix.
"""

from __future__ import annotations

from aeo.phases.query_expansion import (
    MARKET_PLACEHOLDER,
    expand_queries,
    unexpanded_placeholders,
)


def _sources(*queries: str) -> dict:
    return {"s": {"name_field": "f", "fields": ["f"], "queries": list(queries)}}


class TestExpansion:
    def test_substitutes_every_market_into_the_template(self):
        out = expand_queries(_sources("firms near {market}"), ["Austin, TX", "Round Rock, TX"])
        assert out["s"]["queries"] == ["firms near Austin, TX", "firms near Round Rock, TX"]

    def test_a_template_without_the_placeholder_is_passed_through_once(self):
        # Some searches are legitimately geography-free (a national registry);
        # rewriting them would be an invention.
        out = expand_queries(_sources("national church registry"), ["Austin, TX", "Dallas, TX"])
        assert out["s"]["queries"] == ["national church registry"]

    def test_caps_the_product_of_templates_and_markets(self):
        # Each expanded query is a grounded search, and templates × markets multiplies.
        out = expand_queries(_sources("a {market}", "b {market}"), ["M1", "M2", "M3"], max_per_source=4)
        assert len(out["s"]["queries"]) == 4

    def test_no_markets_leaves_queries_exactly_as_authored(self):
        # Keeps the failure visible rather than silently searching for a literal
        # "{market}" — the caller warns and enforcement is skipped.
        out = expand_queries(_sources("firms near {market}"), [])
        assert out["s"]["queries"] == ["firms near {market}"]

    def test_does_not_mutate_the_caller_sources(self):
        src = _sources("firms near {market}")
        expand_queries(src, ["Austin, TX"])
        assert src["s"]["queries"] == ["firms near {market}"]


class TestStragglerDetection:
    def test_reports_a_query_that_would_carry_no_geography(self):
        # The most expensive failure mode this repo has seen: confident, well-formed,
        # geographically random results.
        strays = unexpanded_placeholders(_sources("firms near {market}"))
        assert len(strays) == 1 and MARKET_PLACEHOLDER in strays[0]

    def test_silent_once_everything_is_expanded(self):
        out = expand_queries(_sources("firms near {market}"), ["Austin, TX"])
        assert unexpanded_placeholders(out) == []
