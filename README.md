# configurable-prospect-scanner

The single, config-driven prospect scanner behind every skill the AEO platform
builds. Registered in conqrse-queue as **one** catalog entry, `taskRef:
configurable-prospect-scanner` — decision C-1: many skills, one runtime. Per-skill
behaviour arrives as **config in the org's runtime context**, never as a separate
image, so this repo must contain no vertical logic and no customer data.

The repo name, the queue `taskRef`, and `skills.runtime_slug` in aeo-backend are
deliberately the **same string**. The Conversational Skill Builder feature produced
five distinct name-collision defects; this removes a sixth.

## What is ours and what is not

| Path | Provenance | Rule |
|---|---|---|
| `av_lead_scanner.py` | **vendored** from `skill-av-lead-scanner` @ `3791a71` | **do not edit** — see [UPSTREAM.md](UPSTREAM.md) |
| `tests/` | vendored | upstream's tests for the engine |
| `aeo/` | **ours** | all AEO-specific behaviour lives here |
| `reference/` | vendored, for reading only | not imported, not shipped |

That split is not our invention — upstream drew it themselves. Their integration
example states the core tool "stays generic — it knows nothing about AEO" and that
AEO-specific behaviour belongs in a wrapper. `aeo/` is that wrapper, promoted from
example to product.

## How a run works

```
queue task  →  aeo/runner.py
                 1. GET  {AEO_BACKEND_URL}/runtime/organizations/{ORGANIZATION_ID}/context
                 2. aeo/config_mapping.py   AEO context ─→ engine context
                 3. av_lead_scanner.discover(...)  →  score_prospects(...)
                 4. POST {AEO_BACKEND_URL}/runtime/scans/{SCAN_RUN_ID}/events   (per event)
```

## The mapping is the point of this repo

The engine reads a flat context: `organization`, `product_description`, `gemini`,
`output`, `sources`, `scoring`. AEO's runtime context returns something else — org
columns, resolved geography, personas, products, and the authored skill recipe
nested under `skill.config` in the Skill Builder's schema.

**Those two shapes were never reconciled.** Upstream's example asserted AEO returns
the engine's own shape; it does not — four of six top-level keys are absent. Left
unmapped, a conversationally-authored config reaches the engine and is **silently
ignored**: the scan runs on defaults, returns plausible prospects, and nothing
errors. `aeo/config_mapping.py` exists to make that impossible, and it **refuses
rather than defaults** whenever the authored recipe cannot drive a real scan —
because a default discovery strategy is not a smaller feature, it is a wrong answer
wearing a confident face.

### Known gaps, stated rather than discovered later

- **`discovery.sources` is a proposal.** The Skill Builder's config schema leaves
  the `discovery` section's internals open (`additionalProperties: true`) because
  they were never ratified. This repo proposes the engine's own source shape as
  those internals — settled by something that executes rather than by discussion.
- **`validation` and `contacts` do not run.** The PRD's scanner has five phases;
  this engine exposes discovery and scoring. A config authoring those sections gets
  a loud warning at start-up, not silence and a short result set.
- The corrected context path: upstream fetched `/api/runtime/...`; aeo-backend sets
  **no global prefix**, so that 404s. Fixed here, verified against its controller.

## Local run (no Docker, no queue)

The cheapest useful test, and it needs neither of the platform's blocked
dependencies — the ordinary scan path is live, only the builder's chat and
test-run endpoints are stubs.

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt

export AEO_BACKEND_URL=http://localhost:3000
export ORGANIZATION_ID=<an org in your local aeo_platform>
export SCAN_RUN_ID=<a row in scan_runs, so events have something to bind to>
export CI_USER=... CI_PASSWORD=...
export AV_SCANNER_MOCK=1        # offline provider — no model key needed

python -m aeo.runner
```

Expect one of three informative outcomes: a mapping refusal naming exactly which
config sections are unusable, an engine rejection of the mapped context, or a real
(mock-sourced) run whose events land on the scan run.

## Container

```bash
docker build -t configurable-prospect-scanner:dev .
```

The entrypoint is `aeo.runner`, never the raw engine — invoking the tool directly
skips the mapping and quietly scans on defaults. Register the image against
`taskRef: configurable-prospect-scanner` in conqrse-queue's catalog (DB-backed CRUD,
no code change needed there).

## Environment

| Var | Required | Purpose |
|---|---|---|
| `ORGANIZATION_ID` | yes | whose runtime context to fetch |
| `SCAN_RUN_ID` | yes | binds every emitted event to a scan run |
| `AEO_BACKEND_URL` | yes | e.g. `http://aeo-backend` |
| `CI_USER` / `CI_PASSWORD` | for real runs | HTTP Basic on both AEO calls |
| `AV_SCANNER_MOCK` | no | `1` → offline provider, no model key |
| `AV_SCANNER_PROVIDER` | no | `gemini` \| `claude` when not mocking |
| `GEMINI_API_KEY` | when using gemini | grounded search |
| `SCANNER_TOP_N` | no | ranking cut, default 50 |
