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

## Events out: `aeo/event_mapping.py`

The mirror of the config mapping. Verified field-by-field against aeo-backend's
`scan-event.dto.ts`, and the good news is that the hard parts already agree: the
engine's prospect `id` is a `uuid5` of `(scan_run_id, normalised name)`, which is
exactly AEO's "skill-generated, stable on retry"; `discovery_data` is already an
object, matching migration 071; and `prospect_id` / `contact_name` / `score` /
`rank` / `score_factors` map 1:1 on the scored event.

Three things a pass-through gets wrong, all of them total failures rather than
partial ones:

- **`phase` / `phase_name` are per-ITEM in AEO, per-EVENT in the engine.** Unmapped,
  every prospect in the sweep fails validation.
- **AEO caps an event at 1000 items**; the engine emits one event per sweep. Over the
  cap the whole callback 400s and the sweep is lost, not truncated.
- **`pipeline_status` is a name collision** — see below.

Undeclared fields are folded into `scoring_payload` rather than dropped: the
engine's vertical-shaped extras (`denomination`, `campaign_goal`, `project_type`)
are real signal, they just are not columns.

### ⚠️ `pipeline_status` — do not connect these two fields

The engine emits `pipeline_status` from `calculate_pipeline()`: a **construction
project** stage inferred from timeline arithmetic ("in campaign", "breaking
ground"). AEO's `prospects.pipeline_status` is the **sales** pipeline workflow an
operator drives by hand via `PATCH /prospects/:id/pipeline-status`.

Same name, unrelated meanings. Mapping one onto the other silently overwrites
operator sales state with construction strings on every scan, with nothing erroring.
It stays inside `scoring_payload`, and a test asserts it never travels top-level.

This is the sixth name collision this feature has produced. The other five each cost
a defect.

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

## Running under conqrse-queue (the real path)

**The queue injects exactly one variable: `TASK_RECORD_ID`.** The business payload
stays in the portal's database and is delivered *by reference* — both repos say so
independently (`k8s-job.executor.ts`: "Only the reference is injected";
aeo-backend's enqueue site: "the opaque business envelope the scan task reads back by
reference"). So a container expecting `ORGANIZATION_ID`/`SCAN_RUN_ID` in its
environment gets **none of them**. `aeo/bootstrap.py` fetches
`GET {QUEUE_API_URL}/api/tasks/{TASK_RECORD_ID}` and maps the payload:

| Payload field | Runner value |
|---|---|
| `organization_id` *(not `org_id`)* | `ORGANIZATION_ID` |
| `tenant_id` | `TENANT_ID` |
| `scan_run_id` | `SCAN_RUN_ID` |
| `skill.slug` | `SKILL_SLUG` |
| `phases[]` *(absent = all)* | `PHASES` — this is R6's reduced-phase run |

In queue mode the **payload wins** over any ambient env: the task record is the
authoritative statement of what the run is, and a stale env var silently scanning the
wrong org is worse than a missing one.

Everything else — credentials, `AEO_BACKEND_URL`, `QUEUE_API_URL` — arrives through
the catalog entry's `envFrom` (Secrets/ConfigMaps), which the executor wires normally.
Only the business payload is by-reference.

### Verified locally, end to end

Docker Desktop's Kubernetes, the queue on `:3200`, the gateway on `:3000`:

```bash
docker build -t configurable-prospect-scanner:local .

kubectl create secret generic scanner-secrets \
  --from-literal=GEMINI_API_KEY=... --from-literal=CI_USER=... --from-literal=CI_PASSWORD=...
kubectl create configmap scanner-config \
  --from-literal=AEO_BACKEND_URL=http://host.docker.internal:3000 \
  --from-literal=QUEUE_API_URL=http://host.docker.internal:3200 \
  --from-literal=AV_SCANNER_PROVIDER=gemini

curl -X POST localhost:3200/api/catalog/entries -H 'Content-Type: application/json' -d '{
  "taskRef": "configurable-prospect-scanner",
  "image": "configurable-prospect-scanner:local",
  "resources": {"cpu":"500m","memory":"512Mi"},
  "retry": {"attempts":1,"backoff":"fixed","delaySec":10},
  "timeoutSec": 900, "namespace": "default",
  "envFrom": ["secret/scanner-secrets","configmap/scanner-config"]
}'

curl -X POST localhost:3200/api/tasks -H 'Content-Type: application/json' \
  -d '{"taskRef":"configurable-prospect-scanner","idempotencyKey":"<runId>","payload":{...}}'
```

Two local-only details that matter: **`host.docker.internal`** is how a pod reaches a
service on the host, and the catalog image must be resolvable without a registry —
the queue's `K8S_IMAGE_PULL_POLICY=IfNotPresent` plus Docker Desktop's shared image
store makes a locally-built tag work as-is.

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
| `SCANNER_TOP_N` | no | default 50. Fallback for `discovery.target_prospects` and `contacts.max_prospects` when the config omits them. **Not a ranking cut on this path** — the cut lives in `FileSink.close()` and the AEO path uses `AeoEventSink`, which has no `close()`. **Not** a fallback for `discovery.max_prospects` either: at the production value of `1` that would cap every run at one prospect. |
| `SCANNER_PHASE_CONCURRENCY` | no | width of the per-prospect phases, default **2**. The biggest single lever on run duration — location verification and validation are one grounded call per prospect each, so wall-clock ≈ `2 × prospects ÷ this × call latency` (~47 s). Raise only as far as the model key's rate limit allows; a 429's backoff costs more than the parallelism wins. |

### Bounding a run

Two knobs decide how long a scan takes, and they multiply:

- **`discovery.max_prospects`** (skill config) — a hard ceiling on prospects per run,
  cumulative across discovery rounds and applied *before* any prospect is persisted or
  verified. Absent means no ceiling. This is the only real cap; see
  `aeo/phases/prospect_budget.py` for why the three things that look like one are not.
- **`SCANNER_PHASE_CONCURRENCY`** (env) — how many of the per-prospect calls run at once.

Measured, on the run that motivated the ceiling: 262 discovered prospects at concurrency
2 needs roughly 3.5 hours of grounded calls. The same set at concurrency 6, capped to
100, is about 40 minutes.
