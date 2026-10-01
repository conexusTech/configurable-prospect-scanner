---
type: Capability
title: Leads are located by the address the scanner verified
description: Which location a lead is scored on and stored with, when the verified address replaces what discovery reported, what is sent to the gateway, and when a run warns that its target-market gate could not be evaluated.
---

# Leads are located by the address the scanner verified

The geography step looks up each candidate's own business address before anything is scored. A
lead it keeps is scored on that verified location and sent to the gateway with it, whatever field
name the skill's discovery sources happened to use.

**Seeded 2026-10-01** by `send-verified-location-with-scored-leads`. The gateway's handling of the
location it receives is `aeo-backend:/capabilities/scored-lead-location.md`.

Related: [runner](/lib/runner.md) · [event mapping](/lib/event-mapping.md) · [scoring](/lib/scoring.md)

## Scenarios

#### Scenario: A kept lead is located by its verified address

- GIVEN a scan whose area includes Charlotte, NC, and a lead discovery reported in Austin, TX, whose
  address the scanner verified as Charlotte, North Carolina 28202
- WHEN the lead is kept
- THEN it is scored on, and sent with, city "Charlotte", state "NC", zip "28202"
- AND a verified location that is itself outside the scan's area, or that normalises to a bare
  state with no usable city or zip, never replaces the location the lead already has
- AND the run log counts how many kept leads were relocated, and how many moved state

**Checked by:** vloc-kept-lead-relocated, vloc-out-of-area-not-applied, vloc-bare-state-not-applied, vloc-wire-through-runner-step, vloc-relocations-logged, vloc-scored-on-verified, vloc-runner-attaches, vloc-wire-sends-verified

#### Scenario: Location no longer depends on the discovery field's name

- GIVEN a skill whose discovery sources return only a combined headquarters field
- WHEN a lead's address is verified
- THEN the lead carries a state, and the target-market gate can admit it

**Checked by:** vloc-scored-on-verified

#### Scenario: A lead without a usable verified address keeps discovery's location and sends none

- GIVEN a lead whose verification failed, or returned a place that is not a US state
- WHEN it is kept
- THEN it keeps the location discovery reported
- AND no location is sent to the gateway for it

**Checked by:** vloc-unverified-unchanged, vloc-non-us-unchanged, vloc-wire-silent-when-unverified

#### Scenario: State names and codes are normalised the way the gateway accepts them

- GIVEN a verified state written as "north carolina", " NC ", "nc" or "District of Columbia"
- WHEN it is normalised
- THEN it becomes "NC" or "DC"
- AND a non-US region, a malformed or non-ASCII zip, or a city that is over-long or contains a
  control character becomes absent

**Checked by:** vloc-normalise-state, vloc-normalise-zip-city, vloc-zip-ascii-only, vloc-city-no-control-chars

#### Scenario: The street address follows the place

- GIVEN a kept lead with a street address from discovery
- WHEN it is relocated to a different city or state
- THEN its street address is cleared
- AND WHEN it is relocated within the same city and state, its street is kept

**Checked by:** vloc-street-cleared-on-move, vloc-street-kept-same-place

#### Scenario: Legacy skills are scored on the verified location too

- GIVEN a skill on the legacy additive model with a region bonus
- WHEN a kept lead is relocated
- THEN its region bonus is computed from the verified city and state, which can raise or lower it

**Checked by:** vloc-legacy-region-follows

#### Scenario: A rejected lead is unchanged

Which leads geography keeps or rejects is exactly as before this capability. A relocated lead's
later steps — signal validation, stage judgment and contact search — now read the verified
location, so their verdicts can differ from what discovery's location would have produced.

- GIVEN a lead whose verified address is outside the scan's target area
- WHEN it is rejected
- THEN it is recorded exactly as before, with the verified address in its validation data

**Checked by:** vloc-rejected-unchanged

#### Scenario: A run whose target-market gate cannot be evaluated says so

- GIVEN a gated skill
- WHEN more than half of the scored leads have no state, no scored lead passes the gate, or the
  skill is gated but configures no usable target-market gate
- THEN the run logs one warning naming the run and the counts
- AND a run where the gate can be evaluated logs no such warning, and a malformed scored item
  never makes the check fail the run

**Checked by:** vloc-warns-when-gate-blind, vloc-warns-when-no-gate, vloc-quiet-when-healthy, vloc-half-is-not-blind, vloc-warning-never-raises
