---
type: External Integration
title: LLM providers — grounded search and judgment
description: Grounded search, validation, enrichment and stage judgment come from a provider chosen by environment, with a deterministic offline mock as a first-class option.
resource: av_lead_scanner.py
tags: [llm, grounded-search, provider, mock]
timestamp: 2026-09-02
---

# LLM providers

The providers do the finding and most of the judging: discovery search, validation,
enrichment lanes, stage judgment, and score explanation.

Two real providers are implemented and selected by environment, one of them the default,
with a separate judgment model tier configured alongside the main one. **Which model runs
is a deployment concern and is not config-authorable** — see
[lib/config-mapping](/lib/config-mapping.md).

## The mock provider is not a test fixture

It is a deterministic, offline, no-network provider, selectable by one environment
variable, and it is the reason a full run can be exercised for free. Every per-prospect
phase costs one provider call per prospect, so the difference between "runnable offline"
and "runnable only against a paid API" decides whether anybody can develop against this
repo at all.

There is a matching `today` override so time-dependent pipeline logic is deterministic
under test rather than depending on the day the suite runs.

Costs are bounded structurally, not by trust — the prospect ceiling and the bounded
concurrent fan-out in [jobs/phases](/jobs/phases.md).
