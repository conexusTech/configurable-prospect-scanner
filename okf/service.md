---
type: Service
title: configurable-prospect-scanner
description: The one config-driven prospect discovery and scoring engine behind every skill the AEO platform builds — many skills, one runtime, behaviour supplied entirely as config.
resource: configurable-prospect-scanner
tags: [scanner, prospects, pipeline, queue-task, config-driven]
timestamp: 2026-09-02
---

# configurable-prospect-scanner

One container that finds and scores prospects for **any** vertical. Per-skill behaviour
arrives as authored config on the org's runtime context — never as a separate image. See
[business/one-runtime-many-skills](/business/one-runtime-many-skills.md) for why that
decision is load-bearing rather than a packaging preference.

## What a run is

The queue starts the container with a **task record id and nothing else**. From there:

1. Fetch the task payload by reference from [conqrse-queue](/integrations/conqrse-queue.md) —
   organization, tenant, scan run, skill slug, and optionally a phase subset.
2. Fetch the org runtime context, scoped to that skill, from
   [the gateway](/integrations/aeo-backend.md).
3. Map that context into the engine's flat shape — [lib/config-mapping](/lib/config-mapping.md),
   which is the actual point of this repo.
4. Run the phases — [jobs/phases](/jobs/phases.md).
5. Translate every result into a scan event and POST it to the gateway —
   [lib/event-mapping](/lib/event-mapping.md).

**It is stateless and writes nothing directly.** No database client, no object store, no
files in the queue path. Everything durable happens through the gateway's event API.

## The shape of it

| | |
|---|---|
| Runtime | Python 3.12 slim container, non-root, one process |
| Entry point | `python -m aeo.runner` — [lib/runner](/lib/runner.md) |
| Started by | [conqrse-queue](/integrations/conqrse-queue.md), as an isolated k8s Job from a single catalog entry |
| Providers | grounded search and judgment via [an LLM provider](/integrations/llm-providers.md), or an offline mock |

**Never invoke the vendored engine directly in production.** It reads a different, generic
context shape, so it skips the AEO mapping and scans on defaults — quietly. See
[business/vendored-engine](/business/vendored-engine.md).

## Where to go next

- The pipeline: [jobs/phases](/jobs/phases.md)
- Why a missing config section is refused rather than defaulted: [lib/config-mapping](/lib/config-mapping.md)
- How one config serves many orgs: [business/context-references](/business/context-references.md)
- Spending nothing while tuning a config: [playbooks/offline-evaluation](/playbooks/offline-evaluation.md)
- What must be true: [capabilities/](/capabilities/index.md), proven by [qa/](/qa/index.md)
