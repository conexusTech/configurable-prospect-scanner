#!/usr/bin/env python3
"""av-lead-scanner — a generic, LLM-agnostic prospecting workflow tool.

This is a self-contained CLI that any LLM (Claude, Gemini, …) can drive as a
tool. It has no dependency on the outer scaffold repo, no hardcoded business
domain, and no output side-effects beyond what the caller asks for. All
domain knowledge — what to search for, how to score, what to emit — is passed
in as an *organization context* (see `examples/organization.json`). The
church/AV configuration that ships in `examples/` is just one such context.

The tool exposes three phases behind three subcommands:

    discover   Run the context's discovery sources through a grounded-search
               provider (default: Gemini), dedupe the union by normalized
               company name, and emit a `prospects` event per source.
    score      Deterministically score + rank prospects (NO network, NO API
               key): cross-source merge, config-defined factors, pipeline
               timing, rank. Emits one `scored` event.
    run        discover → score → completed, in one invocation.

Output is an *event stream* — the sink is chosen by the caller:

    --out <file>   (default)  persist the aggregated result to a JSON file.
    --out -                   stream newline-delimited JSON (NDJSON) events to
                              stdout, one per line, as they happen — so an
                              orchestrator can forward each to its own sink
                              (e.g. AEO's POST /runtime/scans/{id}/events).

Events:
    {"type":"phase_start","phase":<name>}
    {"type":"prospects","phase":<source>,"items":[...]}
    {"type":"scored","phase":"score","items":[...]}
    {"type":"phase_complete","phase":<name>,"count":<int>}
    {"type":"completed","summary":{...}}
    {"type":"error","message":<str>}

See SKILL.md for the full contract an LLM follows to construct inputs and
consume outputs.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

log = logging.getLogger("av_lead_scanner")

# UUID namespace for deterministic prospect IDs (RFC-4122 NAMESPACE_URL).
_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

# Timeout ceiling per grounded-search call, seconds. One slow query must not
# sink the whole run.
_QUERY_TIMEOUT_S = 180.0


# =============================================================================
# Context loading & validation
# =============================================================================

# Fields a valid context must supply for `discover`. `score` is more lenient
# (it can run on caller-supplied prospects with no `sources` at all).
_MIN_DISCOVER_KEYS = ("sources",)


def load_context(path_or_dash: str) -> dict[str, Any]:
    """Load the organization context JSON from a file path or '-' (stdin)."""
    if path_or_dash == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path_or_dash).read_text(encoding="utf-8")
    try:
        ctx = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"context is not valid JSON: {exc}") from exc
    if not isinstance(ctx, dict):
        raise ValueError("context must be a JSON object")
    return ctx


def validate_context(ctx: dict[str, Any], *, need_sources: bool) -> list[str]:
    """Return a list of human-readable problems. Empty list == valid.

    Kept as a pure function so the AEO wrapper (Scenario 2) can call it to
    validate the aeo-backend context BEFORE running, and so tests can assert
    on it without side-effects.
    """
    problems: list[str] = []
    if need_sources:
        sources = ctx.get("sources")
        if not isinstance(sources, dict) or not sources:
            problems.append("context.sources must be a non-empty object")
        else:
            for name, cfg in sources.items():
                if not isinstance(cfg, dict):
                    problems.append(f"source {name!r} must be an object")
                    continue
                if not cfg.get("queries"):
                    problems.append(f"source {name!r} has no queries")
    return problems


def organization_name(ctx: dict[str, Any]) -> str:
    """Best-effort organization display name (used for default output paths)."""
    org = ctx.get("organization")
    if isinstance(org, dict) and org.get("name"):
        return str(org["name"])
    return str(ctx.get("organization_name") or "organization")


def _slug(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_-]+", "-", text) or "organization"


# =============================================================================
# Name normalization & dedup helpers
# =============================================================================


def normalize_name(name: str) -> str:
    """Collapse a company name to a dedup key.

    Ported verbatim from the legacy score_leads.normalize_name so cross-source
    dedup matches the original behavior: lowercase, drop common corporate
    suffixes, strip parentheticals and punctuation, squeeze whitespace.
    """
    name = (name or "").lower().strip()
    for suffix in (", inc.", ", inc", " inc.", " inc", " llc", " corp"):
        name = name.replace(suffix, "")
    name = re.sub(r"\([^)]*\)", "", name).strip()
    name = re.sub(r"[^\w\s]", "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


# =============================================================================
# Grounded-search provider seam
# =============================================================================
#
# The tool talks to exactly one provider function with this signature:
#
#     search(prompt: str, *, model, temperature, retry_attempts, timeout_s) -> str
#
# returning the model's raw text (the tool parses the JSON array out). This
# thin seam is what makes the discovery path swappable/mockable: two real
# providers ship (Gemini with Google Search grounding, and Claude/Anthropic
# with the web-search tool), `--mock` substitutes a deterministic offline
# generator, and tests monkeypatch it directly. Adding another provider is a
# new function of this signature plus one registry entry — no other change.

SearchProvider = Callable[..., str]

_gemini_client: Any = None
_claude_client: Any = None
_client_lock = threading.Lock()  # guards lazy client construction under concurrency


def gemini_provider(
    prompt: str,
    *,
    model: str,
    temperature: float,
    retry_attempts: int,
    timeout_s: float,  # noqa: ARG001 — enforced by the caller via signal/thread
) -> str:
    """Default provider: Gemini with Google Search grounding.

    Lazy-imports google.genai so the module imports fine (for `score` and for
    tests) in environments without the SDK or an API key.
    """
    import time

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set — required for `discover` (or use --mock)")

    from google import genai  # noqa: PLC0415
    from google.genai import types  # noqa: PLC0415

    global _gemini_client
    if _gemini_client is None:
        with _client_lock:
            if _gemini_client is None:
                _gemini_client = genai.Client(api_key=api_key)

    last_exc: Exception | None = None
    for attempt in range(retry_attempts):
        try:
            resp = _gemini_client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())],
                    temperature=temperature,
                ),
            )
            return resp.text or ""
        except Exception as exc:  # noqa: BLE001 — retry orchestration
            last_exc = exc
            msg = str(exc)
            if attempt == retry_attempts - 1:
                break
            wait = 15 * (2 ** attempt) if ("429" in msg or "RESOURCE_EXHAUSTED" in msg) else 5
            log.warning("gemini retry %d/%d in %ds: %s", attempt + 1, retry_attempts, wait, msg[:200])
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


# Claude models that support the modern web-search tool (dynamic filtering).
# Everything else falls back to the basic web_search_20250305 variant.
_CLAUDE_MODERN_MARKERS = ("opus-4-8", "opus-4-7", "opus-4-6", "sonnet-5", "sonnet-4-6", "fable-5", "mythos-5")


def _claude_web_search_tool(model: str) -> str:
    m = (model or "").lower()
    return "web_search_20260209" if any(k in m for k in _CLAUDE_MODERN_MARKERS) else "web_search_20250305"


def claude_provider(
    prompt: str,
    *,
    model: str,
    temperature: float,  # noqa: ARG001 — Opus 4.8/4.7 reject temperature; steered by prompt
    retry_attempts: int,
    timeout_s: float,
) -> str:
    """Alternative provider: Claude (Anthropic) with the web-search server tool.

    Lazy-imports the anthropic SDK so the module imports fine (for `score` and
    tests) without it. Uses the Anthropic-hosted `web_search` tool for grounding
    and returns the model's final answer text (the tool parses the JSON array
    out). `temperature` is intentionally NOT passed — Opus 4.8/4.7 reject it;
    steer output through the prompt instead. Auth resolves from ANTHROPIC_API_KEY
    or an `ant auth login` profile via the zero-arg client.
    """
    import time

    import anthropic  # noqa: PLC0415

    global _claude_client
    if _claude_client is None:
        with _client_lock:
            if _claude_client is None:
                _claude_client = anthropic.Anthropic()  # resolves ANTHROPIC_API_KEY or a profile

    client = _claude_client.with_options(timeout=timeout_s)
    tools = [{"type": _claude_web_search_tool(model), "name": "web_search"}]

    last_exc: Exception | None = None
    for attempt in range(retry_attempts):
        try:
            messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
            final_text = ""
            # Server-tool loop: web_search can pause the turn (stop_reason=pause_turn)
            # to run more searches; re-send to resume. Bounded so a runaway can't hang.
            for _ in range(6):
                resp = client.messages.create(model=model, max_tokens=8192, tools=tools, messages=messages)
                if resp.stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": resp.content})
                    continue
                final_text = "".join(
                    b.text for b in resp.content if getattr(b, "type", None) == "text"
                )
                break
            return final_text
        except Exception as exc:  # noqa: BLE001 — retry orchestration
            last_exc = exc
            msg = str(exc).lower()
            if attempt == retry_attempts - 1:
                break
            wait = 15 * (2 ** attempt) if ("429" in msg or "rate" in msg or "overloaded" in msg) else 5
            log.warning("claude retry %d/%d in %ds: %s", attempt + 1, retry_attempts, wait, str(exc)[:200])
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def mock_provider(
    prompt: str,
    *,
    model: str,  # noqa: ARG001
    temperature: float,  # noqa: ARG001
    retry_attempts: int,  # noqa: ARG001
    timeout_s: float,  # noqa: ARG001
) -> str:
    """Offline deterministic provider for `--mock`.

    Fabricates plausible rows so the full pipeline runs end-to-end with no
    network or API key — useful for demos, Scenario-1 dry runs, and CI. The
    prompt carries the source's output keys ("Each object must have these
    keys: a, b, c"), the search query, and the seed names; we echo those back
    as deterministic synthetic rows. Output is fully determined by the prompt,
    so runs are reproducible.
    """
    keys = _extract_keys_from_prompt(prompt)
    n = _extract_n_from_prompt(prompt)
    query = _extract_query_from_prompt(prompt)
    seeds = _extract_seeds_from_prompt(prompt)
    name_key = _name_key_for(keys)

    rows: list[dict[str, Any]] = []
    for i in range(n):
        base = seeds[i % len(seeds)] if seeds else f"{query[:24].title()}"
        name = f"{base}" if i < len(seeds) else f"{base} #{i + 1}"
        row: dict[str, Any] = {}
        for k in keys:
            row[k] = _mock_value(k, name=name, query=query, idx=i)
        row[name_key] = name
        rows.append(row)
    return json.dumps(rows)


def _mock_value(key: str, *, name: str, query: str, idx: int) -> str:
    """Deterministic synthetic value for a given output key."""
    k = key.lower()
    if k in ("firm_name", "organization_name", "company_name"):
        return name
    if "url" in k or "website" in k or "linkedin" in k or "portfolio" in k:
        return f"https://example.com/{_slug(name)}"
    if k == "city":
        return ["Dallas", "Denver", "Indianapolis", "Austin"][idx % 4]
    if k == "state":
        return ["TX", "CO", "IN", "TX"][idx % 4]
    if "project_description" in k:
        return f"New sanctuary construction (mock result for query: {query[:40]})"
    if "project_type" in k:
        return "new build"
    if "project_phase" in k:
        return ["capital campaign", "planning", "approved", "design"][idx % 4]
    if "estimated_timeline" in k or k == "meeting_date":
        return f"completion 202{6 + (idx % 3)}"
    if "campaign_goal" in k:
        return f"${(idx + 2)}00,000"
    if "denomination" in k or "denominations" in k:
        return ["Baptist", "Methodist", "Nondenominational", "Catholic"][idx % 4]
    if "key_contact" in k or "key_consultants" in k or "key_architects" in k or "key_contacts" in k:
        return f"Pastor {name.split()[0]}"
    if "notes" in k:
        return "Mock lead — offline synthetic data. Not a real prospect."
    if "permit_type" in k:
        return "building permit"
    if "geographic" in k:
        return "Regional (TX/CO/IN)"
    return f"mock-{k}-{idx}"


def _extract_keys_from_prompt(prompt: str) -> list[str]:
    m = re.search(r"have these keys:\s*(.+?)(?:\n\s*\n|Return ONLY)", prompt, re.DOTALL | re.IGNORECASE)
    if not m:
        return ["organization_name", "project_description", "city", "state"]
    blob = m.group(1)
    keys = [k.strip() for k in re.split(r"[,\n]", blob) if k.strip()]
    return keys or ["organization_name", "project_description", "city", "state"]


def _extract_n_from_prompt(prompt: str) -> int:
    m = re.search(r"EXACTLY\s+(\d+)", prompt, re.IGNORECASE)
    return int(m.group(1)) if m else 3


def _extract_query_from_prompt(prompt: str) -> str:
    m = re.search(r"Search for:\s*(.+)", prompt)
    return m.group(1).strip() if m else "prospects"


def _extract_seeds_from_prompt(prompt: str) -> list[str]:
    m = re.search(r"find ADDITIONAL firms\):\s*(.+)", prompt)
    if not m:
        return []
    return [s.strip() for s in m.group(1).split(",") if s.strip()][:6]


def _name_key_for(keys: list[str]) -> str:
    for candidate in ("firm_name", "organization_name", "company_name"):
        if candidate in keys:
            return candidate
    return keys[0] if keys else "organization_name"


# =============================================================================
# Prompt construction
# =============================================================================

_DEFAULT_PROMPT = """You are a research analyst finding prospects for the following business:

