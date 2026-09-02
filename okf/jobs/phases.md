---
type: Background Job
title: The scan pipeline
description: The phases a run executes in order, what each produces, and the two mechanisms that keep discovery honest about geography and cost.
resource: aeo/phases
tags: [pipeline, phases, discovery, scoring]
timestamp: 2026-09-02
---

# The scan pipeline

In order:

| Phase | Produces |
|---|---|
| ZIP discovery | validated ZIP rows per target market; may widen the org's markets when the config asks it to |
| Discovery | deduplicated candidate companies, capped by the run's prospect ceiling and verified as genuinely in-area |
| Validation | a per-prospect qualification verdict: qualified or not, signals found, disqualifiers hit, and the reasoning |
| Enrichment lanes | additional authored fact passes, each differently shaped, stored per lane |
| AI stage judgment | the sales-pipeline stage and its reasoning, replacing the engine's date-only ladder |
| Scoring | the ranked, scored list — [lib/scoring](/lib/scoring.md) |
| Contacts | decision-maker details merged onto the top-ranked prospects |

⚠️ **`README.md` states that validation and contacts do not run. That is false** — both
are implemented and executed. See [log.md](/log.md); the claim survives in three documents
and one dead import.

## Geography is enforced, not requested

Putting a town in a query does not mean the results are in that town. Discovery therefore
runs as a **search, verify, re-search loop**: candidates are checked for being genuinely
in-area and the search is repeated as needed, bounded by a configured round limit and a
strictness setting.

Separately, market names are substituted into each source's query templates. That
substitution is recorded in the code as the actual root cause of geography drift — the
queries had been running without the market bound into them.

## Cost is bounded in two places

A **cumulative prospect ceiling** across the run, and a **bounded concurrent fan-out** for
every per-prospect phase, with an enforced timeout. Per-prospect phases are the expensive
ones — one provider call per prospect per phase — so an unbounded fan-out is both a cost
and a rate-limit problem.

Custom per-prospect modules exist behind a review gate and are **off by default**; their
output is rendered, never scored, so a plug-in cannot move a ranking.
