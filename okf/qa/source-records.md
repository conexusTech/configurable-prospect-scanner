---
type: QA Checklist
title: Checks for reading prospects from a gateway feed
description: One check per requirement in the source-records capability, with the test that runs it and the condition that makes it fail.
---

# Checks for reading prospects from a gateway feed

Proves [source-records](/capabilities/source-records.md).

**Preconditions.** Python 3.12 with `requirements.txt` and `pytest`. No network, no queue and no
model: whole runs go through `aeo.runner.main()` with `tests/runner_harness.py`, which answers the
context and the feed from fixtures, records every callback instead of posting it, and counts every
provider call by the phase that made it. The feed answer is
`tests/fixtures/source-records/health_inspections.json`, the same shape aeo-backend's
`scan-sources-read-gateway-feeds` pins. Run with `bash scripts/gate.sh`, or one check with its
`Automated:` path.

⚠️ The checks read what leaves for the gateway and what the provider was asked, not only a
phase's return value: a feed run that quietly searched as well would still return the right
prospects.

---

### Check: rows-in-territory-only

**Requirement:** Only rows in the organization's territory become prospects
**Surface:** Scanner run → prospects event
**Automated:** `tests/test_source_records.py::TestRowsInTerritoryOnly`, `tests/test_source_records.py::TestAssembly`, `tests/test_source_records.py::TestAFeedRunKeepsTheTerritory`

**Do**

Run the feed skill for an organization whose market is "Richmond, VA", with zip discovery
answering 23220 (Richmond) and 23111 (Mechanicsville), against the fixture's ten rows. Run it
again with zip discovery left out, and the sweep once under `geo_strictness: state`. Separately,
judge a row with no `zip_code`, the city "Glen Allen" (not in the area) and the address
"1 Main St, Glen Allen, VA 23220"; and rows with only `state: VA`, and with `state: VA` plus the
Norfolk ZIP 23510, under metro, under an unknown strictness and under `state`.

**Expect**

The six Richmond and Mechanicsville rows are the prospects; Harbor Grill (Norfolk, VA), Carolina
Smokehouse (Raleigh, NC), Food Truck 7 (state only) and Waterside Oyster Bar (state and a Norfolk
ZIP, no city) are not. Without zip discovery, Mechanicsville Pho is out too. Under `state`,
Norfolk, Food Truck 7 and Waterside are in and North Carolina is still out. The Glen Allen row is
in by the ZIP in its address, and out once that ZIP is 23059. The two state-only rows are out
under metro and under the unknown strictness, and in under `state`. The log counts 10 rows
answered, 6 placed and 4 not placed, as rows before held companies and the ceiling. Each
prospect's `discovery_data.by_source.inspections` equals its row; its city, state, ZIP, address
and industry are the row's; its id is the engine's `uuid5(scan_run_id:normalised name)`. The
organization named as the feed names the row ("River City Diner, 803 N Arthur Ashe Blvd") is
excluded. The provider makes no `geography` call. Any of
these differing fails.

---

### Check: no-search-spend

**Requirement:** A feed run pays for no search
**Surface:** Scanner run → provider calls
**Automated:** `tests/test_phases.py::TestAFeedRunPaysForNoSearch`

**Do**

Run the feed skill (no `validation` section: a feed skill authoring one is refused, see
`feed-validation-refused`) against the fixture, counting provider calls by phase, and read the
completed summary.

**Expect**

`discovery`, `geography`, `validation` and `enrichment` are each called 0 times. The phases
called are exactly `zip_discovery` (once, one market), `judgment` and `contacts`. The gateway is
asked for `inspections` once. The summary reports 6 prospects, 0 validated, 6 scored, and no
validations callback is sent. Any search phase called even once fails.

---

### Check: contacts-found-for-feed-prospects

**Requirement:** Contacts are found for feed prospects as for any prospect
**Surface:** Scanner run → scored event
**Automated:** `tests/test_phases.py::TestContactsAreFoundForFeedProspects`

**Do**

Run the feed skill with a stub contact search naming Avery (Owner), Blake and Casey at every
restaurant; then with `contacts.max_prospects: 2`.

**Expect**

All six scored items carry `contacts` of three, the first being "Avery <restaurant>", and
`contact_name`, `contact_title` and `contact_email` equal that first person. Six restaurants cost
one contact call. With the limit at 2, exactly two scored items carry people. A scored item with
fewer than three, or a column disagreeing with the first person, fails.

---

### Check: held-restaurant-not-repeated

**Requirement:** A restaurant the organization already holds is not added again
**Surface:** Scanner run → prospects event
**Automated:** `tests/test_source_records.py::TestAHeldRestaurantIsNotRepeated::test_a_held_restaurant_is_not_sent_again`, `tests/test_source_records.py::TestAHeldRestaurantIsNotRepeated::test_the_held_one_frees_its_place_under_the_ceiling`

