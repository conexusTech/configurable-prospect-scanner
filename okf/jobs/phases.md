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
| Discovery | deduplicated candidate companies, capped by the run's prospect ceiling and verified as genuinely in-area; for a skill whose sources name a gateway feed, the feed's rows in the territory instead, with no search — see below |
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

## A feed skill reads its prospects instead of searching

A discovery source may name a feed the gateway already holds — health-inspection findings first
— instead of being searched. Its `queries` are then the feed's terms, which the gateway reads from
the stored skill, so nothing about them is sent or searched. The run asks the gateway once per
feed source for the rows ([the gateway](/integrations/aeo-backend.md)) and keeps only the rows its
own address places in the organization's territory: the same area, built from its markets and zip
discovery's ZIPs, that a search is held to. **Under the default `metro` strictness the placement
must be positive** — the row's ZIP is one of the area's ZIPs, or its city one of its cities (a ZIP
inside the address counts). A matching state alone places nothing, though the classifier a search
uses would admit it: a searched candidate is verified afterwards, a record's address is all there
is. Under `geo_strictness: state` the classifier's answer stands. Each kept row becomes a prospect
through the engine's own assembly, so ids and the exclusion of the organization's own listing are
what a search would give. The row's non-empty values travel on the prospect, which is how the
gateway shows it as the prospect's finding. Companies the organization already holds are dropped,
then the prospect ceiling applies, exactly as for a search.

Nothing is verified by a model, because the address is the state's record, and **a feed skill
cannot author `validation`** (refused when its config is mapped: it would be a paid call per
prospect). A feed run therefore pays only for zip discovery, the stage judgment and contacts, which
run unchanged. **The gateway names every row "<name>, <street>"** (the address's first line;
else the city; else the plain name alone), whether or not the name is shared, so every branch of a
chain is its own prospect and a restaurant keeps one name in every answer; the company name is that
name as given. As a safety net, two different establishments whose names still meet under the
engine's name normalisation ("McDonald's, 5 E Main St", "MCDONALDS, 5 E MAIN ST") are never
merged: the newest finding, by date (a date that is not an ISO date string counts as oldest), is
kept and the run logs a warning. ⚠️ Because the name carries the street, an organization's own
listing is excluded, and a company it already holds is skipped, only when it holds that same
"<name>, <street>" form.

A skill mixing feed and searched sources is refused when its config is mapped. A run with no
territory to hold the rows to — including, under metro, markets that give only states and no
city or ZIP — a gateway that refuses, redirects or cannot be reached, or an answer this scanner
cannot read (more than 500 rows or 4 MB among them) stops with a named error (`source_records_*`)
rather than reporting an empty week. See
[capabilities/source-records](/capabilities/source-records.md).

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
