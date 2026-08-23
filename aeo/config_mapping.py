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

#: Record fields every vertical has, appended to whatever a skill authored.
#:
#: AEO declares `website` and `industry` on its prospect callback, `PROSPECT_PASSTHROUGH`
#: already names both, and the engine already resolves them onto columns — `website` via
#: `_IDENTITY_ALIASES["website_url"]`, `industry` under its authored name. All of that was
#: in place and still produced NULL columns, because a source only receives what it ASKS
#: the model for, and two skills in a row asked for neither: `commercial-flooring` and
#: `commercial-hvac-mechanical-services` (the live production skill). Measured on a real
#: run after adding them to one config: website 0/60 -> 58/60, industry 0/60 -> 60/60.
#:
#: Appended here rather than left to per-skill authoring because the requirement is not a
#: vertical judgement — it is a property of AEO's `prospects` table, so N configs is N
#: chances to forget, and forgetting is silent (a NULL column on a scan that reports
#: success). The builder authoring it instead would fix only skills written afterwards,
#: probabilistically, while every existing skill stayed broken.
#:
#: ⚠️ This is NOT the default that design rule 1 forbids. That rule is about **who gets
#: scanned** — queries and seed firms — where a default is "a wrong answer wearing a
#: confident face". This changes only what is captured about a record already discovered,
#: and it mirrors what the engine does for city / state / zip_code / contact_title
#: regardless of what a skill authored.
#: `event_date`/`event_type` joined them 2026-08-23, for the same reason one layer up.
#: Every completed run yields a pipeline stage — that is AEO's ruling, not a vertical's
#: preference — and a stage is a judgement about TIMING. So a skill that asks for no date
#: cannot be staged by anything, however good the judge is. Measured across the whole
#: production copy: `commercial-hvac-mechanical-services` collected **0 date-shaped fields
#: on 497 prospects** (its four sources ask for `permit_status`, `bid_title`, `scope`,
#: `firm_type` and no date at all), so all 37 judged rows on its last run were placed at
#: the entry rung — correctly, on the nothing they carried.
#:
#: 🔑 **The model decides WHICH event matters, not us.** The prompt asks for "the single
#: most decisive dated event" and lets the model name it in `event_type`; nothing here
#: enumerates `permit_date` vs `bid_due_date` vs `closing_date`, because that list is a
#: vertical judgement and would need re-authoring per industry — the exact shape the
#: scanner is required not to have. A skill that already asks for its own dated field
#: keeps it: `with_universal_fields` appends, never substitutes, and
#: `_resolve_signal_fields` unions both.
UNIVERSAL_RECORD_FIELDS = ("website", "industry", "event_date", "event_type")

#: The universal pair above, excluded from the COMPLETENESS denominator by
#: `aeo/runner.py`. `fields` does double duty — merge vocabulary and
#: `filled / len(fields)` — so appending two keys to every source would quietly dock
#: every prospect discovered from a source that legitimately has no event (a property
#: directory listing, a firm register). Capturing a signal must not penalise the records
#: that honestly lack it.
UNIVERSAL_TIMING_FIELDS = ("event_date", "event_type")

#: Spellings the engine already folds into `website_url` (`_IDENTITY_ALIASES`). If a skill
#: authored any of them, asking again as `website` would duplicate the key in the prompt.
_WEBSITE_SPELLINGS = frozenset({"websiteurl", "website", "url", "domain"})


def _norm_field(name: Any) -> str:
    """Case- and punctuation-insensitive field-name key, matching how the engine's
    `_norm_key` compares authored names against what a model returns."""
    return "".join(ch for ch in str(name).lower() if ch.isalnum())


def with_universal_fields(fields: Any) -> list[str]:
    """Authored fields, plus any UNIVERSAL_RECORD_FIELDS the skill did not already ask
    for. Order is preserved and the authored names win — appended, never substituted."""
    out = [str(f) for f in (fields or [])]
    seen = {_norm_field(f) for f in out}
    for extra in UNIVERSAL_RECORD_FIELDS:
        if extra == "website" and (seen & _WEBSITE_SPELLINGS):
            continue
        if _norm_field(extra) in seen:
            continue
        out.append(extra)
        seen.add(_norm_field(extra))
    return out