{product_description}

Search for: {query}

Find real, specific organizations matching the search. For each result, gather
the fields listed below. Be accurate; leave a field blank rather than guessing.

{seed_context}

Return EXACTLY {n} results as a JSON array. Each object must have these keys:
{keys}

Return ONLY the JSON array, no other text."""


def build_prompt(*, source_cfg: dict[str, Any], query: str, n: int, product_description: str) -> str:
    """Construct the grounded-search prompt for one query of one source.

    A source may supply its own `prompt` template (with {query}/{n}/{seed_context}
    placeholders) for hand-tuned wording; otherwise we synthesize one from the
    source's `fields` list and the org's product description. Keeping prompt
    text in the *context* (not code) is what makes the tool domain-generic.
    """
    seed_context = _build_seed_context(source_cfg.get("seed_firms") or [])
    template = source_cfg.get("prompt")
    if template:
        return template.format(query=query, n=n, seed_context=seed_context)

    fields = source_cfg.get("fields") or [
        "organization_name", "project_description", "city", "state",
        "project_type", "project_phase", "estimated_timeline",
        "key_contact", "denomination", "notes",
    ]
    return _DEFAULT_PROMPT.format(
        product_description=product_description or "(no product description provided)",
        query=query,
        seed_context=seed_context,
        n=n,
        keys=", ".join(fields),
    )


def _build_seed_context(seed_firms: list[str]) -> str:
    if not seed_firms:
        return ""
    return (
        "FOR REFERENCE — organizations already in our database (do NOT return "
        "these; find ADDITIONAL firms): " + ", ".join(seed_firms)
    )


def _name_field(source_cfg: dict[str, Any]) -> str:
    """Which field in a source's rows holds the company/org name."""
    explicit = source_cfg.get("name_field")
    if explicit:
        return explicit
    fields = source_cfg.get("fields") or []
    return _name_key_for(list(fields)) if fields else "organization_name"


# =============================================================================
# JSON array parsing (Gemini responses)
# =============================================================================


def parse_json_array(text: str) -> list[dict[str, Any]]:
    """Extract a JSON array of objects from a model's text response."""
    if not text:
        return []
    text = text.strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.split("\n") if not l.strip().startswith("```")).strip()
    try:
        parsed = json.loads(text)
        return [p for p in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(p, dict)]
    except json.JSONDecodeError:
        pass
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return [p for p in parsed if isinstance(p, dict)]
        except json.JSONDecodeError:
            log.warning("json parse failed; snippet=%s", text[:200])
    return []


# =============================================================================
# Phase 1 — Discovery
# =============================================================================


