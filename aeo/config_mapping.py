"""Map an AEO runtime context onto the vendored tool's context shape.

**This module is the whole point of this repo.** The vendored engine
(`av_lead_scanner.py`) reads a flat context with top-level `organization`,
`product_description`, `gemini`, `output`, `sources` and `scoring`. AEO's
`GET /runtime/organizations/{id}/context` returns something different: org columns,
resolved geography, personas, products — and the authored skill recipe nested under
`skill.config`, in the schema the Conversational Skill Builder emits.

Those two shapes were never reconciled. Upstream's integration example asserts the
AEO endpoint returns "the SAME shape as examples/organization.json"; it does not,
and four of the six top-level keys the engine reads are absent from it. Left
unmapped, a conversationally-authored config reaches the engine and is **silently
ignored** — the scan runs on defaults, returns plausible prospects, and nothing
errors. That is the failure mode this file exists to make impossible.

Design rules, in order of importance:

1. **Never invent discovery strategy.** Queries and seed firms decide who gets
   scanned. A default here is not a smaller feature, it is a wrong answer wearing
   a confident face. Anything undetermined raises `UnmappedConfigError`.
2. **Do not touch the vendored engine.** This maps *into* its contract; it does
   not change to suit us. See UPSTREAM.md.
3. **Prefer the authored config over org data** where both could supply a field.
   The config is what an operator accepted in the builder; org data is the
   substrate it was authored against.
"""

from __future__ import annotations

from typing import Any

# Keys the engine requires on every entry of `sources`. `seed_firms` is optional —
# upstream's own example ships sources with an empty list (municipal_permits,
# planning_board), so requiring it would reject a legitimate config.
SOURCE_REQUIRED_KEYS = ("name_field", "fields", "queries")

# Provider defaults. Deliberately NOT read from the skill config: model choice and
# retry policy are deployment concerns that change with our infrastructure, not
# authoring decisions an operator made in a chat. Overridable by env at run time.
DEFAULT_PROVIDER = {
    "model": "gemini-3-flash-preview",
    "temperature": 0.1,
    "entries_per_query": 3,
    "retry_attempts": 3,
}