# Provider defaults. `model`, `temperature` and `retry_attempts` are deliberately NOT
# read from the skill config: they are deployment concerns that change with our
# infrastructure, not authoring decisions an operator made in a chat.
#
# ⚠️ **`entries_per_query` is the exception, and grouping it here was a categorisation
# error.** It is not infrastructure — it is the YIELD knob, and it was the binding cap on
# every scan: 13 geo-anchored queries x 3 entries = 39 raw, 36 after dedup, on two
# consecutive production runs that produced identical counts from different companies.
# How many candidates a query should return is exactly the kind of decision an operator
# makes, so `discovery.entries_per_query` now overrides it (see `build_tool_context`).
DEFAULT_PROVIDER = {
    "model": "gemini-3-flash-preview",
    #: Model for the AI **judgment** phase only. A deliberate second tier, not an
    #: oversight.
    #:
    #: 🔑 **Measured, not assumed.** Four real rows from run `9f9fe2d7` were judged by
    #: both tiers. Three came out identically and correctly on each — a 2019 lease and a
    #: 2011 "current occupant" were both recognised as TENURE rather than an event, and a
    #: June-2026 lease signing became `5 - Decision Imminent` because the fit-out is
    #: still ahead. They diverged on a mechanical/electrical permit dated 2026-02-20:
    #:
    #:   flash -> `5 - Decision Imminent`  "well into construction, approaching the
    #:                                      finishing phase where flooring happens"
    #:   pro   -> `6 - Likely Awarded`     "permit issued 6 months ago, so if flooring is
    #:                                      involved the contractor was likely already
    #:                                      selected"
    #:
    #: Pro is right, and the distinction is the whole ladder: these rungs measure when
    #: the **decision** happens, not when the work happens. Flash reasoned about the work.
    #:
    #: ⚠️ Judgment only. Discovery is retrieval-with-grounding where flash is already
    #: enough, and it issues grounded search queries — paying pro rates there buys
    #: nothing. Judgment is the one phase whose output a human acts on directly.
    #:
    #: ⚠️ **Pinned, not `gemini-pro-latest`.** A floating alias would change the model
    #: under a running pipeline with no deploy, so sales stages would shift silently —
    #: the same reason production pins an image DIGEST rather than a tag. This IS a
    #: preview id and so can move on Google's schedule; that is the accepted cost of the
    #: better reasoning, and it is survivable because the judgement's reasoning is
    #: persisted to `ai_analysis`, so a shift is explainable rather than invisible.
    "judgment_model": "gemini-3.1-pro-preview",
    "temperature": 0.1,
    "entries_per_query": 3,
    "retry_attempts": 3,
}


#: Injected as a source's `prompt` when the authored config does not supply one.
#:
#: **Why this is not just wording.** The engine's default prompt mentions the market
#: only inside `Search for: {query}` — one line among several, weighted against
#: whatever else grounded search surfaces. Measured live: Austin and Round Rock zips
#: went in, *Dallas* firms came out, three runs in a row. Big-metro firms outrank
#: small-town ones in search, so the drift is systematic rather than random.
#:
#: With the location requirement stated first, named as the primary filter, and paired
#: with explicit permission to return FEWER results, the same model returned Austin
#: firms for Austin zips and Round Rock firms for Round Rock zips. The permission
#: matters as much as the instruction: "return EXACTLY {n}" pressures a model to pad
#: a thin area with plausible out-of-area names.
#:
#: This is the soft half of the fix. `aeo/phases/geo_filter.py` is the guarantee —
#: prompts persuade, verification enforces, and only one of those is reliable.
#:
#: An authored source that defines its own `prompt` keeps it; this only fills a gap.
GEO_STRICT_PROMPT = """You are a research analyst finding prospects for the following business:

{product_description}

Search for: {query}

**STRICT LOCATION REQUIREMENT — apply this before anything else.** The search text
above names a specific area. Return ONLY organizations whose own business address is
in that city or its immediate suburbs. Do NOT return firms headquartered in other
metropolitan areas, however well known or otherwise well matched. If you cannot find
{n} organizations in that area, return FEWER — an empty array is a correct answer
when the area genuinely has none.

For each result gather the fields listed below. Be accurate; leave a field blank
rather than guessing.

**`event_date` and `event_type` — the timing signal.** Report the single most
decisive dated event you can find for this organization: the one a salesperson would
act on. YOU decide what that is for this kind of record and this industry — a permit
issued, a lease signed, a facility acquired, a contract awarded, a bid closing, a
funding round, a site announced. Put the date in `event_date` (as precisely as the
source gives it — "2026-03-14", "March 2026" and "2019" are all acceptable) and name
the event in `event_type` in your own words. Leave BOTH blank if the record genuinely
has no dated event; a tenure fact ("occupant since 2011") is not an event, and a
guessed date is worse than none.
{seed_context}
Return AT MOST {n} results as a JSON array. Each object must have these keys:
{keys}

Return ONLY the JSON array, no other text."""


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


