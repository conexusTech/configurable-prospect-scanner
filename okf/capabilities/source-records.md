---
type: Capability
title: A skill whose source names a gateway feed reads its prospects from the gateway's records
description: How a feed skill gets its prospects from records the gateway already holds instead of from a paid search, which of them it keeps, how they reach contacts, and what stops such a run.
---

# A skill whose source names a gateway feed reads its prospects from the gateway's records

A discovery source can name a feed the gateway already holds, such as health-inspection findings,
instead of being searched for. A run of that skill asks the gateway for the feed's rows, keeps only
those in the organization's territory, makes each one a prospect carrying its finding, and then
scores it and finds its contacts as for any prospect. It pays for no search to find or place them.

**Seeded 2026-11-19** by `sources-read-gateway-records` (Phase CRM-3, PRD J-7 and R-82). What the
gateway serves, and how a stored row becomes a prospect's finding, is
`aeo-backend:/capabilities/scan-source-feeds.md`, seeded by `scan-sources-read-gateway-feeds`.

Related: [phases](/jobs/phases.md) · [the gateway](/integrations/aeo-backend.md) ·
[contact search](/capabilities/contact-search.md)

## Scenarios

#### Scenario: Only rows in the organization's territory become prospects

- GIVEN an organization whose market is Richmond, VA, and a feed answer with restaurants in
  Richmond, one in a nearby ZIP that zip discovery returned for Richmond, one in Norfolk, one in
  North Carolina, a food truck with only its state, and a restaurant with only its state and a
  Norfolk ZIP
- WHEN a run of its feed skill reads the feed, under the default (metro) strictness
- THEN the Richmond restaurants and the one in the nearby ZIP become prospects
- AND the Norfolk, North Carolina, state-only and state-plus-Norfolk-ZIP rows do not: a row is in
  the territory only when its ZIP or its city is one of the territory's, and a matching state alone
  places nothing
- AND a ZIP found only inside the row's address still places it
- AND under state strictness every row in a territory state is in, including the state-only two
- AND each prospect carries its row's non-empty values, finding and all, with its name, city,
  state, ZIP, address and inspection type in the prospect's own fields
- AND no model is asked where any of them is

**Checked by:** rows-in-territory-only

#### Scenario: A feed run pays for no search

- GIVEN a feed skill (which authors no validation: one that does is refused, below)
- WHEN it runs
- THEN no discovery search, no address verification and no validation call is made
- AND the only model calls are the territory's ZIP lookup, the stage judgment and the contact search
- AND the gateway is asked once for each feed source

**Checked by:** no-search-spend

#### Scenario: Contacts are found for feed prospects as for any prospect

- GIVEN a feed run whose contact search names three people at each restaurant
- WHEN its scored prospects are sent
- THEN each carries the three people, best match first, and the first fills its contact fields
- AND six restaurants cost one contact call
- AND the skill's contact limit still decides how many restaurants are searched

**Checked by:** contacts-found-for-feed-prospects

#### Scenario: A restaurant the organization already holds is not added again

- GIVEN an organization that already holds one of the restaurants in the feed's answer
- WHEN its feed skill runs
- THEN that restaurant is not sent as a new prospect
- AND it is dropped before the run's prospect limit, so it never takes the place of a new one

**Checked by:** held-restaurant-not-repeated

#### Scenario: Each branch of a chain is its own prospect

- GIVEN a feed answer in which the gateway names every row "<name>, <street>" (else the city; else
  the plain name alone), with two branches of one chain ("Subway, 123 W Broad St", "Subway, 4500
  Forest Hill Ave")
- WHEN the run reads it
- THEN both become prospects, each under the name the gateway gave it, with the bare name kept
  beside its finding
- AND holding one branch does not hide the other
- AND, as a safety net, two different establishments whose names still meet once case and
  punctuation are set aside ("McDonald's, 5 E Main St" and "MCDONALDS, 5 E MAIN ST") are never
  merged into one prospect: the one with the newest finding is kept, whole, and the other is left
  out with a warning in the run log that names it by the source's name field
- AND "newest" compares the findings' dates as dates: a date that is not an ISO date string
  ("unknown", 20261101, "11/05/2026") counts as the oldest, and two such keep the first
- AND when either row carries no permit, the warning says the two may be the same establishment
- AND one establishment answered under two such spellings is still one prospect

**Checked by:** each-branch-a-prospect

#### Scenario: A feed skill that authors validation is refused

- GIVEN a skill whose sources read a gateway feed and which also authors validation, signals or
  lanes
- WHEN it runs
- THEN it stops before any model call and before the feed is read, with an error naming
  `validation` and the gateway feed
- AND a searched skill authoring validation runs as before

**Checked by:** feed-validation-refused

#### Scenario: A searched skill runs exactly as before

- GIVEN a skill whose sources are searched, as every skill was before feeds existed
- WHEN it runs
- THEN it makes the same model calls with the same prompts, and sends the gateway the same
  callbacks, as it did before this change
- AND it never asks the gateway for records

**Checked by:** searched-skills-unchanged

#### Scenario: No territory, no run

- GIVEN a feed skill connected to an organization with no markets, or with markets that name no
  city, state or ZIP, or, under the default metro strictness, with markets that name only states
  ("VA") and no zip discovery to add ZIPs
- WHEN it runs
- THEN it stops with an error naming the missing territory; for states alone, the error says to
  write the markets as cities or ZIPs, or to set the skill's strictness to `state`
- AND it sends nothing else, and never asks the gateway for the feed
- AND the same states-only territory under `state` strictness reads the feed

**Checked by:** no-territory-refused

#### Scenario: What a feed run cannot read stops it, named

- GIVEN a feed skill that also has a searched source, or a gateway that refuses the request (an
  unknown feed, an unknown skill or organization), answers with a redirect, cannot be reached or
  breaks off mid-answer, answers for another feed, answers more than 500 rows, more than 4 MB, no
  item list, items none of which is a record, or rows none of which carries the source's name field
- WHEN it runs
- THEN it stops with an error whose leading reason says which, carrying the gateway's own words
  (cut to 200 characters) when it gave any, and naming the fields the rows do carry when the
  name field is missing
- AND items in an answer that are not records are skipped and counted in the run log
- AND a redirect is never followed, so the scanner's credentials never go to another address, on
  this call or on the context fetch and event posts
- AND a skill mixing the two kinds of source makes no model call at all

**Checked by:** feed-refusals-named
