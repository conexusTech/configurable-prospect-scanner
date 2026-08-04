"""Substitute the target markets into each source's query templates.

**This is the root cause of the geography drift, and nothing else was.**

The engine takes `source_cfg["queries"]` and hands each string to the model verbatim
(`build_prompt(..., query=query)`). It **never substitutes `{market}`**, and it never
reads `organization.markets` during discovery at all — `grep -i market` finds nothing
in its discovery path. That field feeds scoring's `region_bonus`, not searching.

So a config authored as `"church architecture firms near {market}"` reached the model
with the placeholder **unexpanded**. The model was asked for church architecture firms
"near {market}", received no geography whatsoever, and returned the most prominent
firms in the state — which for Texas architecture means Dallas. Every symptom follows
from that one fact:

- Austin/Round Rock zips went in and Dallas firms came out, run after run.
- Standalone tests "worked" only because they substituted the market by hand.
- Strict prompting could not help: there was no location in the prompt to be strict
  about.
- Enforcement rejected everything, correctly, because nothing in-area was ever
  searched for.

Expanding here rather than asking config authors to pre-expand is deliberate: the
markets are not known until zip discovery has run, so the config cannot contain them.

## Cost

One query per (template × market). Three templates over five markets is fifteen
searches, so `cap` bounds the product — markets are consumed in order, which is why
zip discovery returns them primary-market-first.
"""

from __future__ import annotations

import copy
from typing import Any

#: Placeholder a config query uses for the market. Chosen to match the convention
#: already present in authored configs (and in the AV skill's own examples).
MARKET_PLACEHOLDER = "{market}"

#: Upper bound on expanded queries per source. A config with several templates and a
#: metro's worth of markets multiplies fast, and each one is a grounded search.
DEFAULT_MAX_QUERIES_PER_SOURCE = 12


def expand_queries(
    sources: dict[str, Any],
    markets: list[str],
    *,
    max_per_source: int = DEFAULT_MAX_QUERIES_PER_SOURCE,
) -> dict[str, Any]:
    """Return `sources` with every `{market}` placeholder resolved.

    A template without the placeholder is passed through once, unchanged — some
    searches are legitimately geography-free (a national registry, say), and rewriting
    them would be an invention.

    With no markets, templates are left exactly as authored. That keeps the failure
    visible rather than silently searching for a literal "{market}": the caller logs
    the empty-geography case, and enforcement is skipped for the same reason.
    """
    if not markets:
        return sources

    out = copy.deepcopy(sources)
    for source in out.values():
        if not isinstance(source, dict):
            continue
        templates = source.get("queries") or []
        expanded: list[str] = []
        for template in templates:
            text = str(template)
            if MARKET_PLACEHOLDER not in text:
                expanded.append(text)
                continue
            for market in markets:
                expanded.append(text.replace(MARKET_PLACEHOLDER, market))
                if len(expanded) >= max_per_source:
                    break
            if len(expanded) >= max_per_source:
                break
        source["queries"] = expanded[:max_per_source]
    return out


def unexpanded_placeholders(sources: dict[str, Any]) -> list[str]:
    """Queries that still contain the placeholder — a scan about to search for nothing.

    Worth checking explicitly rather than trusting the expansion: a query reaching the
    model with `{market}` in it produces confident, well-formed, geographically random
    results, which is the single most expensive failure mode this repo has seen.
    """
    stragglers: list[str] = []
    for name, source in (sources or {}).items():
        if not isinstance(source, dict):
            continue
        for query in source.get("queries") or []:
            if MARKET_PLACEHOLDER in str(query):
                stragglers.append(f"{name}: {query}")
    return stragglers