def _authored_yield(config: dict[str, Any]) -> dict[str, Any]:
    """`discovery.entries_per_query`, when the skill authored one.

    Returns an override fragment rather than a value so the provider block keeps its
    single merge order and a bad value simply changes nothing. Non-positive and
    unparseable values are ignored: a zero here would silently return no prospects at
    all, which is worse than a low cap.
    """
    raw = (config.get("discovery") or {}).get("entries_per_query")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return {}
    return {"entries_per_query": value} if value > 0 else {}


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

    # The pipeline-stage vocabulary AEO publishes on the runtime context. Refused
    # rather than defaulted, for the same reason as `scoring`: the AI judgment phase
    # decides a SALES stage from it, and rungs we invented here would put prospects in
    # columns nobody defined. AEO ships this for every scraping org — a declared
    # `config.pipeline.stages[]` or the shared ladder — so an absence means the gateway
    # is older than this scanner, which is worth failing loudly on.
    pipeline = context.get("pipeline") or {}
    stages = pipeline.get("stages") if isinstance(pipeline, dict) else None
    if not isinstance(stages, list) or not stages:
        problems.append(
            "`pipeline.stages` is missing from the runtime context. The scoring "
            "phase judges each prospect into these rungs, so without them there is "
            "no vocabulary to decide between — and inventing one would file "
            "prospects under stages nobody defined. Upgrade the gateway, or check "
            "that GET /runtime/organizations/{id}/context returns a `pipeline` block."
        )
    elif not all(isinstance(s, dict) and s.get("key") for s in stages):
        problems.append(
            "`pipeline.stages` contains an entry with no `key`. The key is the exact "
            "value AEO validates on the callback, so a rung without one can never be "
            "persisted."
        )

    if problems:
        raise UnmappedConfigError(problems)

    return {
        # `sales_cycle_months` is the org's real decision lead time. It was already
        # exposed by the gateway's runtime context and reachable by nothing: the engine
        # instead used a hardcoded 13-month church-construction cycle. Passed through
        # verbatim (None when the org has not set it) so the engine can tell "not set"
        # from a value and fall back to its own default only then.
        "organization": {
            "name": org_name,
            "markets": markets,
            "sales_cycle_months": org.get("sales_cycle_months",
                                          context.get("sales_cycle_months")),
            # The commissioning org's own identity, so discovery can exclude it from its
            # own prospect list. On the first real production run the org was discovered
            # as a prospect for itself and survived every filter — rejected only by luck
            # of an unrelated disqualifier it happened to match.
            "aliases": org.get("aliases") or [],
            "exclusions": org.get("exclusions") or [],
        },
        "product_description": product_description,
        "gemini": {**DEFAULT_PROVIDER, **(provider or {}), **_authored_yield(config)},
        "output": {"path": output_path, "top_n": top_n},
        "sources": _with_geo_strict_prompt(
            # Order matters only for readability; both are pure. The fan-out runs on
            # the RESOLVED config, so `lookalike_sources` arrives as the org's real
            # customer list rather than as a `context_ref` binding.
            fan_out_seed_firms(sources, (config.get("discovery") or {}).get("lookalike_sources")),
            product_description,
        ),
        "scoring": scoring,
        # The pipeline-stage vocabulary the AI judgment phase decides INTO, straight
        # from AEO's runtime context (`context["pipeline"]`).
        #
        # 🔑 **A 7th top-level key, and the first addition since this mapping was
        # written.** The engine does not read it — `aeo/phases/ai_judgment.py` does. It
        # is carried here rather than passed separately because `build_tool_context` is
        # the one place that turns an AEO context into something the phases consume, and
        # a second channel would be a second thing to forget.
        #
        # ⚠️ NOT defaulted. A missing vocabulary is refused above, next to the other
        # `UnmappedConfigError` faults, because the alternative is judging into rungs we
        # invented — which is design rule 1 ("a default here is a wrong answer wearing a
        # confident face") applied to the sales stage instead of to discovery.
        "pipeline": pipeline,
    }