**Do**

Run with "Fan District Cafe, 1700 W Main St" among the organization's known companies. Then hold the in-territory
restaurant whose prospect id sorts lowest (upper-cased, to prove the comparison is the gateway's
lower-and-trim), with `discovery.max_prospects: 5`.

**Expect**

Fan District Cafe is not sent. Under the ceiling of five, the five other in-territory restaurants
are all sent: the held one did not take a place. Four sent, or the held one sent, fails.

---

### Check: each-branch-a-prospect

**Requirement:** Each branch of a chain is its own prospect
**Surface:** Scanner run → prospects event
**Automated:** `tests/test_source_records.py::TestAssembly::test_the_name_as_given_is_the_company_and_the_plain_name_rides_along`, `tests/test_source_records.py::TestAHeldRestaurantIsNotRepeated::test_each_branch_of_a_chain_is_its_own_prospect`, `tests/test_source_records.py::TestNamesThatCollide`

That the gateway names every row "<name>, <street>" (else the city; else the plain name) is the
gateway's to prove (`scan-sources-read-gateway-feeds` in aeo-backend); this check takes the
fixture's names as given, with `TestTheContractFixture` pinning that the fixture follows the rule,
and proves what the scanner does with them. The collision handling is a safety net for two names
that still meet.

**Do**

Read the fixture's two Subways ("Subway, 123 W Broad St" and "Subway, 4500 Forest Hill Ave",
both `establishment_name_plain` "Subway"). Then run with "Subway, 123 W Broad St" held. Then
read an answer of two establishments, "McDonald's, 5 E Main St" (permit P1, 2026-11-01, the
longer name and address) and "MCDONALDS, 5 E MAIN ST" (permit P2, 2026-11-08), in both orders; then the same two spellings under one
permit. Then give P1 the dates "unknown", 20261101 (an integer), "11/05/2026", "" and none
against P2 on 2026-10-15, in both orders; P1 the timestamp "2026-11-09T08:30:00Z" against P2
on 2026-11-08; and both establishments unreadable dates. Then name the source by
`establishment_name_plain` with distinct `establishment_name`s; then drop both permits.

**Expect**

Both Subways are prospects under the names as given, each with `establishment_name_plain`
"Subway" in its stored row. With one held, the other is still sent. The two McDonald's
establishments give one prospect whose stored row is exactly P2's row and whose name and
address are P2's, in either order, with one WARNING naming "McDonald's" and "1 row(s)" and
calling it another establishment. One permit under two spellings gives one prospect and no
warning. Every unreadable date loses to P2's 2026-10-15; the timestamp wins as 2026-11-09; two
unreadable dates keep whichever came first. The warning names the dropped row by
`establishment_name_plain`, never by its `establishment_name`. Without permits it says the rows
may be the same establishment. One Subway where there were two, a prospect mixing P1's and P2's
fields, or an unreadable date winning, fails.

---

### Check: feed-validation-refused

**Requirement:** A feed skill that authors validation is refused
**Surface:** Config mapping → scanner run error event
**Automated:** `tests/test_source_records.py::TestTheMappingRefusesWhatV1CannotRun::test_a_feed_skill_authoring_validation_is_refused`, `tests/test_source_records.py::TestTheMappingRefusesWhatV1CannotRun::test_no_validation_or_an_empty_one_is_fine_and_a_searched_skill_may_validate`, `tests/test_source_records.py::TestTheMappingRefusesWhatV1CannotRun::test_build_tool_context_raises_the_validation_refusal`, `tests/test_source_records.py::TestWhatAFeedRunRefuses::test_a_feed_skill_authoring_validation_is_refused_before_any_call`

**Do**

Map a feed skill authoring `validation` as signals, as lanes only, and as a bare string. Map it
with no `validation` and with an empty one, and a searched skill with lanes. Run the feed skill
with signals and lanes authored.

**Expect**

Each authored form gives one problem naming `validation`; the absent and empty forms and the
searched skill give none; `build_tool_context` raises it. The run exits 1 with an error callback
naming `validation` and the gateway feed as its only callback, no provider call and no
records request. Any model call, or a run that proceeds, fails.

---

### Check: searched-skills-unchanged

**Requirement:** A searched skill runs exactly as before
**Surface:** Scanner run → every callback and every provider call
**Automated:** `tests/test_phases.py::TestASearchedSkillIsUnchanged`

**Do**

Run the harness's searched fixture (one source of two market-bound queries, validation and
contacts authored, one company held) and compare with
`tests/fixtures/searched-run/wire-callbacks.json`, recorded from the scanner at `95d1eea`, before
this change, with the same harness.

