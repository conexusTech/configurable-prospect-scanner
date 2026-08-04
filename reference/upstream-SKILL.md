---
name: av-lead-scanner
description: "Generic, LLM-agnostic prospecting tool. Discovers and ranks sales leads by hunting upstream proxy signals for an organization, driven entirely by a company context you supply. Any LLM can drive it as a tool: construct the context, run the phases, consume the ranked output. Usage: python3 av_lead_scanner.py {discover|score|run} --context <json> --out <file|->"
user-invocable: true
metadata:
  requires:
    bins: [python3]
  emoji: "🔭"
---

# av-lead-scanner

A self-contained CLI that **any LLM (Claude, Gemini, …) can drive as a tool**
to do prospecting. You feed it an *organization context* (who the company is,
what signals to hunt, how to score) and it returns a **ranked list of scored
leads**. Nothing about the business domain is hardcoded — the church/AV setup
in `examples/organization.json` is just one context; swap it for any domain.

You (the LLM) do two things: **construct the input context** and **consume the
output**. The tool does the deterministic heavy lifting in between.

---

## The core idea

Organizations rarely announce "we're ready to buy." Instead the tool hunts
**upstream proxy signals** — the activities that reliably precede a purchase —
across several **sources**, dedupes what it finds across those sources, and
**scores each lead by how well it fits and how well-timed it is**. Earlier in
the buying timeline = more lead time = higher score.

Two phases:

| Phase | Command | LLM/API? | What it does |
|---|---|---|---|
| **Discovery** | `discover` | Yes (grounded search) | Runs each source's search queries through a provider, dedupes the union by normalized company name, emits the unified prospect list. |
| **Scoring** | `score` | No — pure & deterministic | Merges cross-source matches, scores 5 config-defined factors + optional AI adjustment, assigns a timeline/pipeline stage, ranks. |

`run` does both in one call.

**Two ways to get prospects into scoring:**
1. **Tool-driven discovery** (`discover`/`run`) — the tool calls a grounded-search
   provider. Pick with `--provider`:
   - `gemini` (default) — Gemini + Google Search grounding; needs `GEMINI_API_KEY`.
   - `claude` — Claude + the Anthropic web-search tool; needs `ANTHROPIC_API_KEY`
     (or an `ant auth login` profile). Per-provider knobs live in a `claude` /
     `gemini` block in the context (`model`, `entries_per_query`, …).
2. **LLM-driven discovery** — *you* do the web research with your own tools,
   hand the tool a `prospects` array, and call `score`. No API key needed. This
   is the most portable path and works with any LLM.

---

## Basic workflow (any LLM — start here)

**Scenario:** a marketing manager tells an AI assistant:

> "Using the av-lead-scanner tool, read `organization.json` as the company
> context and save the generated data as `av-lead.<organization name>.json`."

What the assistant does:

1. Read this SKILL.md and `organization.json`.
2. Decide the discovery path:
   - If a `GEMINI_API_KEY` is available → let the tool discover:
     ```bash
     python3 av_lead_scanner.py run --context organization.json --out "av-lead.csd.json"
     ```
   - If not → do the web research yourself, assemble a `prospects` array (see
     schema below), write it into a context JSON, and score it:
     ```bash
     python3 av_lead_scanner.py score --context with_prospects.json --out "av-lead.csd.json"
     ```
   - No key and just want to see it work end-to-end offline → add `--mock`:
     ```bash
     python3 av_lead_scanner.py run --context organization.json --mock --out "av-lead.csd.json"
     ```
3. Read back `av-lead.csd.json`, summarize the top leads, act (draft outreach,
   flag home-market prospects, etc.).

The **output filename is your call** — the manager asked for
`av-lead.<organization name>.json`, so name it that. By default (no `--out`),
the tool writes `av-lead.<org-slug>.json` (or `output.path` from the context).

---

## Input — the organization context

A single JSON object. Only `sources` is required for discovery; `score` instead
requires a `prospects` array. All of it is data you (the LLM) construct.

```jsonc
{
  "organization":       { "name": "CSD", "markets": ["TX","CO","IN"] },
  "product_description": "What the company sells — used to ground search prompts.",

  "gemini": {                        // discovery provider knobs (optional)
    "model": "gemini-3-flash-preview",
    "temperature": 0.1,
    "entries_per_query": 3,          // results requested per query
    "retry_attempts": 3
  },

  "output": {                        // how to shape the persisted file (optional)
    "path": "av-lead.csd.json",      // default output path
    "top_n": 50,                     // keep only the top N ranked leads
    "fields": ["rank","score","company_name","city","state","pipeline_status"]
  },

  "sources": {                       // REQUIRED for `discover`/`run`
    "<source_name>": {
      "name_field": "organization_name",   // which field holds the company name
      "fields": ["organization_name","project_description","city","state", "..."],
      "queries": ["natural-language search 1", "search 2"],
      "seed_firms": ["Known Co A", "Known Co B"],   // told to the model as "find OTHERS"
      "prompt": "optional custom template with {query} {n} {seed_context}"
    }
  },

  "scoring": { /* optional overrides — see Scoring below */ },

  "prospects": [ /* REQUIRED for `score` — see shape below */ ]
}
```

**Notes for the driving LLM:**
- Prompts live in the context, not the code. If you omit a source's `prompt`, the
  tool synthesizes one from `product_description` + `fields`. Provide a custom
  `prompt` only when you want hand-tuned wording.
- `seed_firms` are names the model is told it already knows — it returns *new*
  ones. Use them to steer coverage, not to seed the output.
- To generalize to a new domain, change `product_description`, the `sources`
  (queries/fields), and the `scoring` knobs. No code changes.