def fan_out_seed_firms(
    sources: dict[str, Any], lookalike_sources: Any
) -> dict[str, Any]:
    """Populate every source's `seed_firms` from the R12-bound `lookalike_sources`.

    **This closes a library-invariant hole, and it is the reason `seed_firms` must
    never be authored.** Seed firms are the commissioning org's own existing customers
    — org-specific by definition. Authored as a literal inside `discovery.sources` they
    passed the config schema (section internals are `additionalProperties: true`),
    passed R12's binding checks (not an enumerated position), and finalized — after
    which **the next org to connect that skill would search using the first org's
    customer list.** Found by aeo-agent-service reading this repo (thread #17).

    The fix is deliberately split across the two places that can each only do half:
    aeo-backend's lint now **rejects** an authored `seed_firms`, and this fans the
    bound value in at scan time so the capability survives. A fan-out alone would have
    left the hole open for anyone who authored the literal anyway.

    Fanning to *every* source is correct rather than lazy: the engine renders seed firms
    as "do NOT return these; find ADDITIONAL firms", and an org's existing customers
    should be excluded from every source, not just one.
    """
    if not lookalike_sources:
        return sources

    if isinstance(lookalike_sources, list):
        firms = [str(v).strip() for v in lookalike_sources if str(v).strip()]
    else:
        text = str(lookalike_sources).strip()
        firms = [text] if text else []
    if not firms:
        return sources

    out: dict[str, Any] = {}
    for name, source in sources.items():
        if not isinstance(source, dict):
            out[name] = source
            continue
        # Union rather than overwrite, and order-preserving: a source may legitimately
        # carry runtime-populated firms from an earlier step in future.
        existing = [str(v) for v in (source.get("seed_firms") or [])]
        merged = existing + [f for f in firms if f not in existing]
        out[name] = {**source, "seed_firms": merged}
    return out


def _with_geo_strict_prompt(
    sources: dict[str, Any], product_description: str
) -> dict[str, Any]:
    """Give every source a geographically-strict prompt unless it authored its own.

    The engine reads `source_cfg["prompt"]` and falls back to its own template, whose
    location handling is too weak to hold (see GEO_STRICT_PROMPT). Filling the gap
    here rather than asking every config author to remember means the default
    behaviour is the correct one.

    `product_description` and `keys` are substituted now because the engine only
    passes `{query}`, `{n}` and `{seed_context}` to an authored template — a
    `{keys}` left in it would raise `KeyError` at format time.
    """
    out: dict[str, Any] = {}
    for name, source in sources.items():
        if not isinstance(source, dict):
            out[name] = source
            continue
        if source.get("prompt"):
            out[name] = source
            continue
        fields = with_universal_fields(
            source.get("fields") or ["organization_name", "city", "state"]
        )
        out[name] = {
            **source,
            # ⚠️ Write the appended fields back into `fields` too, not just into the
            # prompt. `fields` is the MERGE VOCABULARY: `canonical_fields_from_sources`
            # reads it, and `_merge_raw_rows` keeps only those names plus
            # `_IDENTITY_ALIASES`. Asking the model for a key we then omit from the
            # vocabulary means the value arrives and is silently discarded.
            #
            # That is exactly what happened to `industry`, and the asymmetry is why it
            # went unnoticed for so long: `website` survived regardless, because
            # `_IDENTITY_ALIASES["website_url"]` folds the spelling `website` and the
            # alias keys are appended to the vocabulary unconditionally. `industry` has
            # no alias entry, so it had nothing to ride in on. Measured across every
            # production run since the prompt started asking: website 0 -> 127/147,
            # industry 0/N on every single run. One model returned both; one column got
            # filled. Same defect shape as the locality write-back in `av_lead_scanner`
            # — prompt and merge are two views of one fact, and only one was updated.
            #
            # The authored-prompt path above deliberately does NOT get this. There we
            # never asked, so claiming the field would be a lie with a cost: an
            # unfillable name in `canonical` enlarges the `completeness` denominator
            # (`filled / len(fields)`) and drags every prospect's score down. The
            # invariant is `fields` == what we actually asked for.
            "fields": fields,
            "prompt": GEO_STRICT_PROMPT.replace(
                "{product_description}", product_description or "(not specified)"
            ).replace("{keys}", ", ".join(str(f) for f in fields)),
        }
    return out


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