**Expect**

The exit code, every callback (zip codes, prospects, validations, scored, completed; items
compared sorted by id), the call count per phase and the sha256 of every prompt per phase equal
the recording. The gateway is never asked for records. Any difference fails; the recording is
never regenerated to make it pass.

---

### Check: no-territory-refused

**Requirement:** No territory, no run
**Surface:** Scanner run → error event
**Automated:** `tests/test_source_records.py::TestWhatAFeedRunRefuses::test_no_markets_is_refused_by_the_mapping_before_any_call`, `tests/test_source_records.py::TestWhatAFeedRunRefuses::test_a_territory_of_states_alone_is_refused_under_metro_before_the_feed_is_read`, `tests/test_source_records.py::TestWhatAFeedRunRefuses::test_markets_that_place_nothing_are_refused_before_the_feed_is_read`, `tests/test_source_records.py::TestWhatTheSweepRefuses::test_an_area_with_nothing_to_match_is_refused_before_the_gateway_is_asked`

Two refusals cover this. No markets at all is the mapping's own "No resolved markets", shared
with every skill and reached before any feed code. Markets that place nothing, and under metro
markets that give states but no city or ZIP, are the feed code's `source_records_no_territory`.

**Do**

Run the feed skill for an organization with an empty geography; then with the single market
"--" and a zip discovery that finds nothing; then with the single market "VA" and zip discovery
left out, first under the default strictness and then with `geo_strictness: state`.

**Expect**

Exit code 1. With no geography, the only callback is an error naming the missing markets, and no
provider call and no records request is made. With "--", the error begins
`source_records_no_territory:`, the gateway is never asked for the feed, and no prospect is sent.
With "VA" under the default, the error begins `source_records_no_territory:` and names cities,
ZIPs and `geo_strictness`, and the feed is never read; under `state` the run exits 0 having read
the feed once, with the North Carolina row still out. A run that reads the feed or sends a
prospect where it should have stopped, or a states-only metro run that completes, fails.

---

### Check: feed-refusals-named

**Requirement:** What a feed run cannot read stops it, named
**Surface:** Scanner run → error event; `fetch_source_records`; every gateway call
**Automated:** `tests/test_source_records.py::TestWhatAFeedRunRefuses`, `tests/test_source_records.py::TestWhatTheSweepRefuses`, `tests/test_source_records.py::TestTheFetch`, `tests/test_source_records.py::TestTheMappingRefusesWhatV1CannotRun`, `tests/test_gateway_redirects.py`

**Do**

Run a skill with a feed source and a searched source. Run the feed skill against a gateway that
answers 404 "Unknown feed 'health_inspections'", then 404 "Organization not found". Call the fetch
against a 400, a 404 whose reason is 5,000 characters, each of 301/302/303/307/308, an unreachable
gateway, a body cut short (`IncompleteRead`, `HTTPException`, a timeout), a body that is not JSON,
and, with the bound set to 64 bytes, a 64-byte and a 65-byte answer; and check the request it
sends. Through the runner's real opener with an in-process gateway answering each redirect
status, call the source-records fetch, the context fetch and the event POST. Feed the sweep an
answer for another feed (also one 5,000 characters long), one with no item list, items none of
which is an object, 501 and 500 rows, rows that lack the source's `name_field`, 5,000-character
`terms`, `since` and `total`, and `truncated` as "true", 1 and `True`.

**Expect**

The mixed skill stops at mapping: one error callback naming both sources, no provider call, no
records request. The 404s stop the run with `source_records_refused:`, the status, the gateway's
message and the source name, and send no prospect and make no judgment or contact call. The
fetch asks `GET /runtime/organizations/<org>/source-records?skill=<slug>&source=<name>` with the
same Basic header the context fetch sends, reads at most the bound plus one byte, and maps the
failures to `source_records_refused`, `source_records_redirected`, `source_records_unreachable`
and `source_records_malformed`; the long reason leaves an error under 500 characters. Through the
real opener each redirect is refused on all three calls: the gateway sees exactly one request, the
redirect's target none, and the context fetch and POST raise `GatewayRedirectRefused`. The sweep
refuses with `source_records_feed_mismatch` (under 500 characters for the long name),
`source_records_malformed` (no list, no object, 501 rows) and `source_records_unnamed` (naming
`establishment_name` and `permit_id` among the keys the rows carry); 500 rows are read. Long
gateway values leave every log line under 1,200 characters, and only `True` logs a truncation. A
5,000-character error body is cut to 200 characters in the records error, while a rejected event
POST still logs its first 800. An answer of one row and two items that are not records reads the
row and logs "2 item(s) that are not records". An empty answer is not a refusal. A run that goes on after any of these, or any request reaching a
redirect's target, fails.