def discover(
    ctx: dict[str, Any],
    *,
    scan_run_id: str,
    provider: SearchProvider,
    emit: Callable[[dict[str, Any]], None],
    provider_config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run all discovery sources, dedupe the union, return the prospect list.

    Emits a `prospects` event per source (with that source's contribution) so
    an orchestrator can persist incrementally, then returns the fully merged +
    deduped prospect list for scoring.

    `provider_config` carries the resolved {model, temperature, entries_per_query,
    retry_attempts} for the chosen provider. If omitted, it falls back to the
    legacy `gemini` block (keeps older callers and tests working unchanged).
    """
    pc = provider_config or _provider_config(ctx, "gemini")
    model = pc["model"]
    temperature = pc["temperature"]
    entries_per_query = pc["entries_per_query"]
    retry_attempts = pc["retry_attempts"]
    max_concurrency = int(pc.get("max_concurrency", 6))
    product_description = str(ctx.get("product_description", ""))

    sources: dict[str, Any] = ctx.get("sources", {})

    # Build one task per (source, query). Grounded search is network-bound, so
    # we fan the queries out across a bounded thread pool instead of running
    # them sequentially — collapsing a ~15-20 min sweep to a few minutes and
    # overlapping rate-limit backoffs. phase_start is emitted per source up
    # front; phase_complete when that source's last query returns.
    tasks: list[tuple[str, str, str]] = []          # (source, name_field, prompt)
    per_source_total: dict[str, int] = {}
    for source_name, source_cfg in sources.items():
        queries = source_cfg.get("queries") or []
        if not queries:
            continue
        emit({"type": "phase_start", "phase": source_name})
        name_field = _name_field(source_cfg)
        per_source_total[source_name] = len(queries)
        for query in queries:
            prompt = build_prompt(
                source_cfg=source_cfg, query=query, n=entries_per_query,
                product_description=product_description,
            )
            tasks.append((source_name, name_field, prompt))

    def _run_query(task: tuple[str, str, str]) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
        source_name, name_field, prompt = task
        try:
            text = provider(
                prompt, model=model, temperature=temperature,
                retry_attempts=retry_attempts, timeout_s=_QUERY_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — one bad query mustn't kill the source
            log.warning("query failed source=%s: %s", source_name, str(exc)[:200])
            return source_name, []
        rows: list[tuple[str, dict[str, Any]]] = []
        for row in parse_json_array(text):
            name = str(row.get(name_field, "") or "").strip()
            norm = normalize_name(name)
            if norm:
                rows.append((norm, row))
        return source_name, rows

    # Workers only fetch+parse and RETURN rows; the main thread aggregates into
    # `groups` and emits — so no shared-state locking is needed.
    groups: dict[str, list[dict[str, Any]]] = {}
    name_fields = {s: nf for s, nf, _ in tasks}
    per_source_rows = {s: 0 for s in per_source_total}
    per_source_done = {s: 0 for s in per_source_total}
    if tasks:
        with ThreadPoolExecutor(max_workers=min(max_concurrency, len(tasks))) as pool:
            futures = [pool.submit(_run_query, t) for t in tasks]
            for fut in as_completed(futures):
                source_name, rows = fut.result()
                for norm, row in rows:
                    groups.setdefault(norm, []).append(
                        {"source": source_name, "name_field": name_fields[source_name], "raw": row}
                    )
                    per_source_rows[source_name] += 1
                per_source_done[source_name] += 1
                if per_source_done[source_name] == per_source_total[source_name]:
                    emit({"type": "phase_complete", "phase": source_name,
                          "count": per_source_rows[source_name]})

    prospects = _assemble_prospects(groups=groups, scan_run_id=scan_run_id,
                                    canonical=canonical_fields_from_sources(sources),
                                    excluded_names=excluded_prospect_names(ctx),
                                    log=_log_from(ctx))
    emit({
        "type": "prospects",
        "phase": "discover",
        "items": [_strip_internal(p) for p in prospects],
    })
    return prospects


def _strip_internal(prospect: dict[str, Any]) -> dict[str, Any]:
    """Drop the internal scoring bag — not part of the on-the-wire contract."""
    return {k: v for k, v in prospect.items() if k != "_internal"}


def _log_from(ctx: dict[str, Any]) -> Callable[[str], None]:
    """The caller's log sink if it supplied one, else the engine's own stderr line.

    Exists so `_assemble_prospects` can report an exclusion visibly. The engine prints
    `[aeo]`-style progress through an injected callable; anything written to the module
    logger is invisible in the container, which is how a silent filter came to look
    identical to a filter that never ran.
    """
    sink = ctx.get("_log") if callable(ctx.get("_log")) else None
    if sink:
        return sink

    def _default(message: str) -> None:
        print(f"[aeo] {message}", file=sys.stderr, flush=True)

    return _default


def excluded_prospect_names(ctx: dict[str, Any]) -> set[str]:
    """Normalized names discovery must never return as prospects.

    The commissioning organization itself, its aliases, and anything it has asked to
    suppress. **Measured need:** on the first real production run the org appeared in its
    own prospect list and passed every filter — it was rejected only because it happens to
    have in-house mechanical staff, i.e. by luck of an unrelated disqualifier. An org
    without that quirk would have been sold to itself.

    Matched on `normalize_name`, the same key cross-source dedup uses, so a legal-entity
    suffix or punctuation difference cannot slip past.
    """
    org = ctx.get("organization") if isinstance(ctx.get("organization"), dict) else {}
    candidates: list[Any] = [org.get("name"), ctx.get("organization_name")]
    for key in ("aliases", "exclusions"):
        value = org.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    names = {normalize_name(str(c)) for c in candidates if str(c or "").strip()}
    names.discard("")
    return names


def _assemble_prospects(*, groups: dict[str, list[dict[str, Any]]], scan_run_id: str,
                        canonical: Sequence[str] | None = None,
                        excluded_names: set[str] | None = None,
                        log: Callable[[str], None] | None = None) -> list[dict[str, Any]]:
    """Collapse source-bucketed rows into unified, deterministic-ID prospects.

    `excluded_names` drops the commissioning org and anything it suppresses — see
    `excluded_prospect_names`.
    """
    prospects: list[dict[str, Any]] = []
    for norm, members in groups.items():
        if not members:
            continue
        company_name = _pick_canonical_name(members)
        sources_found_in = sorted({m["source"] for m in members})
        source_count = len(sources_found_in)

        per_source: dict[str, dict[str, Any]] = {}
        for m in members:
            src = m["source"]
            if src not in per_source or _richness(m["raw"]) > _richness(per_source[src]):
                per_source[src] = m["raw"]

        if excluded_names and norm in excluded_names:
            # Through the INJECTED logger, not the module one. The first version used
            # `log.info`, which never reaches container stdout — so an exclusion that
            # fired looked exactly like one that never happened, and the absence of this
            # line was mistaken for evidence the org had not been discovered.
            message = f"excluding {company_name!r} — the organization's own listing"
            if log:
                log(message)
            continue

        internal = _merge_for_scoring(members, canonical)
        internal.update(sources_found_in=", ".join(sources_found_in),
                        multi_source=source_count > 1, source_count=source_count)

        # Locality: prefer what the source returned; fall back to parsing the address
        # it *did* return. On the first real production run every prospect had a street
        # address and NULL city/state/zip, because sources answer with one address
        # string — which also killed region scoring, since that reads city/state.
        address = internal.get("location_address") or None
        city, state, zip_code = _parse_locality(
            address,
            city=internal.get("city"),
            state=internal.get("state"),
            zip_code=internal.get("zip_code"),
        )

        # ⚠️ Write the parsed locality back into `internal` too, not just the record.
        # `_scoring_input` reads `_internal` whenever it is present — which is always,
        # in a single-process run — so parsing only into the record below left the
        # SCORER seeing empty city/state while the database showed them correctly.
        # Measured: every prospect scored `region_bonus: 0` on a run whose rows all
        # had a city, and the geo-fence simultaneously reported them as in-area. Two
        # views of one fact is the defect; one assignment fixes it.
        if city:
            internal["city"] = city
        if state:
            internal["state"] = state
        if zip_code:
            internal["zip_code"] = zip_code

        prospects.append({
            "id": str(uuid.uuid5(_NAMESPACE, f"{scan_run_id}:{norm}")),
            "company_name": company_name,
            "industry": internal.get("industry") or None,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "address": address,
            "website": internal.get("website_url") or None,
            # Contact details a discovery source already returned. AEO declares both on
            # its prospect callback; before 2026-08-12 neither was sent, so 12 of 36
            # prospects on a real run had a title sitting in `discovery_data` while the
            # column stayed empty. `or None` matters: this emitted `''` for most rows,
            # which reads as "present" to a count or a truthiness check.
            "contact_name": internal.get("key_contact") or None,
            "contact_title": internal.get("contact_title") or None,
            # Provenance. The column is plain text and was never populated, so nothing
            # could say which source a prospect came from without opening the JSONB.
            "sources": ", ".join(sources_found_in) or None,
            "discovery_data": {
                "sources_found_in": sources_found_in,
                "source_count": source_count,
                "by_source": {s: {k: v for k, v in r.items() if v} for s, r in per_source.items()},
            },
            "_internal": internal,
        })
    return prospects


#: Trailing "…, <city>, <ST> <zip>" — the shape sources actually return. Deliberately
#: narrow: it fills a gap, it does not guess. Anything that does not match this exact
#: tail is left as None, because a wrong city silently mis-scores region and mis-routes
#: a lead to the wrong sales territory, which is worse than an empty field.
_LOCALITY_TAIL = re.compile(
    r",\s*(?P<city>[^,]{2,40}),\s*(?P<state>[A-Za-z]{2})\.?\s*(?P<zip>\d{5}(?:-\d{4})?)?\s*$"
)


def _parse_locality(
    address: str | None, *, city: Any = None, state: Any = None, zip_code: Any = None
) -> tuple[str | None, str | None, str | None]:
    """Best-effort city/state/zip, **never overwriting what a source supplied.**

    Returns `(city, state, zip)`. A supplied value always wins; parsing only fills what
    is missing, and only when the address ends in an unambiguous locality tail.
    """
    have_city = str(city or "").strip() or None
    have_state = str(state or "").strip() or None
    have_zip = str(zip_code or "").strip() or None
    if have_city and have_state and have_zip:
        return have_city, have_state, have_zip

    m = _LOCALITY_TAIL.search(str(address or "").strip()) if address else None
    if not m:
        return have_city, have_state, have_zip
    return (
        have_city or (m.group("city") or "").strip() or None,
        have_state or (m.group("state") or "").strip().upper() or None,
        have_zip or (m.group("zip") or "").strip() or None,
    )


def _pick_canonical_name(members: list[dict[str, Any]]) -> str:
    best = ""
    for m in members:
        cand = str(m["raw"].get(m["name_field"], "") or "").strip()
        if len(cand) > len(best):
            best = cand
    return best


def _richness(row: dict[str, Any]) -> int:
    return sum(1 for v in row.values() if v)


# Alias groups: heterogeneous source schemas → one uniform scoring dict.
#: **Vertical-neutral identity aliases — the ONLY hardcoded field names left, and
#: deliberately so.** Every prospect in every vertical has a name, a location and a web
#: presence, and this engine's own internals read them by these canonical names
#: (`score_region` reads `city`/`state`; the prospect record reads `organization_name` /
#: `location_address` / `website_url`). Nothing here names an industry, an organization
#: or a place.
#:
#: ⚠️ **Everything domain-specific is DERIVED from the skill config** — see
#: `canonical_fields_from_sources`. This replaced a hardcoded 20-field map
#: (`denomination`, `campaign_goal`, `amount_raised`, `av_opportunity_notes`, …) that
#: `_merge_raw_rows` iterated, which meant **only those fields survived the merge**: on a
#: real HVAC run it silently discarded 7 of the 11 fields the skill authored, including
#: `square_footage` (17 prospects had it) and `portfolio_size` (10 did) — the exact data
#: the operator's ICP and scoring factors depended on. See UPSTREAM.md.
_IDENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "organization_name": ("organization_name", "company_name", "firm_name", "name"),
    "city": ("city",),
    "state": ("state",),
    "zip_code": ("zip_code", "zip", "postal_code", "postcode"),
    "location_address": ("location_address", "address", "location", "street_address", "property_address"),
    "website_url": ("website_url", "website", "url", "domain"),
    "key_contact": ("key_contact", "contact_name", "contact", "key_contacts"),
    "contact_title": ("contact_title", "title", "job_title", "role"),
}


def _norm_key(key: Any) -> str:
    """Normalize a field name for matching: case- and punctuation-insensitive.

    Replaces per-field synonym lists for everything except identity. A source that
    returns `squareFootage`, `square_footage` or `Square Footage` for an authored
    `square_footage` all land on the same field, without anyone maintaining a table.
    """
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def canonical_fields_from_sources(sources_cfg: Any) -> tuple[str, ...]:
    """The union of `fields` authored across every configured discovery source.

    **The authored fields ARE the vocabulary.** A skill built for any vertical declares
    what it collects; this engine must carry all of it through the merge rather than
    intersect it with a list written for one industry.
    """
    names: list[str] = []
    for src in (sources_cfg or {}).values() if isinstance(sources_cfg, dict) else []:
        for f in (src or {}).get("fields") or []:
            f = str(f).strip()
            if f and f not in names:
                names.append(f)
    return tuple(names)


def _merge_raw_rows(
    rows: list[dict[str, Any]], canonical: Sequence[str] | None = None
) -> dict[str, Any]:
    """Collapse heterogeneous source rows into the uniform scoring dict:
    for each field, keep the longest non-empty value across rows.

    `canonical` is the config-derived field set. **When it is absent the fallback is
    every key present in the data**, never a fixed list — an unknown key is data we
    were asked to collect, so dropping it is always the wrong default.
    """
    indexed: list[dict[str, Any]] = []
    for raw in rows:
        indexed.append({_norm_key(k): v for k, v in (raw or {}).items()})

    names: list[str] = [str(n) for n in (canonical or [])]
    if not names:
        for raw in rows:
            for k in (raw or {}):
                if str(k) not in names:
                    names.append(str(k))
    for ident in _IDENTITY_ALIASES:
        if ident not in names:
            names.append(ident)

    def _longest(candidates: tuple[str, ...]) -> str:
        best = ""
        for idx in indexed:
            for cand in candidates:
                val = idx.get(_norm_key(cand))
                if isinstance(val, list):
                    val = ", ".join(str(v) for v in val)
                val = str(val if val is not None else "").strip()
                if len(val) > len(best):
                    best = val
        return best

    combined: dict[str, Any] = {}
    for name in names:
        combined[name] = _longest(_IDENTITY_ALIASES.get(name, (name,)))

    # Descriptive free text, generically: any field whose name mentions "description".
    # The old version read the single church-AV field `project_description`.
    descriptions: list[str] = []
    for idx in indexed:
        for norm, val in idx.items():
            if "description" not in norm:
                continue
            d = str(val or "").strip()
            if d and d not in descriptions:
                descriptions.append(d)
    combined["all_descriptions"] = descriptions
    return combined


def _merge_for_scoring(
    members: list[dict[str, Any]], canonical: Sequence[str] | None = None
) -> dict[str, Any]:
    return _merge_raw_rows([m["raw"] for m in members], canonical)


# =============================================================================
# Phase 2 — Scoring (deterministic, config-driven, no network)
# =============================================================================
#
# Default weights mirror the legacy score_leads.py 5-factor model:
#   completeness 0-15, fit 0-25, region 0-10, multi_source 0-10, pipeline 0-30
#   + AI adjustment -15..+15 (supplied by the caller per prospect; 0 if absent)
# capped at 100. Every knob is overridable via context["scoring"].

_DEFAULT_SCORING: dict[str, Any] = {
    "completeness": {
        "max": 15,
        "fields": [
            "organization_name", "city", "state", "project_description",
            "project_type", "project_phase", "denomination", "key_contact",
            "estimated_timeline", "campaign_goal", "av_opportunity_notes",
            "permit_type", "consultant_firm",
        ],
    },
    "fit": {
        "max": 25,
        "text_fields": ["project_description", "project_type"],
        "keyword_scores": {
            "new build": 25, "new construction": 25, "new sanctuary": 25,
            "new worship center": 25, "new church": 25,
            "campus": 20, "relocation": 20,
            "major renovation": 18, "expansion": 18, "addition": 15,
            "renovation": 12, "remodel": 10, "conversion": 10, "repurposing": 10,
        },
    },
    "region_bonus": {
        "max": 10,
        # Empty by design: the region map and its aliases are derived from the org's
        # own markets (see `regions_from_markets`). This used to hardcode Texas,
        # Colorado and Indiana — the previous customer's states.
        "state_aliases": {},
        "regions": {},  # {state_abbr: [city, ...]} — empty means no bonus
    },
    "multi_source": {"max": 10, "tiers": [[3, 10], [2, 6]]},
    "pipeline": {
        "max": 30,
        "decision_lead_months": 13,
        #: Fields that may carry the timing signal, in priority order. Derived from the
        #: skill's authored fields when it does not declare them (see `score_prospects`).
        "timing_fields": ("estimated_timeline",),
        "statuses": [
            [18, 999, "1 - Early Discovery", "18+ months to decision.", 20],
            [12, 18, "2 - Relationship Building", "12-18 months to decision.", 26],
            [8, 12, "3 - Design Influence", "8-12 months to decision.", 30],
            [4, 8, "4 - Active Pursuit", "4-8 months to decision.", 30],
            [0, 4, "5 - Decision Imminent", "0-4 months to decision.", 28],
            [-4, 0, "6 - Likely Awarded", "Decision likely already made.", 8],
            [-999, -4, "7 - Too Late", "Decided 4+ months ago.", 2],
        ],
        "phase_fallback": {
            "feasibility": ["1 - Early Discovery", "Feasibility stage.", 20],
            "planning": ["1 - Early Discovery", "Planning stage.", 22],
            "capital campaign": ["2 - Relationship Building", "Capital campaign active.", 26],
            "fundraising": ["2 - Relationship Building", "Fundraising phase.", 26],
            "campaign": ["2 - Relationship Building", "Campaign phase.", 24],
            "applied": ["3 - Design Influence", "Permits applied.", 30],
            "under review": ["3 - Design Influence", "Under review.", 28],
            "zoning": ["3 - Design Influence", "Zoning phase.", 28],
            "approved": ["4 - Active Pursuit", "Approved.", 30],
            "site plan": ["4 - Active Pursuit", "Site plan phase.", 30],
            "design": ["4 - Active Pursuit", "Design phase.", 30],
            "permitted": ["5 - Decision Imminent", "Permitted.", 28],
            "under construction": ["6 - Likely Awarded", "Under construction.", 8],
            "construction": ["6 - Likely Awarded", "Construction underway.", 8],
        },
        "campaign_goal_floor": {"status": "2 - Relationship Building", "detail": "Has campaign goal; assumed fundraising.", "score": 20},
        # ⚠️ **Abstain, do not invent.** `pipeline_status` is consumed by AEO as a SALES
        # stage and drives operator kanbans, so a fabricated "Unknown" put every
        # prospect into a column no human moved it to. `None` leaves the column NULL.
        # Score is 0, not 10: with no timing evidence this axis must not contribute —
        # awarding 10/30 to every prospect is what made a real run's scores cluster at
        # 11-12 regardless of the prospect.
        "default": {"status": None, "detail": "No timing evidence available.", "score": 0},
    },
    "ai_adjustment": {"min": -15, "max": 15},
    #: Budget for the skill's own `scoring.factors`, as its OWN axis.
    #:
    #: `None` keeps the legacy behaviour: authored factors BORROW the `fit` axis (at 40
    #: unless `fit.max` is authored), so a config that never heard of this key scores
    #: exactly as before. Set it and factors become independent of `fit`, which is what
    #: lets a skill put its whole 0-100 budget on its own criteria and zero the axes it
    #: never authored — a vertical whose fit is not geographic sets `region_bonus.max: 0`,
    #: one with no construction timeline sets `pipeline.max: 0`.
    #:
    #: A scalar rather than `factors.max` because `scoring.factors` is a LIST.
    "factors_max": None,
    "score_cap": 100,
}


#: Axes that own a slice of `score_cap`. Used only for the load-time budget warning.
_BUDGET_AXES = ("completeness", "fit", "region_bonus", "multi_source", "pipeline")

#: Keys whose structured value must NOT be overlaid onto the scoring input: they are
#: envelopes the scorer already reads through their own paths, and shadowing a merged
#: field with the raw envelope would change what every other factor sees.
_STRUCTURED_RESERVED = frozenset(
    {"_internal", "discovery_data", "validation_data", "contacts_data", "_ai_judgment"}
)


def _deep_get(scoring: dict[str, Any], key: str) -> dict[str, Any]:
    """Merge caller's scoring override for `key` over the default (shallow)."""
    merged = dict(_DEFAULT_SCORING[key])
    override = scoring.get(key)
    if isinstance(override, dict):
        merged.update(override)
    return merged


def _scoring_input(prospect: dict[str, Any], canonical: Sequence[str] | None = None) -> dict[str, Any]:
    """Extract the uniform scoring dict from a prospect.

    Accepts prospects produced by `discover` (carry `_internal`) OR raw lead
    dicts supplied by an LLM that did its own discovery (fields at top level).
    """
    if isinstance(prospect.get("_internal"), dict):
        base = dict(prospect["_internal"])
    else:
        # Reconstruct from a wire prospect: a `discover` result persisted to a
        # file and scored in a separate `score` call, OR a raw lead list an LLM
        # built itself. Merge across discovery_data.by_source (if present) plus
        # the top-level fields, using the same alias logic as discovery.
        dd = prospect.get("discovery_data") or {}
        by_source = dd.get("by_source") or {}
        rows = list(by_source.values()) if by_source else []
        rows.append(prospect)  # top-level fields (raw LLM leads live here)
        base = _merge_raw_rows(rows, canonical)
        if not base.get("organization_name") and prospect.get("company_name"):
            base["organization_name"] = str(prospect["company_name"]).strip()
        base["sources_found_in"] = ", ".join(dd.get("sources_found_in", [])) or base.get("sources_found_in", "")
        base["source_count"] = dd.get("source_count", prospect.get("source_count", 1))
        base["multi_source"] = base["source_count"] > 1
    base.setdefault("source_count", prospect.get("source_count", 1))
    base.setdefault("multi_source", base["source_count"] > 1)
    return base


_NUMBER_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _first_number(text: str) -> float | None:
    """First number in a free-text value: "15,273 sq ft" -> 15273.0."""
    m = _NUMBER_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def factor_label(spec: dict[str, Any]) -> str:
    """The name this factor is REPORTED under, which is not necessarily what it reads.

    `key` when authored, else `name`. Configs with no `key` are unaffected, so the
    per-factor breakdown an operator already sees keeps its existing labels.
    """
    key = str(spec.get("key") or "").strip()
    return key or str(spec.get("name") or "").strip()


def factor_source_fields(spec: dict[str, Any]) -> tuple[str, ...]:
    """The collected field(s) this factor READS, in priority order.

    Three-way split — `key` identifies, `source_field` binds, `name` labels — because
    conflating identity with binding is a measured production defect: a skill authored
    `multi_site_portfolio` / `recent_mechanical_permit` / `building_age` while collecting
    `portfolio_size`, `permit_date` and nothing at all, so three of four factors scored
    zero for every prospect, forever, with no error anywhere.

    `source_field` may be a LIST, which is how one factor reads two discovery lanes that
    named the same quantity differently ("Estimated Headcount" in one sweep, "Employee
    Estimate" in another). First candidate that carries a value wins.

    ⚠️ Still an EXPLICIT binding, never an inferred one. Guessing that
    `multi_site_portfolio` means `portfolio_size` is the same silent inference this whole
    class of defect is made of — the fix is that the author can now SAY it.
    """
    raw = spec.get("source_field")
    if isinstance(raw, (list, tuple)):
        fields = [str(f).strip() for f in raw if str(f).strip()]
    elif isinstance(raw, str) and raw.strip():
        fields = [raw.strip()]
    else:
        fields = []
    if not fields:
        name = str(spec.get("name") or "").strip()
        fields = [name] if name else []
    return tuple(fields)


def _tier_credit(text: str, tiers: Any) -> float:
    """Graded credit from a threshold table: `[{threshold, points}, ...]`.

    The highest threshold the value MEETS wins, so the table may be non-monotonic — a
    sweet-spot curve (peak in the middle, lower at both ends) is the normal case for a
    size band, not an exception. Credit is `points / max(points in table)`, which keeps
    this on the same [0, 1] scale the weight normalization already uses: a factor's
    `weight` stays its share of the axis and the table only says how much of that share
    this value earned.

    Returns 0.0 when the value carries no number, when it is below every threshold, or
    when the table awards no positive points anywhere.
    """
    parsed: list[tuple[float, float]] = []
    for entry in tiers if isinstance(tiers, (list, tuple)) else ():
        if not isinstance(entry, dict):
            continue
        try:
            parsed.append((float(entry["threshold"]), float(entry.get("points", 0) or 0)))
        except (KeyError, TypeError, ValueError):
            continue
    if not parsed:
        return 0.0
    ceiling = max(p for _, p in parsed)
    if ceiling <= 0:
        return 0.0
    num = _first_number(text)
    if num is None:
        return 0.0
    for threshold, points in sorted(parsed, key=lambda tp: -tp[0]):
        if num >= threshold:
            return max(0.0, min(1.0, points / ceiling))
    return 0.0


def normalize_keyword_table(keywords: Any) -> list[tuple[str, float]]:
    """Accept the three shapes a keyword→points table is naturally authored in.

    * `{"bundled": 20, "none": 14}` — the concise form.
    * `[{"keyword": "bundled", "points": 20}, ...]` — one keyword per entry.
    * `[{"keywords": ["county", "municipality"], "points": 20}, ...]` — a named bucket
      with several spellings sharing one score, which is how a ranked industry list is
      written.

    All three are unambiguous, so accepting all three makes porting an existing config a
    translation of structure rather than of meaning.
    """
    table: list[tuple[str, float]] = []

    def add(word: Any, points: Any) -> None:
        text = str(word or "").strip().lower()
        if not text:
            return
        try:
            table.append((text, float(points)))
        except (TypeError, ValueError):
            return

    if isinstance(keywords, dict):
        for word, points in keywords.items():
            add(word, points)
    elif isinstance(keywords, (list, tuple)):
        for entry in keywords:
            if not isinstance(entry, dict):
                continue
            points = entry.get("points", entry.get("fit_score"))
            words = entry.get("keywords")
            if isinstance(words, (list, tuple)) and words:
                for word in words:
                    add(word, points)
            else:
                add(entry.get("keyword", entry.get("name")), points)
    return table


def _keyword_credit(text: str, keywords: Any) -> float:
    """Graded credit from a keyword→points table. **Longest match wins.**

    ⚠️ Longest-match, deliberately NOT table order and NOT first match. Keyword tables
    routinely contain one term that is a substring of another — `standalone` inside
    `standalone-recent` — and with order-dependent matching the shorter, wrong, and
    usually *higher*-scoring term wins by accident. Making length decide means an author
    cannot break their own table by reordering it, and the same rule already settled this
    class of bug elsewhere in the platform.

    Ties on length are broken by the higher score, only so the result is deterministic
    rather than dependent on dict insertion order.
    """
    table = normalize_keyword_table(keywords)
    if not table:
        return 0.0
    ceiling = max(points for _, points in table)
    if ceiling <= 0:
        return 0.0
    haystack = text.lower()
    matched = [(word, points) for word, points in table if word in haystack]
    if not matched:
        return 0.0
    _, points = sorted(matched, key=lambda wp: (-len(wp[0]), -wp[1]))[0]
    return max(0.0, min(1.0, points / ceiling))


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_signal_date(text: Any) -> date | None:
    """A STRICT date parse, for recency. Returns None rather than guessing.

    ⚠️ **Deliberately not `parse_estimated_date`.** That function exists to estimate a
    project *completion* date and ends with a bare-year fallback that invents a month
    (`_estimate_month` returns 6 when it recognizes nothing), which is exactly how
    fabricated dates got manufactured. For recency that is not a smaller error, it is a
    different answer: "2019" becoming 2019-06-01 silently places a signal inside or
    outside a trailing-12-month window depending on nothing.

    So: **a bare year is refused.** A month must be known. Handled forms, all observed in
    real collected data — `2026-07-30`, `2026-07`, `02/14/2024`, `August 2026`,
    `April 27, 2023`, `27 April 2023`. Day defaults to 1 only when the MONTH is known,
    which is a precision limit, not an invention.
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    low = raw.lower()

    def _mk(y: int, m: int, d: int = 1) -> date | None:
        # ⚠️ No day clamping. An earlier cut clamped anything past 28 "to be safe" and
        # silently turned a valid 2026-07-30 into 2026-07-28 — inventing a different date,
        # which is the exact failure this parser exists to avoid. An impossible day (Feb
        # 30) is producer garbage and becomes None, i.e. "undated", which downstream
        # treats as base-credit-no-bonus rather than as a date it made up.
        if not (1900 <= y <= 2100):
            return None
        try:
            return date(y, m, d)
        except ValueError:
            return None

    m = re.search(r"\b(\d{4})-(\d{1,2})(?:-(\d{1,2}))?\b", low)      # 2026-07-30 / 2026-07
    if m:
        return _mk(int(m.group(1)), int(m.group(2)), int(m.group(3) or 1))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", low)            # 02/14/2024 (US)
    if m:
        return _mk(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b(\d{1,2})/(\d{4})\b", low)                      # 07/2026
    if m:
        return _mk(int(m.group(2)), int(m.group(1)))
    names = "|".join(_MONTHS)
    m = re.search(rf"\b({names})[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", low)
    if m:                                                            # April 27, 2023
        return _mk(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({names})[a-z]*\.?\s+(\d{{4}})\b", low)
    if m:                                                            # 27 April 2023
        return _mk(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    m = re.search(rf"\b({names})[a-z]*\.?\s+(\d{{4}})\b", low)
    if m:                                                            # August 2026
        return _mk(int(m.group(2)), _MONTHS[m.group(1)])
    return None  # a bare year lands here, and that is the point


def _event_rows(raw: Any) -> list[dict[str, Any]]:
    """Normalize whatever the field held into rows an event factor can walk.

    An enrichment lane returns a LIST of rows (one employer can carry several dated
    events). A single dict is one row; anything else is one row under a synthetic key so a
    flat field still works.
    """
    if isinstance(raw, list):
        return [r if isinstance(r, dict) else {"value": r} for r in raw]
    if isinstance(raw, dict):
        return [raw]
    return [{"value": raw}] if str(raw or "").strip() else []


def _event_signal_credit(
    raw: Any,
    spec: dict[str, Any],
    *,
    today: date | None,
    lead: dict[str, Any] | None = None,
) -> float:
    """Graded credit from dated EVENTS, with a recency window and a bonus keyword set.

    The shape a timing factor actually needs and the other modes cannot express: some
    events are worth more than others (`bonus_keywords`, e.g. a live procurement notice
    against a routine renewal), and an old event is worth nothing however strong it was.

    Credit is `best_row_points / (base_points + bonus_points)` — same relative scale as
    `tiers` and `keywords`, so `weight` still owns the factor's share of the axis. The
    BEST surviving row wins; several current events do not stack, because two signals are
    not twice as in-market as one.

    Two judgement calls, both erring toward not inventing a verdict:

    * **An undated row earns the base, never the bonus.** Excluding it would let missing
      evidence silently shrink a result set; awarding the recency-gated bonus would assert
      a freshness nothing established.
    * **A row dated in the FUTURE is current**, not out of window. A scheduled renewal is
      a timing signal.
    """
    try:
        base = float(spec.get("base_points", 0) or 0)
        bonus = float(spec.get("bonus_points", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    ceiling = base + bonus
    if ceiling <= 0:
        return 0.0

    keywords = [
        str(k).strip().lower()
        for k in (spec.get("bonus_keywords") or [])
        if str(k).strip()
    ]
    date_field = str(spec.get("date_field") or "").strip()
    try:
        window = int(spec.get("recency_months") or 0)
    except (TypeError, ValueError):
        window = 0

    best: float | None = None
    for row in _event_rows(raw):
        dated = parse_signal_date(row.get(date_field)) if date_field else None
        # SIBLING-FIELD fallback. An enrichment lane returns rows that carry their own
        # date, but a discovery source flattens the same information into two fields side
        # by side (`trigger_type` and `trigger_date`), which is how a real existing config
        # already collects it. Without this, event mode silently degrades to "undated" on
        # every such config — base credit, never the bonus, recency never applied — and
        # nothing says so. Only consulted when the row itself has no date.
        if dated is None and date_field and lead is not None:
            dated = parse_signal_date(_lookup_field(lead, date_field))
        if window > 0 and dated is not None and today is not None:
            # AGE, so the arguments are (today, dated): `_months_between(a, b)` is a - b,
            # which for a past signal is NEGATIVE and could never exceed the window — an
            # inverted sign here silently disables the whole gate and every stale signal
            # scores full. A future date gives a negative age and stays current.
            if _months_between(today, dated) > window:
                continue
        haystack = " ".join(str(v) for v in row.values()).lower()
        earned = base
        if keywords and dated is not None and any(k in haystack for k in keywords):
            earned += bonus
        elif keywords and not date_field and any(k in haystack for k in keywords):
            # No recency gate configured at all: the bonus is not conditioned on a date.
            earned += bonus
        best = earned if best is None else max(best, earned)

    if best is None:
        return 0.0
    return max(0.0, min(1.0, best / ceiling))


def hard_disqualifier(lead: dict[str, Any], rules: Any) -> str | None:
    """The reason the FIRST matching rule excludes this prospect, or None.

    **Ineligible is not the same as low-scoring**, and conflating them is why this exists.
    `disqualify_below` is a ranking statement: the operator picked a cutoff and wants to
    see what fell under it. A rule here says the prospect cannot be sold to at all — too
    small, wrong country, a multi-year contract signed with someone else last month — and
    no threshold on a score can express that, because a disqualified prospect may still
    score well on every other axis.

    Evaluated BEFORE any factor work, so a ruled-out prospect costs nothing to score and
    cannot end up recorded as both strongly-fitting and excluded.

    Rule primitives, deliberately few and all industry-neutral:

    * `below` / `above` — the first number in the field is outside the bound.
    * `keywords` — any of these appears in the field.
    * `required_keywords` — NONE of these appears in the field.

    ⚠️ **A rule never fires on a field the prospect does not carry.** Absence of evidence
    is not grounds for exclusion — the same rule validation follows for its own verdict —
    so a missing country does not mean "not in the US". A rule that should fire on
    absence is a rule about a field the discovery sources must be made to collect.
    """
    for rule in rules if isinstance(rules, (list, tuple)) else ():
        if not isinstance(rule, dict):
            continue
        raw = None
        for candidate in factor_source_fields(rule):
            raw = _lookup_field(lead, candidate)
            if raw is not None:
                break
        text = str(raw if raw is not None else "").strip()
        if not text:
            continue  # nothing to judge — see the warning above

        hit = False
        number = _first_number(text)
        for bound, worse in (("below", True), ("above", False)):
            if rule.get(bound) is None or number is None:
                continue
            try:
                limit = float(rule[bound])
            except (TypeError, ValueError):
                continue
            if (number < limit) if worse else (number > limit):
                hit = True

        haystack = text.lower()
        words = [str(k).strip().lower() for k in (rule.get("keywords") or ()) if str(k).strip()]
        if words and any(w in haystack for w in words):
            hit = True
        required = [
            str(k).strip().lower()
            for k in (rule.get("required_keywords") or ())
            if str(k).strip()
        ]
        if required and not any(w in haystack for w in required):
            hit = True

        if hit:
            return (
                str(rule.get("reason") or "").strip()
                or str(rule.get("key") or rule.get("name") or "").strip()
                or "Disqualified by rule"
            )
    return None


def priority_band(score: int, bands: Any) -> str | None:
    """The authored label for a score. None when the skill authored no bands.

    A number ranks; a band says what to do about it. Accepts `{range: [lo, hi]}` or
    `{min, max}` — both spellings appear in real configs and neither is ambiguous.
    """
    for band in bands if isinstance(bands, (list, tuple)) else ():
        if not isinstance(band, dict):
            continue
        span = band.get("range")
        try:
            if isinstance(span, (list, tuple)) and len(span) == 2:
                lo, hi = float(span[0]), float(span[1])
            else:
                lo = float(band.get("min", 0) or 0)
                hi = float(band.get("max", 0) or 0)
        except (TypeError, ValueError):
            continue
        if lo <= score <= hi:
            label = str(band.get("label") or band.get("name") or "").strip()
            return label or None
    return None


def band_coverage_gap(bands: Any, cap: int) -> str | None:
    """A human-readable description of the first gap or overlap, or None if clean.

    Advisory only. A gap means some achievable score has no verdict at all, which is
    invisible until the one prospect that lands there renders with an empty column.
    """
    spans: list[tuple[int, int, str]] = []
    for band in bands if isinstance(bands, (list, tuple)) else ():
        if not isinstance(band, dict):
            continue
        span = band.get("range")
        try:
            if isinstance(span, (list, tuple)) and len(span) == 2:
                lo, hi = int(span[0]), int(span[1])
            else:
                lo, hi = int(band.get("min", 0) or 0), int(band.get("max", 0) or 0)
        except (TypeError, ValueError):
            continue
        spans.append((lo, hi, str(band.get("label") or "?")))
    if not spans:
        return None
    spans.sort()
    if spans[0][0] > 0:
        return f"scores 0-{spans[0][0] - 1} have no band"
    cursor = spans[0][1]
    for lo, hi, label in spans[1:]:
        if lo > cursor + 1:
            return f"scores {cursor + 1}-{lo - 1} have no band"
        if lo <= cursor:
            return f"band '{label}' overlaps the one below it at {lo}"
        cursor = max(cursor, hi)
    if cursor < cap:
        return f"scores {cursor + 1}-{cap} have no band"
    return None


def _lookup_field(lead: dict[str, Any], name: str) -> Any:
    """One field, exact then punctuation/case-insensitive. Returns None if empty."""
    raw = lead.get(name)
    if raw is not None and str(raw).strip():
        return raw
    norm = _norm_key(name)
    return next(
        (v for k, v in lead.items() if _norm_key(k) == norm and str(v).strip()), None
    )


def _factor_credit(
    lead: dict[str, Any], name: str, spec: dict[str, Any], *, today: date | None = None
) -> float:
    """Credit in [0, 1] for one authored factor, from the data actually collected.

    **Presence is the evidence.** A factor names a field the skill asked for; if the
    field came back with a value, the factor is satisfied. `min`/`max` refine that into
    a threshold when the config supplies one — which is how an operator's ICP bound
    (`square_footage >= 10000`) becomes a scoring input rather than prose.

    Deliberately NOT a keyword table: that is what tied the previous scorer to one
    industry.

    **Presence is only the DEFAULT mode.** Authoring a graded table switches this factor
    to it, and the mode is derived from which table is present rather than from a separate
    `type` field — so a table can never be authored and then silently ignored, which is
    the failure this codebase keeps producing:

    * `tiers`       — `[{threshold, points}]`, graded by a numeric value. `_tier_credit`.
    * `keywords`    — keyword → points, graded by text. `_keyword_credit`.
    * `base_points` — dated events with a recency window. `_event_signal_credit`.

    Both bypass `min`/`max`, which exist only to turn *presence* into a threshold; a table
    already states the whole curve. That also removes the inversion `max` causes here — a
    value ABOVE `max` scores 0.0, so "managing 8 property types" earned less than 4 — and
    makes "at least 2 earns partial credit, 5 or more earns full" expressible at all,
    which is what several live factors already promise in their own descriptions.
    """
    raw = None
    for candidate in factor_source_fields(spec) or (name,):
        raw = _lookup_field(lead, candidate)
        if raw is not None:
            break

    # Event mode reads the RAW value, because a lane hands back a list of rows that
    # stringifying would destroy. Checked before the emptiness guard for the same reason.
    if spec.get("base_points") is not None:
        return _event_signal_credit(raw, spec, today=today, lead=lead)

    text = str(raw if raw is not None else "").strip()
    if not text:
        return 0.0

    tiers, keywords = spec.get("tiers"), spec.get("keywords")
    has_tiers = isinstance(tiers, (list, tuple)) and bool(tiers)
    has_keywords = bool(normalize_keyword_table(keywords))
    # Both tables authored is ambiguous. Resolved deterministically here (tiers win) and
    # WARNED about once per run in `score_prospects`, which is where the log lives —
    # silence is how an ignored table gets shipped.
    if has_tiers:
        return _tier_credit(text, tiers)
    if has_keywords:
        return _keyword_credit(text, keywords)

    num = _first_number(text)
    lo, hi = spec.get("min"), spec.get("max")
    if num is not None and (lo is not None or hi is not None):
        try:
            if lo is not None and num < float(lo):
                return 0.0
            if hi is not None and num > float(hi):
                return 0.0
        except (TypeError, ValueError):
            pass
    return 1.0


def score_config_factors(
    lead: dict[str, Any], factors: Any, max_points: int, *, today: date | None = None
) -> tuple[int, dict[str, float]]:
    """Score the skill's OWN authored `scoring.factors`, normalized to `max_points`.

    Replaces the hardcoded `fit` keyword table. The authored factors are the ICP-fit
    axis — which is what they always were semantically; nothing read them until
    2026-08-12, so a model could author four weighted factors, have them validate, and
    watch every prospect score identically off unrelated defaults.

    Returns `(points, per_factor_credit)` so the breakdown is inspectable rather than a
    single opaque number.
    """
    total_weight = 0.0
    earned = 0.0
    detail: dict[str, float] = {}
    for spec in factors or []:
        if not isinstance(spec, dict):
            continue
        name = str(spec.get("name") or "").strip()
        # A factor is usable if it has EITHER a label or a binding. Requiring `name`
        # here silently skipped any factor bound only by `source_field` — the same
        # invisible-skip failure this three-way split exists to remove. The schema still
        # requires `name` as the human label; the engine no longer depends on it to read.
        if not name and not factor_source_fields(spec):
            continue
        try:
            weight = float(spec.get("weight", 1) or 0)
        except (TypeError, ValueError):
            weight = 0.0
        if weight <= 0:
            continue
        credit = _factor_credit(lead, name, spec, today=today)
        total_weight += weight
        earned += weight * credit
        detail[factor_label(spec) or name] = round(credit, 3)
    if total_weight <= 0:
        return 0, detail
    return round((earned / total_weight) * max_points), detail


def score_completeness(lead: dict[str, Any], cfg: dict[str, Any]) -> int:
    fields = cfg["fields"]
    filled = sum(1 for f in fields if str(lead.get(f, "")).strip())
    return round((filled / len(fields)) * cfg["max"]) if fields else 0


def score_fit(lead: dict[str, Any], cfg: dict[str, Any]) -> int:
    text = " ".join(str(lead.get(f, "")) for f in cfg["text_fields"]).lower()
    best = 0
    for kw, pts in cfg["keyword_scores"].items():
        if kw in text:
            best = max(best, pts)
    return min(best, cfg["max"])


#: Name fragments that mark a field as carrying a date/timing signal. A vocabulary of
#: *shapes*, not of an industry — "permit_date" and "estimated_timeline" both match, and
#: nothing here names a vertical, an org or a place.
_TIMING_NAME_HINTS = ("timeline", "date", "completion", "due", "schedule", "when")


def timing_fields_from_authored(canonical: Sequence[str] | None) -> tuple[str, ...]:
    """Authored fields that plausibly carry a timing signal, in authored order.

    Used only when the skill does not declare `pipeline.timing_fields` itself. Ordering
    follows the config so an operator's first-declared date field wins, and every
    candidate is *parsed* before it is used — a non-date value simply does not match, so
    a false positive here costs nothing.
    """
    hits: list[str] = []
    for name in canonical or []:
        norm = _norm_key(name)
        if any(hint in norm for hint in _TIMING_NAME_HINTS) and name not in hits:
            hits.append(str(name))
    return tuple(hits)


def regions_from_markets(markets: Any) -> tuple[dict[str, list[str]], list[str]]:
    """Build the region map from the ORG's own target markets.

    Returns `({region_token: [cities]}, [all market cities])`. Replaces a hardcoded
    `regions = {}` (empty, so no region bonus was reachable at all) plus a three-entry
    `state_aliases` table covering Texas, Colorado and Indiana — the previous customer's
    states. A Tennessee org could not score region no matter what it collected.

    **No state-name table is introduced**, deliberately: markets written as
    "Nashville, Tennessee" cannot be bridged to a lead's "TN" without one, so the city
    list exists to match those cases instead. Cities are the more specific signal anyway.
    """
    regions: dict[str, list[str]] = {}
    cities: list[str] = []
    for market in markets or []:
        parts = [seg.strip().lower() for seg in str(market).split(",") if seg.strip()]
        if not parts:
            continue
        city = parts[0] if len(parts) >= 2 else ""
        token = parts[-1]
        regions.setdefault(token, [])
        if city:
            if city not in regions[token]:
                regions[token].append(city)
            if city not in cities:
                cities.append(city)
    return regions, cities


def score_region(lead: dict[str, Any], cfg: dict[str, Any]) -> int:
    regions = cfg.get("regions") or {}
    market_cities = [str(c).lower() for c in (cfg.get("market_cities") or [])]
    if not regions and not market_cities:
        return 0
    city = str(lead.get("city", "")).lower()
    state = str(lead.get("state", "")).lower().strip()
    # City first: it survives a market written with a full state name, which no
    # abbreviation lookup can resolve without a static table.
    if city and any(c and c in city for c in market_cities):
        return cfg["max"]
    abbr = state if len(state) == 2 else cfg.get("state_aliases", {}).get(state, "")
    if abbr in regions:
        cities = regions[abbr]
        if not city or any(c in city for c in cities):
            return cfg["max"]
    return 0


def score_multi_source(lead: dict[str, Any], cfg: dict[str, Any]) -> int:
    count = int(lead.get("source_count", 1) or 1)
    for threshold, pts in sorted(cfg["tiers"], reverse=True):
        if count >= threshold:
            return min(pts, cfg["max"])
    return 0


# ---- pipeline timing ----

def _subtract_months(d: date, months: int) -> date:
    total = (d.year * 12 + (d.month - 1)) - months
    return date(total // 12, total % 12 + 1, 1)


def _months_between(a: date, b: date) -> int:
    return (a.year - b.year) * 12 + (a.month - b.month)


def _estimate_month(text: str) -> int:
    t = text.lower()
    if any(w in t for w in ("q1", "winter", "early", "january", "february", "march")):
        return 2
    if any(w in t for w in ("q2", "spring", "april", "may", "june")):
        return 5
    if any(w in t for w in ("q3", "summer", "july", "august", "september")):
        return 8
    if any(w in t for w in ("q4", "fall", "autumn", "october", "november", "december")):
        return 11
    if "mid" in t:
        return 6
    if "late" in t or "end" in t:
        return 11
    return 6


def parse_estimated_date(text: str) -> date | None:
    if not text:
        return None
    t = text.lower().strip()
    for pattern in (
        r"(?:complet|open|finish|ready|occupy|done|end|deliver)\w*\s+(?:in\s+)?(?:(?:early|mid|late|spring|summer|fall|winter|q[1-4])\s+)?(\d{4})",
        r"(\d{4})\s*(?:complet|open|finish|target)",
    ):
        m = re.search(pattern, t)
        if m and 2024 <= int(m.group(1)) <= 2035:
            return date(int(m.group(1)), _estimate_month(t), 1)
    rng = re.search(r"(\d{4})\s*[-–to]+\s*(\d{4})", t)
    if rng and 2024 <= int(rng.group(2)) <= 2035:
        return date(int(rng.group(2)), _estimate_month(t), 1)
    yr = re.search(r"(20[2-3]\d)", t)
    if yr:
        return date(int(yr.group(1)), _estimate_month(t), 1)
    return None


def _pipeline_from_judgment(
    prospect: dict[str, Any], cfg: dict[str, Any]
) -> dict[str, Any] | None:
    """A model-decided stage in `calculate_pipeline`'s return shape, or None.

    ⚠️ **VENDORED-ENGINE ADDITION** — see UPSTREAM.md. Attached by
    `aeo/phases/ai_judgment.py` as `prospect["_ai_judgment"]`.

    The scoring axis is taken from the STAGE the judge chose, not recomputed from a
    date. That consistency is the point: score the axis from the ladder while the label
    comes from the judge and the two disagree on the same row — a prospect reading
    "Decision Imminent" while carrying the 2-point "Too Late" weight.

    A stage the vocabulary does not weight (`score` absent) contributes 0 rather than
    borrowing a number from a rung it does not have. `pipeline_detail` carries the
    model's own reasoning, so the "why" survives into `score_factors` and the event log
    even before `ai_analysis` reaches its column.
    """
    judged = prospect.get("_ai_judgment")
    if not isinstance(judged, dict):
        return None
    stage = judged.get("pipeline_status")
    if not stage:
        return None

    # Prefer the weight AEO shipped with the rung; fall back to the engine's own
    # `statuses` table, whose keys match the shared ladder.
    score = judged.get("stage_score")
    if score is None:
        for entry in cfg.get("statuses") or []:
            if len(entry) >= 4 and entry[2] == stage:
                score = entry[4] if len(entry) > 4 else 0
                break
    try:
        score = int(score) if score is not None else 0
    except (TypeError, ValueError):
        score = 0

    return {
        "pipeline_status": stage,
        # Provenance travels WITH the stage. AEO re-derives a customer skill's stage
        # unless it can see this marker, because the `calculate_pipeline` fallback it
        # would otherwise be trusting is month-arithmetic that is wrong for event
        # dates. `calculate_pipeline` returns no such key, so absent == derived.
        "pipeline_source": "ai",
        "pipeline_detail": judged.get("ai_analysis") or "Stage judged by model.",
        "months_to_decision": None,
        "estimated_completion": "Unknown",
        "estimated_decision": "Unknown",
        "score": min(max(score, 0), int(cfg.get("max", 30))),
    }


def calculate_pipeline(lead: dict[str, Any], cfg: dict[str, Any], today: date) -> dict[str, Any]:
    lead_months = int(cfg["decision_lead_months"])
    # Which field carries the timing signal is the SKILL's business, not ours. This read
    # a single hardcoded `estimated_timeline`, so a skill whose signal is `permit_date`
    # or `replacement_due` could never score the timing axis — 30 of 100 points
    # unreachable, silently, for every vertical that does not use that one word.
    completion = None
    for field in cfg.get("timing_fields") or ("estimated_timeline",):
        completion = parse_estimated_date(str(lead.get(field, "") or ""))
        if completion:
            break
    if completion:
        decision = _subtract_months(completion, lead_months)
        months = _months_between(decision, today)
        for lo, hi, status, desc, score in cfg["statuses"]:
            if lo <= months < hi:
                return {
                    "pipeline_status": status,
                    "pipeline_detail": f"{desc} Est. completion {completion:%Y-%m}, decision {decision:%Y-%m}, {months} months out.",
                    "months_to_decision": months,
                    "estimated_completion": f"{completion:%Y-%m}",
                    "estimated_decision": f"{decision:%Y-%m}",
                    "score": min(score, cfg["max"]),
                }
    phase_text = (str(lead.get("project_phase", "")) + " " + str(lead.get("project_description", ""))).lower()
    best: dict[str, Any] | None = None
    for kw, (status, detail, score) in cfg["phase_fallback"].items():
        if kw in phase_text and (best is None or score > best["score"]):
            best = {
                "pipeline_status": status,
                "pipeline_detail": f"{detail} (from project phase; no date)",
                "months_to_decision": None,
                "estimated_completion": "Unknown",
                "estimated_decision": "Unknown",
                "score": min(score, cfg["max"]),
            }
    if best:
        return best
    if str(lead.get("campaign_goal", "")).strip():
        f = cfg["campaign_goal_floor"]
        return {"pipeline_status": f["status"], "pipeline_detail": f["detail"], "months_to_decision": None,
                "estimated_completion": "Unknown", "estimated_decision": "Unknown", "score": min(f["score"], cfg["max"])}
    d = cfg["default"]
    return {"pipeline_status": d["status"], "pipeline_detail": d["detail"], "months_to_decision": None,
            "estimated_completion": "Unknown", "estimated_decision": "Unknown", "score": min(d["score"], cfg["max"])}


def score_prospects(
    prospects: list[dict[str, Any]],
    ctx: dict[str, Any],
    *,
    today: date,
) -> list[dict[str, Any]]:
    """Score, rank, and return scored items (highest score first)."""
    scoring = ctx.get("scoring", {}) if isinstance(ctx.get("scoring"), dict) else {}
    c_cfg = _deep_get(scoring, "completeness")
    f_cfg = _deep_get(scoring, "fit")
    r_cfg = _deep_get(scoring, "region_bonus")
    m_cfg = _deep_get(scoring, "multi_source")
    p_cfg = _deep_get(scoring, "pipeline")
    ai_cfg = _deep_get(scoring, "ai_adjustment")
    cap = int(scoring.get("score_cap", _DEFAULT_SCORING["score_cap"]))

    # The fields the skill asked for. Used for BOTH the merge vocabulary and
    # completeness: "how much of what this skill asked for did we actually get" is a
    # question every vertical can answer, unlike a fixed field list.
    canonical = canonical_fields_from_sources(ctx.get("sources"))
    if not (scoring.get("completeness") or {}).get("fields"):
        c_cfg = {**c_cfg, "fields": list(canonical) or list(_IDENTITY_ALIASES)}
    authored_factors = scoring.get("factors")
    has_factors = isinstance(authored_factors, list) and bool(authored_factors)

    #: Two modes, and the default is unchanged.
    #:
    #: LEGACY (`factors_max` absent): authored factors BORROW the `fit` axis — the
    #: largest single axis at 40, larger than pipeline timing — because the operator's
    #: stated ICP is the best signal available about who is worth calling, and leaving it
    #: at the legacy `fit` weight of 25 made it a minority of a score dominated by axes
    #: nobody authored.
    #:
    #: OWN-AXIS (`factors_max` set): factors are independent of `fit`, so a skill can put
    #: its whole budget on its own criteria and zero the axes it never authored.
    factors_max_cfg = scoring.get("factors_max")
    legacy_factor_axis = has_factors and factors_max_cfg is None

    # ⚠️ `is None`, NOT falsy. `fit.max: 0` is a legitimate authored value — it is how a
    # skill says "this axis contributes nothing" — and a falsy test read it as unset and
    # silently restored 40, which is the opposite of what was asked for.
    if legacy_factor_axis and (scoring.get("fit") or {}).get("max") is None:
        f_cfg = {**f_cfg, "max": 40}

    # A factor carrying BOTH graded tables is ambiguous. `tiers` wins deterministically,
    # but say so — the whole point of deriving the mode from the authored table is that a
    # table can never be silently ignored, and picking one quietly would reintroduce that.
    if has_factors:
        both = [
            factor_label(s) or str(s.get("name") or "?")
            for s in authored_factors
            if isinstance(s, dict)
            and isinstance(s.get("tiers"), (list, tuple)) and s.get("tiers")
            and normalize_keyword_table(s.get("keywords"))
        ]
        if both:
            _log_from(ctx)(
                "scoring: factors with BOTH tiers and keywords, scoring by tiers: "
                + ", ".join(both)
            )

    dq_rules = scoring.get("disqualify_rules")
    bands = scoring.get("priority_bands")
    gap = band_coverage_gap(bands, cap)
    if gap:
        _log_from(ctx)(f"scoring: priority_bands — {gap}")

    # Budget advisory. **Warn, never fail:** over-budget is authorable and the documented
    # behaviour is to clamp at `score_cap`, so refusing to run would reject configs that
    # score correctly. Silence is the actual hazard — a skill whose axes sum to 60 can
    # never produce a top-band prospect, and nothing anywhere says so.
    try:
        budget = sum(int(_deep_get(scoring, a).get("max", 0) or 0) for a in _BUDGET_AXES)
        if has_factors:
            budget += int(
                (f_cfg.get("max", 25) if legacy_factor_axis else factors_max_cfg) or 0
            )
            if legacy_factor_axis:
                budget -= int(f_cfg.get("max", 25) or 0)  # borrowed, not additional
        if budget != cap:
            _log_from(ctx)(
                f"scoring: axis budget {budget} != score_cap {cap} — "
                f"{'unreachable top band' if budget < cap else 'clamped at cap'}"
            )
    except (TypeError, ValueError, AttributeError):
        pass

    # ── org-sourced values ────────────────────────────────────────────────────
    # Anything the engine needs that the skill config does not carry comes from the
    # organization the scan is running FOR, never from a default.
    org = ctx.get("organization") if isinstance(ctx.get("organization"), dict) else {}
    if not (scoring.get("region_bonus") or {}).get("regions"):
        derived_regions, market_cities = regions_from_markets(org.get("markets"))
        if derived_regions or market_cities:
            r_cfg = {**r_cfg, "regions": derived_regions, "market_cities": market_cities}
    if not (scoring.get("pipeline") or {}).get("decision_lead_months"):
        cycle = org.get("sales_cycle_months", ctx.get("sales_cycle_months"))
        try:
            cycle = int(cycle) if cycle is not None else None
        except (TypeError, ValueError):
            cycle = None
        if cycle and cycle > 0:
            p_cfg = {**p_cfg, "decision_lead_months": cycle}
    if not (scoring.get("pipeline") or {}).get("timing_fields"):
        derived_timing = timing_fields_from_authored(canonical)
        if derived_timing:
            p_cfg = {**p_cfg, "timing_fields": derived_timing}

    scored: list[dict[str, Any]] = []
    for p in prospects:
        lead = _scoring_input(p, canonical)

        # `_merge_raw_rows` STRINGIFIES every value, because discovery merging exists to
        # produce text a keyword match can read. That destroys structure: a list of dated
        # event rows arrives as "{'signal_type': ...}" — one element, as a string — so an
        # event factor walking rows would see nothing at all and score every prospect the
        # same. Overlay the structured values back, without touching the merge itself.
        for key, value in p.items():
            if isinstance(value, (list, dict)) and key not in _STRUCTURED_RESERVED:
                lead[key] = value
        enrichment = p.get("validation_data")
        if isinstance(enrichment, dict):
            # Enrichment lanes are keyed by lane name, so a factor binds to a lane the
            # same way it binds to a collected field.
            for lane, rows in enrichment.items():
                lead.setdefault(lane, rows)
        # ⚠️ **VENDORED-ENGINE EDIT** — logged in UPSTREAM.md's edit table.
        #
        # A model-decided stage wins over the date ladder. `aeo/phases/ai_judgment.py`
        # attaches `_ai_judgment` before this runs, having read each prospect's dated
        # event TOGETHER WITH its type — which is the half `calculate_pipeline` cannot
        # see. On a real run the ladder filed a `Commercial building permit` dated
        # 2026-02-20 and a `Lease` from December 2019 under the same verdict.
        #
        # `calculate_pipeline` itself is untouched: it stays the fallback for a prospect
        # the judge did not reach, so a failed model call degrades to today's behaviour
        # rather than to nothing.
        pipeline = _pipeline_from_judgment(p, p_cfg) or calculate_pipeline(
            lead, p_cfg, today
        )
        # Ruled out BEFORE any factor work: a disqualified prospect costs nothing to
        # score, and cannot end up recorded as both strongly-fitting and excluded.
        dq_reason = hard_disqualifier(lead, dq_rules)

        completeness = score_completeness(lead, c_cfg)
        # Authored factors take precedence over the legacy keyword table. In LEGACY mode
        # they occupy the same axis (`fit.max`) so the 0-100 scale is unchanged; in
        # OWN-AXIS mode they are additive and `fit` is scored independently.
        factor_detail: dict[str, float] = {}
        factors_pts = 0
        if has_factors:
            axis_max = (
                int(f_cfg.get("max", 25)) if legacy_factor_axis else int(factors_max_cfg)
            )
            factors_pts, factor_detail = score_config_factors(
                lead, authored_factors, axis_max, today=today
            )
        # 🔴 **Never apply the vendored DEFAULT keyword table to a config that authored
        # its own factors.** That default is the original customer's church-AV vocabulary
        # (`new construction: 25`, `renovation: 12`), and until own-axis mode existed it
        # was unreachable for any skill with factors, because factors REPLACED the fit
        # axis. Own-axis mode made it reachable again: measured on a flooring prospect
        # whose text says "office renovation", the axis silently awarded **12 points from
        # a church keyword list**. That is exactly the static-industry coupling the PO
        # ruled out, reintroduced by a change that looked purely additive.
        #
        # A skill that wants a keyword axis alongside its factors authors
        # `fit.keyword_scores` and gets it. One that does not gets zero rather than
        # someone else's vertical. A config with NO factors is the legacy shape the
        # default exists for, and is untouched.
        if legacy_factor_axis or (has_factors and not (scoring.get("fit") or {}).get("keyword_scores")):
            fit = 0
        else:
            fit = score_fit(lead, f_cfg)
        region = score_region(lead, r_cfg)
        multi = score_multi_source(lead, m_cfg)
        timing = pipeline["score"]

        ai_adj = p.get("ai_score_adjustment", lead.get("ai_score_adjustment", 0))
        try:
            ai_adj = max(ai_cfg["min"], min(ai_cfg["max"], int(ai_adj)))
        except (TypeError, ValueError):
            ai_adj = 0

        total = max(
            0, min(cap, completeness + fit + factors_pts + region + multi + timing + ai_adj)
        )
        # The clamp above is applied AFTER `ai_adj`, deliberately: a base of 95 plus a +15
        # adjustment must land at `cap`, not above the top band, or the band lookup falls
        # off the end of the table and the best prospect in the run renders unbanded.
        if dq_reason:
            total = 0

        # `disqualify_below` — authored by CSB-built skills since day one and read by
        # NOTHING until now. **Implemented as a FLAG, not a filter, deliberately:** the
        # operator picked the threshold before ever seeing a score distribution, and on
        # the first real run every prospect scored 11-12 against a threshold of 20, so
        # filtering would have silently deleted a scan's entire yield with no error
        # anywhere. Flagged, the operator sees both the prospects and their own cutoff
        # and can move it. Absent/invalid threshold => no flag at all.
        disqualified: bool | None = None
        try:
            floor = scoring.get("disqualify_below")
            if floor is not None:
                disqualified = total < float(floor)
        except (TypeError, ValueError):
            disqualified = None
        # A hard rule outranks the floor in both directions. It is a statement about
        # eligibility, not about rank, so it must not be softened by a generous cutoff —
        # nor must a strict cutoff be reported as though a rule had fired.
        if dq_reason:
            disqualified = True

        scored.append({
            "prospect_id": p.get("id"),
            "company_name": p.get("company_name") or lead.get("organization_name", ""),
            "city": lead.get("city", ""),
            "state": lead.get("state", ""),
            # ⚠️ round(): `total` is a float whenever `ai_adj` is fractional, and the
            # model may answer 3.5. AEO's `prospects.score` is an INTEGER column and
            # its driver sends a JS float as TEXT, so 75.5 arrives as '75.5' and the
            # whole callback 500s. Held for months only because `ai_score_adjustment`
            # had no producer and 0 kept every total integral.
            "score": round(total),
            "pipeline_status": pipeline["pipeline_status"],
            # "ai" when the judgment phase decided this stage, "derived" when the
            # engine's date arithmetic did. AEO keeps the former for any skill type
            # and re-derives the latter for a customer skill.
            "pipeline_source": pipeline.get("pipeline_source", "derived"),
            "pipeline_detail": pipeline["pipeline_detail"],
            "estimated_completion": pipeline["estimated_completion"],
            "estimated_decision": pipeline["estimated_decision"],
            "months_to_decision": pipeline["months_to_decision"],
            "contact_name": lead.get("key_contact", ""),
            # The fields this skill actually authored, whatever they are. Replaced six
            # hardcoded church-AV keys (project_description/_type/_phase, campaign_goal,
            # denomination, estimated_timeline) that no non-church skill ever populates.
            "fields": {f: lead.get(f, "") for f in canonical},
            "sources_found_in": lead.get("sources_found_in", ""),
            "multi_source": lead.get("multi_source", False),
            "score_factors": {
                "completeness": completeness,
                # In LEGACY mode the factor score IS the fit axis, and it is reported
                # under `fit` exactly as before — existing operators and FE surfaces read
                # that key, so own-axis mode ADDS `factors_score` rather than moving it.
                "fit": factors_pts if legacy_factor_axis else fit,
                # Own-axis mode only. A config with NO authored factors must not gain a
                # `factors_score: 0` key it never had — that is a shape change to every
                # existing consumer for no information.
                **(
                    {"factors_score": factors_pts}
                    if has_factors and not legacy_factor_axis
                    else {}
                ),
                "region_bonus": region,
                "multi_source": multi, "pipeline_timing": timing, "is_region": region > 0,
                # Per-factor credit for the skill's own authored factors, so an operator
                # can see WHICH of their criteria a prospect met.
                **({"factors": factor_detail} if factor_detail else {}),
            },
            "ai_score_adjustment": ai_adj,
            **({"disqualified": disqualified} if disqualified is not None else {}),
            **({"disqualifier_reason": dq_reason} if dq_reason else {}),
            **(
                {"priority_band": band}
                if (band := priority_band(round(total), bands))
                else {}
            ),
        })

    scored.sort(key=lambda x: x["score"], reverse=True)
    for rank, item in enumerate(scored, 1):
        item["rank"] = rank
    return scored


# =============================================================================
# Output sinks
# =============================================================================


class Sink:
    """Routes events. Subclasses persist to a file (default) or stream NDJSON."""

    def emit(self, event: dict[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self, *, organization: str) -> None:  # pragma: no cover - interface
        pass


class StreamSink(Sink):
    """Write each event as one NDJSON line to a stream (default: stdout).

    This is the sink an orchestrator uses (`--out -`) to forward each event to
    its own destination — e.g. AEO's POST /runtime/scans/{id}/events.
    """

    def __init__(self, stream=sys.stdout):
        self._stream = stream

    def emit(self, event: dict[str, Any]) -> None:
        try:
            self._stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._stream.flush()
        except BrokenPipeError:
            # The reader (an orchestrator consuming our NDJSON) went away — e.g.
            # it exited after a downstream POST failed. Nothing left to stream
            # to; swallow so we don't crash-trace on top of the real error.
            raise SystemExit(3)


class FileSink(Sink):
    """Accumulate events and persist the aggregated result to a JSON file.

    The default sink (Scenario 1). Applies the caller's `output` block:
    `top_n` truncates the ranked list, `fields` projects each scored item.
    """

    def __init__(self, path: str, output_cfg: dict[str, Any]):
        self._path = path
        self._output = output_cfg or {}
        self._prospects: list[dict[str, Any]] = []
        self._scored: list[dict[str, Any]] = []
        self._summary: dict[str, Any] = {}
        self._error: str | None = None

    def emit(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "prospects":
            self._prospects.extend(event.get("items", []))
        elif etype == "scored":
            self._scored = event.get("items", [])
        elif etype == "completed":
            self._summary = event.get("summary", {})
        elif etype == "error":
            self._error = event.get("message")

    def close(self, *, organization: str) -> None:
        scored = self._scored
        top_n = self._output.get("top_n")
        if isinstance(top_n, int) and top_n > 0:
            scored = scored[:top_n]
        fields = self._output.get("fields")
        if isinstance(fields, list) and fields:
            scored = [{k: item.get(k) for k in fields} for item in scored]

        result = {
            "organization_name": organization,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "summary": self._summary,
            "scored": scored,
            "prospects": self._prospects,
        }
        if self._error:
            result["error"] = self._error
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        Path(self._path).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        # A single confirmation line on stderr so stdout stays clean for pipes.
        print(f"[av-lead-scanner] wrote {len(scored)} scored / {len(self._prospects)} prospects → {self._path}", file=sys.stderr)


def make_sink(out: str, ctx: dict[str, Any]) -> Sink:
    """Build the sink from --out and the context's `output` block."""
    output_cfg = ctx.get("output", {}) if isinstance(ctx.get("output"), dict) else {}
    if out == "-":
        return StreamSink()
    if out:
        return FileSink(out, output_cfg)
    # No --out given: default filename from the context or the org name.
    path = output_cfg.get("path") or f"av-lead.{_slug(organization_name(ctx))}.json"
    return FileSink(path, output_cfg)


def _resolve_today(value: str | None) -> date:
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    env = os.environ.get("AV_SCANNER_TODAY")
    if env:
        return datetime.strptime(env, "%Y-%m-%d").date()
    return date.today()


# =============================================================================
# CLI
# =============================================================================


# Registry of grounded-search providers. Add a provider = one function of the
# SearchProvider signature + one entry here + a default model below.
_PROVIDERS: dict[str, SearchProvider] = {
    "gemini": gemini_provider,
    "claude": claude_provider,
    "mock": mock_provider,
}

_PROVIDER_DEFAULT_MODEL: dict[str, str] = {
    "gemini": "gemini-3-flash-preview",
    "claude": "claude-opus-4-8",
    "mock": "mock-model",
}


def _provider_config(ctx: dict[str, Any], provider_name: str) -> dict[str, Any]:
    """Resolve the provider's tunables from `ctx[<provider_name>]` (e.g. the
    `gemini` or `claude` block), applying provider-appropriate defaults."""
    block = ctx.get(provider_name) if isinstance(ctx.get(provider_name), dict) else {}
    return {
        "model": block.get("model", _PROVIDER_DEFAULT_MODEL.get(provider_name, "")),
        "temperature": float(block.get("temperature", 0.1)),
        "entries_per_query": int(block.get("entries_per_query", 3)),
        "retry_attempts": int(block.get("retry_attempts", 3)),
        "max_concurrency": max(1, int(block.get("max_concurrency", 6))),
    }


def _pick_provider(provider_name: str, mock: bool, dry_run: bool) -> SearchProvider:
    if dry_run:
        def _dry(prompt: str, **_: Any) -> str:  # pragma: no cover - trivial
            print(prompt, file=sys.stderr)
            return "[]"
        return _dry
    if mock:
        return mock_provider
    provider = _PROVIDERS.get(provider_name)
    if provider is None:
        raise ValueError(f"unknown provider {provider_name!r}; choose from {sorted(_PROVIDERS)}")
    return provider


def cmd_discover(args: argparse.Namespace) -> int:
    ctx = load_context(args.context)
    problems = validate_context(ctx, need_sources=True)
    if problems:
        print("context invalid:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 2
    sink = make_sink(args.out, ctx)
    provider = _pick_provider(args.provider, args.mock, args.dry_run)
    pconf = _provider_config(ctx, args.provider)
    scan_run_id = args.scan_run_id or os.environ.get("SCAN_RUN_ID") or str(uuid.uuid4())
    start = datetime.now()
    try:
        prospects = discover(ctx, scan_run_id=scan_run_id, provider=provider,
                             emit=sink.emit, provider_config=pconf)
        sink.emit({"type": "completed", "summary": {
            "phase": "discover", "total_prospects": len(prospects),
            "duration_seconds": round((datetime.now() - start).total_seconds(), 2),
        }})
    except Exception as exc:  # noqa: BLE001
        sink.emit({"type": "error", "message": str(exc)})
        sink.close(organization=organization_name(ctx))
        log.exception("discover failed")
        return 1
    sink.close(organization=organization_name(ctx))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    ctx = load_context(args.context)
    prospects = ctx.get("prospects")
    if not isinstance(prospects, list):
        print("score expects a `prospects` array in the input context", file=sys.stderr)
        return 2
    sink = make_sink(args.out, ctx)
    today = _resolve_today(args.today)
    start = datetime.now()
    try:
        scored = score_prospects(prospects, ctx, today=today)
        sink.emit({"type": "phase_start", "phase": "score"})
        sink.emit({"type": "scored", "phase": "score", "items": scored})
        sink.emit({"type": "phase_complete", "phase": "score", "count": len(scored)})
        sink.emit({"type": "completed", "summary": {
            "phase": "score", "total_scored": len(scored),
            "duration_seconds": round((datetime.now() - start).total_seconds(), 2),
        }})
    except Exception as exc:  # noqa: BLE001
        sink.emit({"type": "error", "message": str(exc)})
        sink.close(organization=organization_name(ctx))
        log.exception("score failed")
        return 1
    sink.close(organization=organization_name(ctx))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    ctx = load_context(args.context)
    problems = validate_context(ctx, need_sources=True)
    if problems:
        print("context invalid:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 2
    sink = make_sink(args.out, ctx)
    provider = _pick_provider(args.provider, args.mock, args.dry_run)
    pconf = _provider_config(ctx, args.provider)
    today = _resolve_today(args.today)
    scan_run_id = args.scan_run_id or os.environ.get("SCAN_RUN_ID") or str(uuid.uuid4())
    start = datetime.now()
    try:
        prospects = discover(ctx, scan_run_id=scan_run_id, provider=provider,
                             emit=sink.emit, provider_config=pconf)
        scored = score_prospects(prospects, ctx, today=today)
        sink.emit({"type": "phase_start", "phase": "score"})
        sink.emit({"type": "scored", "phase": "score", "items": scored})
        sink.emit({"type": "phase_complete", "phase": "score", "count": len(scored)})
        sink.emit({"type": "completed", "summary": {
            "phase": "run", "total_prospects": len(prospects), "total_scored": len(scored),
            "duration_seconds": round((datetime.now() - start).total_seconds(), 2),
        }})
    except Exception as exc:  # noqa: BLE001
        sink.emit({"type": "error", "message": str(exc)})
        sink.close(organization=organization_name(ctx))
        log.exception("run failed")
        return 1
    sink.close(organization=organization_name(ctx))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="av-lead-scanner", description=__doc__.split("\n")[0])
    parser.add_argument("--log-level", default="WARNING", help="Python log level (default WARNING)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, *, needs_provider: bool) -> None:
        p.add_argument("--context", "--in", default="-", help="Path to context JSON, or '-' for stdin (default)")
        p.add_argument("--out", default="", help="Output path; '-' streams NDJSON to stdout; empty → default file")
        p.add_argument("--today", default=None, help="Override 'today' as YYYY-MM-DD (for deterministic pipeline timing)")
        if needs_provider:
            p.add_argument("--provider", default="gemini", choices=sorted(_PROVIDERS),
                           help="Grounded-search provider for discovery (default: gemini)")
            p.add_argument("--mock", action="store_true", help="Use the offline deterministic provider (no API key)")
            p.add_argument("--dry-run", action="store_true", help="Print prompts to stderr, return no results")
            p.add_argument("--scan-run-id", default=None, help="Scan run id (else $SCAN_RUN_ID or a random uuid)")

    p_disc = sub.add_parser("discover", help="Run discovery sources → prospects")
    add_common(p_disc, needs_provider=True)
    p_disc.set_defaults(func=cmd_discover)

    p_score = sub.add_parser("score", help="Score + rank prospects (deterministic, no network)")
    add_common(p_score, needs_provider=False)
    p_score.set_defaults(func=cmd_score)

    p_run = sub.add_parser("run", help="discover → score → completed")
    add_common(p_run, needs_provider=True)
    p_run.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING),
                        format="%(levelname)s %(name)s: %(message)s")
    # `score` has no provider flags; give them harmless defaults.
    for attr in ("mock", "dry_run", "scan_run_id"):
        if not hasattr(args, attr):
            setattr(args, attr, None)
    if not hasattr(args, "provider"):
        args.provider = "gemini"
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