### `prospects` shape (for `score`)

Each item is a flat lead dict. Any of these fields (all optional except a name)
are read; unknown fields are ignored:

```jsonc
{
  "organization_name": "Cornerstone Community Church",
  "city": "Frisco", "state": "TX",
  "project_description": "New 1,800-seat sanctuary",
  "project_type": "new build",
  "project_phase": "capital campaign",
  "estimated_timeline": "completion Fall 2027",
  "campaign_goal": "$14,000,000",
  "denomination": "Nondenominational",
  "key_contact": "Pastor James Reed",
  "av_opportunity_notes": "Full AV buildout.",
  "source_count": 3,              // how many independent sources found it (multi-source bonus)
  "ai_score_adjustment": 8        // optional: YOUR -15..+15 qualitative nudge
}
```

You can also pass the exact output of a prior `discover` run (objects with
`discovery_data`) — the tool reconstructs the scoring fields automatically.

---

## Output — an event stream, your choice of sink

The tool emits a sequence of **events**. Where they go is up to the caller:

```jsonc
{"type":"phase_start",    "phase":"active_campaigns"}
{"type":"phase_complete", "phase":"active_campaigns", "count": 12}
{"type":"prospects",      "phase":"discover", "items":[ /* deduped unified leads */ ]}
{"type":"scored",         "phase":"score",    "items":[ /* ranked scored leads */ ]}
{"type":"completed",      "summary":{ "total_prospects": 63, "total_scored": 63, ... }}
{"type":"error",          "message":"..."}      // on failure
```

Two sinks:

- **File (default)** — `--out <path>` (or the context's `output.path`, or the
  auto name `av-lead.<org>.json`). Persists the aggregated result:
  ```jsonc
  { "organization_name","generated_at","summary","scored":[...],"prospects":[...] }
  ```
  This is the default and matches "save the generated data as a JSON file."
- **Stream** — `--out -` prints one event per line (NDJSON) to stdout as it
  happens. Use this when an orchestrator wants to forward each event elsewhere
  (see the AEO integration).

---

## Scoring (deterministic, configurable)

Final score is capped at 100:

```
score = completeness(0-15) + fit(0-25) + region_bonus(0-10)
      + multi_source(0-10) + pipeline_timing(0-30) + ai_adjustment(-15..+15)
```

| Factor | What it measures | Config key |
|---|---|---|
| **completeness** | how many actionable fields are filled | `scoring.completeness.fields` |
| **fit** | keyword match of project text → opportunity size | `scoring.fit.keyword_scores` |
| **region_bonus** | is the lead in a served market | `scoring.region_bonus.regions` |
| **multi_source** | found by ≥2 independent sources = higher confidence | `scoring.multi_source.tiers` |
| **pipeline_timing** | how well-timed vs the buying decision | `scoring.pipeline.*` |
| **ai_adjustment** | your qualitative −15..+15 per lead (optional) | per-prospect `ai_score_adjustment` |

**Pipeline timing** is the heaviest factor: it estimates the buying-decision
date (default = project completion − `decision_lead_months`, 13) and maps
months-to-decision to a stage (`1 - Early Discovery` … `7 - Too Late`). If no
date parses, it falls back to `project_phase` keywords. Earlier stage = more
lead time = higher score. Every band, stage, and weight is overridable under
`scoring` — see `av_lead_scanner.py` `_DEFAULT_SCORING` for the full defaults.

> The AI-adjustment factor is **supplied by you** in each prospect's
> `ai_score_adjustment` (the core tool does no LLM calls during scoring). If you
> want the tool to compute it, do a quick inference pass per lead and set the
> field before calling `score`.

---

## Commands

```bash
# One-shot: discover (Gemini) → score → persist to file
python3 av_lead_scanner.py run --context organization.json --out av-lead.csd.json

# Offline demo (no API key): synthetic deterministic prospects
python3 av_lead_scanner.py run --context organization.json --mock --out av-lead.csd.json

# Discover with Claude instead of Gemini (needs ANTHROPIC_API_KEY)
python3 av_lead_scanner.py run --context organization.json --provider claude --out av-lead.csd.json

# Discovery only, stream events for an orchestrator to forward
python3 av_lead_scanner.py discover --context organization.json --out -

# Scoring only (no key, no network) on an LLM-built prospects array
python3 av_lead_scanner.py score --context with_prospects.json --out av-lead.csd.json

# Deterministic pipeline timing for tests: pin "today"
python3 av_lead_scanner.py run --context organization.json --mock --today 2026-07-06 --out -
```

Flags: `--context/--in` (path or `-` for stdin), `--out` (file path, or `-` for
NDJSON stdout, or empty for the default file), `--mock`, `--dry-run` (print
prompts, no results), `--today YYYY-MM-DD`, `--scan-run-id`.

Environment: `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` (discovery, per provider),
`AV_SCANNER_TODAY` (alt to `--today`), `SCAN_RUN_ID` (alt to `--scan-run-id`).

---

## Sample integration: embed in an AEO skill

See `examples/aeo_integration.py`. It runs as a k8s Job, reads `ORGANIZATION_ID`
+ `SCAN_RUN_ID` from env, fetches the context from aeo-backend
(`GET /api/runtime/organizations/:orgId/context` — same shape as
`organization.json`), validates it, then drives the tool with a custom sink that
forwards each event to `POST /runtime/scans/{scan_run_id}/events` as
`type=prospects | scored | completed | error`. The core tool stays generic — all
AEO specifics live in that wrapper's `Sink` subclass. See `HANDOVER.md` for the
full deployment story.
