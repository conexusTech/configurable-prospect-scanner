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
| Contacts | up to three named decision-makers per top-ranked prospect, best match first; the first fills its contact fields — [capabilities/contact-search](/capabilities/contact-search.md) |

⚠️ **`README.md` states that validation and contacts do not run. That is false** — both
are implemented and executed. See [log.md](/log.md); the claim survives in three documents
and one dead import.

## Contacts: up to three people, none invented

The contact search asks for up to three named decision-makers per business (a skill's contact
settings may ask for one or two instead; three is the most the gateway keeps), six businesses to
a grounded call as before. Every rule the single-contact search had holds for each person: no
placeholder names, and no address that fails to parse or reads as a naming template such as
`first.last@…` — a refused address is kept only as evidence. A field longer than its limit is left
out, never shortened, because one over-long field makes the gateway refuse the whole scored event;
a value that is not text is treated as absent. When nobody can be named, a general office email,
phone or role still fills the contact fields, as the single-contact search allowed, but is never
listed as a person. A person with no real name is not
listed, and the same person twice is listed once. A business where nobody was found sends no
list at all, never an empty one, so a later run cannot erase people an earlier one found. An
answer in the older single-contact shape is still read, as one person, under the same rules.

## Geography is enforced, not requested

Putting a town in a query does not mean the results are in that town. Discovery therefore
runs as a **search, verify, re-search loop**: candidates are checked for being genuinely
in-area and the search is repeated as needed, bounded by a configured round limit and a
strictness setting. A lead that is kept is relocated to its verified address when that
address is itself in the target area, so the location it is scored on and stored under is the
verified one rather than whatever discovery reported.

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
