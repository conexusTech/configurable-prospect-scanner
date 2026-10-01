---
type: Service Module
title: The runner
description: The only thing the container executes — bootstrap, context fetch, mapping, the phase sequence, and event posting.
resource: aeo/runner.py
tags: [entrypoint, orchestration, pipeline]
timestamp: 2026-09-02
---

# The runner

`aeo/runner.py` is the container's entry point and the orchestrator for the whole run.
It resolves the task, fetches and maps the context, runs the phases in order, and posts
every result as an event.

## Two details that look like mistakes and are not

**It exits through `os._exit`, not `sys.exit`.** A normal exit runs interpreter shutdown,
which waits on the thread pool — and an abandoned provider call can hold a worker thread
long past the point where the run is finished. The process would hang after doing all its
work. The hard exit is deliberate; the exit code is still set correctly.

**Completeness fields are pinned from org data**, not from a hardcoded vertical default.
Where the config leaves a gap that the organization can answer — its markets, its sales
cycle — the org's own values fill it. A hardcoded default here would be a wrong answer
that looks like a configured one.

## Where a lead is located

Right after scoring, the runner carries each kept lead's **verified location** (the address
the geography step established — see [jobs/phases](/jobs/phases.md)) onto its scored item, and
logs how many it carried. Only leads that were verified AND relocated get one; the rest send no location at all.

A gated run whose target-market gate cannot be evaluated logs one
`WARNING target-market gate:` line naming the run: no gate configured (no allowed states),
more than half the scored leads have no state, or not one lead passed the gate. Such a gate
fails every lead closed while the run reports normally, so the log line is the only place it
shows. Non-gated runs never warn.

## Phase selection

A run may be given a subset of phases in its task payload rather than the full sequence,
which is what makes a targeted re-run possible without repeating the expensive discovery
work. The full ordering is in [jobs/phases](/jobs/phases.md).

## Test runs are cheaper by construction

A test run lowers the prospect ceiling. The flag is compared **exactly** against the
string `true`, so a truthy-looking value does not accidentally turn a real run into a
draft one, or the reverse.