class UnmappedConfigError(ValueError):
    """A config section the engine needs is missing or not expressible.

    Raised rather than defaulted, and it names every problem at once so an
    operator repairs in one pass instead of discovering them one run at a time.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(
            "Skill config cannot be mapped onto the scanner's context:\n  - "
            + joined
        )


def _resolved_markets(context: dict[str, Any]) -> list[str]:
    """Flatten AEO's resolved geography into the engine's flat market list.

    AEO resolves `geography.home_markets` per-org at scan time — that is the whole
    R12 mechanism, and it is why a builder-authored config carries a `context_ref`
    here rather than a literal. By the time we see the context the reference is
    already resolved, so this only has to flatten whatever shape it resolved to.
    """
    geo = context.get("geography") or {}
    markets: list[str] = []
    for key in ("home_markets", "secondary_markets"):
        value = geo.get(key)
        if isinstance(value, list):
            markets.extend(str(v) for v in value if v)
        elif isinstance(value, dict):
            # {state: [cities]} — upstream's own geography used this shape.
            for state, cities in value.items():
                if isinstance(cities, list):
                    markets.extend(f"{c}, {state}" for c in cities if c)
    # Order-preserving dedupe: markets drive query fan-out, and a duplicate is a
    # duplicated spend rather than a duplicated result.
    return list(dict.fromkeys(markets))


def build_tool_context(
    context: dict[str, Any],
    *,
    output_path: str = "prospects.json",
    top_n: int = 50,
    provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the engine's context from an AEO runtime context.

    `context` is the parsed body of `GET /runtime/organizations/{id}/context`.
    Raises `UnmappedConfigError` if the authored config cannot drive a real scan.
    """
    problems: list[str] = []

    skill = context.get("skill") or {}
    config: dict[str, Any] = skill.get("config") or {}
    if not config:
        problems.append(
            "`skill.config` is empty — the org has no authored recipe, so there "
            "is nothing to scan with. Connect a finalized skill to this org."
        )

    org = context.get("organization") or {}
    org_name = org.get("name")
    if not org_name:
        problems.append("`organization.name` is missing from the runtime context.")

    markets = _resolved_markets(context)
    if not markets:
        problems.append(
            "No resolved markets. The config's `geography.home_markets` binding "
            "resolved to nothing for this org — check the org's onboarding "
            "geography, not the skill."
        )

    product_description = config.get("product_description")
    if not product_description:
        # Fall back to the org's own products before failing: the description is
        # what the model uses to judge fit, and an org that never authored one in
        # the builder may still have products from onboarding.
        products = context.get("products_services") or []
        if products and isinstance(products, list):
            first = products[0]
            if isinstance(first, dict):
                product_description = first.get("description") or first.get("name")
    if not product_description:
        problems.append(
            "No `product_description` in the skill config and no products on the "
            "org — the engine cannot judge prospect fit without knowing what is "
            "being sold."
        )

    # ── discovery → sources ────────────────────────────────────────────────
    #
    # The engine's `sources` is the discovery strategy: named buckets, each with
    # search queries and optional seed firms. Our config schema's `discovery`
    # section is `additionalProperties: true` because its internals were never
    # ratified — so this mapping is also a PROPOSAL for what they should be:
    # `discovery.sources` carrying the engine's own source shape verbatim.
    #
    # Proposing it from a working implementation rather than from a design
    # discussion is deliberate. It is the one section whose shape is already
    # settled by something that executes.
    sources = (config.get("discovery") or {}).get("sources")
    if not isinstance(sources, dict) or not sources:
        problems.append(
            "`discovery.sources` is missing or empty. The engine needs at least "
            "one named source with `queries` — it will not guess who to look for. "
            "Expected shape: {\"<source_key>\": {\"name_field\": str, "
            "\"fields\": [...], \"queries\": [...], \"seed_firms\": [...]}}."
        )
    else:
        for key, source in sources.items():
            if not isinstance(source, dict):
                problems.append(f"`discovery.sources.{key}` is not an object.")
                continue
            missing = [k for k in SOURCE_REQUIRED_KEYS if not source.get(k)]
            if missing:
                problems.append(
                    f"`discovery.sources.{key}` is missing: {', '.join(missing)}."
                )

    # ── scoring ────────────────────────────────────────────────────────────
    #
    # Passed through rather than translated: the engine's scoring is described as
    # "deterministic, config-driven", so its factor vocabulary IS the contract.
    # Absence is a hard stop, not a default — an unscored scan ranks nothing, and
    # a caller reading `top_n` off an unranked list gets an arbitrary slice that
    # looks like a shortlist.
    scoring = config.get("scoring")
    if not isinstance(scoring, dict) or not scoring:
        problems.append(
            "`scoring` is missing. Without it the engine cannot rank, and an "
            "arbitrary slice of results would be indistinguishable from a "
            "shortlist."
        )

    if problems:
        raise UnmappedConfigError(problems)

    return {
        "organization": {"name": org_name, "markets": markets},
        "product_description": product_description,
        "gemini": {**DEFAULT_PROVIDER, **(provider or {})},
        "output": {"path": output_path, "top_n": top_n},
        "sources": sources,
        "scoring": scoring,
    }


# ── What this mapping does NOT cover, stated rather than silently dropped ────
#
# The PRD's scanner has five phases: zip discovery, discovery sweep, signal
# validation, decision-maker search, scoring. The vendored engine exposes
# `discover` and `score`. So the config sections `validation` and `contacts` have
# **no destination in this engine today** and are intentionally not mapped.
#
# They are not silently dropped either: `assert_phase_coverage` below is meant to
# be called by the runner so an operator who authored a validation or contacts
# section is told it will not run, rather than discovering it from absent output.
UNSUPPORTED_SECTIONS = ("validation", "contacts")


def unsupported_authored_sections(context: dict[str, Any]) -> list[str]:
    """Sections the operator authored that this engine cannot execute.

    Returns names, not a boolean, so the caller can name them to the user. Empty
    list means full coverage of what was authored.
    """
    config = (context.get("skill") or {}).get("config") or {}
    return [
        name
        for name in UNSUPPORTED_SECTIONS
        if isinstance(config.get(name), dict) and config.get(name)
    ]
