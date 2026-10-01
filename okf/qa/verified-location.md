---
type: QA Checklist
title: Checks for verified location
description: One check per requirement in the verified-location capability, with the command that runs it and the condition that makes it fail.
---

# Checks for verified location

Proves [verified-location](/capabilities/verified-location.md).

**Preconditions.** Python 3.12 with `requirements.txt` and `pytest`. No network and no model: a
stub verifier is injected. `vloc-e2e-stored` additionally needs the local stack with an
`aeo-backend` that includes `fca4d0e`.

Run with `bash scripts/gate.sh`, or one check with its `Automated:` path.

⚠️ Leads are built through the real producer (`_assemble_prospects`) so they carry `_internal`,
which is what the scorer reads. A hand-written dict without it would pass while the score still used
discovery's location.

---

### Check: vloc-kept-lead-relocated

**Requirement:** A kept lead is located by its verified address
**Surface:** Geography loop
**Automated:** `tests/test_verified_location.py::TestGeoLoopAppliesTheVerifiedAddress::test_a_kept_verified_lead_carries_the_verified_location`

**Do**

Run `discover_in_area` on a producer-built lead reported in Austin, TX, with a stub verifier answering Charlotte, North Carolina, 28202, in an area that includes it.

**Expect**

The kept lead's record AND its `_internal` read "Charlotte", "NC", "28202", and it carries a `verified_location` marker with the same values. Either copy still holding Austin fails the check.

---

### Check: vloc-scored-on-verified

**Requirement:** A kept lead is located by its verified address
**Surface:** Gated scoring
**Automated:** `tests/test_verified_location.py::TestScoredOnTheVerifiedAddress::test_the_target_market_gate_reads_the_verified_state`

**Do**

Build a lead whose discovery source returned only a combined headquarters field in TX, apply a verified NC location, and score it under a gated config allowing only NC.

**Expect**

The target-market gate passes. A lead left on discovery's location (no state) failing the gate is the control.

---

### Check: vloc-runner-attaches

**Requirement:** A kept lead is located by its verified address
**Surface:** Runner
**Automated:** `tests/test_verified_location.py::TestAttachVerifiedLocations::test_copies_the_marker_onto_scored_items_by_prospect_id`

**Do**

Attach verified-location markers from producer-built prospects (keyed `id`) onto scored items (keyed `prospect_id`).

**Expect**

Each scored item whose prospect was verified carries that prospect's marker, and only that one; the return value is the count.

---

### Check: vloc-wire-sends-verified

**Requirement:** A kept lead is located by its verified address
**Surface:** Event mapping
**Automated:** `tests/test_verified_location.py::TestTheWireCarriesOnlyVerifiedLocations::test_a_verified_lead_sends_its_location`

**Do**

Map a scored event whose item carries a `verified_location` marker.

**Expect**

The payload item has exactly that `city`, `state` and `zip_code`.

---

### Check: vloc-unverified-unchanged

**Requirement:** A lead without a usable verified address keeps discovery's location and sends none
**Surface:** Geography loop
**Automated:** `tests/test_verified_location.py::TestGeoLoopAppliesTheVerifiedAddress::test_an_unverifiable_lead_keeps_discovery_and_no_marker`

**Do**

Run the loop with a stub verifier that returns nothing for the lead.

**Expect**

The lead is kept with discovery's location and no `verified_location` marker.

---

### Check: vloc-non-us-unchanged

**Requirement:** A lead without a usable verified address keeps discovery's location and sends none
**Surface:** Geography loop
**Automated:** `tests/test_verified_location.py::TestGeoLoopAppliesTheVerifiedAddress::test_a_non_us_verified_state_keeps_discovery_and_no_marker`

**Do**

Run the loop with a stub verifier answering a place whose state is "Ontario" (in an area that would keep it).

**Expect**

If kept, the lead has discovery's location and no marker.

---

### Check: vloc-out-of-area-not-applied

**Requirement:** A kept lead is located by its verified address
**Surface:** Geography loop
**Automated:** `tests/test_verified_location.py::TestOnlyAnInAreaVerifiedLocationRelocates::test_a_lead_kept_on_discovery_zip_is_not_relocated_out_of_area`

**Do**

In an Austin area, keep a lead discovery placed at Austin, TX 78701 whose verifier answers Dallas, TX with no zip; separately (`test_a_lead_kept_on_its_address_zip_is_not_relocated`) one kept on its address ZIP whose verifier answers Houston, Texas.

**Expect**

Both kept, neither relocated, no marker. Proven to fail when the in-area condition is removed — that was the blocker this check exists for.

---

### Check: vloc-bare-state-not-applied

**Requirement:** A kept lead is located by its verified address
**Surface:** Geography loop
**Automated:** `tests/test_verified_location.py::TestOnlyAnInAreaVerifiedLocationRelocates::test_a_location_normalised_to_a_bare_state_does_not_relocate`

**Do**

Keep a lead whose verifier answers a city containing a NUL, a fullwidth zip and a valid state.

**Expect**

The lead keeps its discovery city, state, zip and street, with no marker. Proven to fail without the guard.

---

### Check: vloc-wire-through-runner-step

**Requirement:** A kept lead is located by its verified address
**Surface:** Loop → scorer → runner attach → event mapping
**Automated:** `tests/test_verified_location.py::TestTheWireCarriesOnlyVerifiedLocations::test_a_relocated_lead_reaches_the_wire_through_the_runner_step`

**Do**

Chain the real producer, geography loop (stub verifier), scorer, `attach_verified_locations` and `map_event`.

**Expect**

The relocated lead's payload carries the verified city, state and zip; the other carries no location and no marker. ⚠️ `main()` itself is not driven — the one call inside it is covered by `vloc-e2e-stored`.

---

### Check: vloc-relocations-logged

**Requirement:** A kept lead is located by its verified address
**Surface:** Geography loop
**Automated:** `tests/test_verified_location.py::TestOnlyAnInAreaVerifiedLocationRelocates::test_the_round_log_counts_relocations_and_state_changes`

**Do**

Run a round with one lead relocated within its state and one relocated to another state.

**Expect**

The round log reports the relocated count and the state-change count.

---

### Check: vloc-zip-ascii-only

**Requirement:** State names and codes are normalised the way the gateway accepts them
**Surface:** US state normalisation
**Automated:** `tests/test_verified_location.py::TestNormalisation::test_zip_is_ascii_digits_only`

**Do**

Normalise a fullwidth zip `２８２０２`.

**Expect**

None. Python's `\d` would accept it and the gateway would ignore the location, splitting score from storage.

---

### Check: vloc-city-no-control-chars

**Requirement:** State names and codes are normalised the way the gateway accepts them
**Surface:** US state normalisation
**Automated:** `tests/test_verified_location.py::TestNormalisation::test_city_refuses_control_characters`

**Do**

Normalise a city containing a NUL, and one containing `\x7f`.

**Expect**

None for both — a NUL fails the gateway's whole-batch UPDATE.

---

### Check: vloc-street-cleared-on-move

**Requirement:** The street address follows the place
**Surface:** Geography loop
**Automated:** `tests/test_verified_location.py::TestOnlyAnInAreaVerifiedLocationRelocates::test_relocating_to_another_place_clears_the_street`

**Do**

Relocate a lead with a street to a different city.

**Expect**

Record `address` None and `_internal` `location_address` empty.

---

### Check: vloc-street-kept-same-place

**Requirement:** The street address follows the place
**Surface:** Geography loop
**Automated:** `tests/test_verified_location.py::TestOnlyAnInAreaVerifiedLocationRelocates::test_relocating_within_the_same_place_keeps_the_street`

**Do**

Relocate a lead to the same city and state, differently cased.

**Expect**

Its street is kept.

---

### Check: vloc-legacy-region-follows

**Requirement:** Legacy skills are scored on the verified location too
**Surface:** Legacy scoring
**Automated:** `tests/test_verified_location.py::TestLegacyScoringFollowsTheVerifiedAddress::test_region_bonus_reads_the_verified_location`

**Do**

Score a relocated lead under a legacy config with a region bonus for the verified place.

**Expect**

The region bonus reflects the verified city and state.

---

### Check: vloc-warns-when-no-gate

**Requirement:** A run whose target-market gate cannot be evaluated says so
**Surface:** Runner
**Automated:** `tests/test_verified_location.py::TestTheBlindGateWarning::test_warns_when_a_gated_config_has_no_usable_target_market_gate`

**Do**

Check a gated config with no `gate.target_market` block, and one with an empty `allowed_states`.

**Expect**

A warning for each — every lead fails the gate closed in both.

---

### Check: vloc-half-is-not-blind

**Requirement:** A run whose target-market gate cannot be evaluated says so
**Surface:** Runner
**Automated:** `tests/test_verified_location.py::TestTheBlindGateWarning::test_exactly_half_without_a_state_is_not_blind`

**Do**

Check 4 scored leads, 2 without a state and at least one passing the gate.

**Expect**

No warning: the rule is strictly more than half.

---

### Check: vloc-warning-never-raises

**Requirement:** A run whose target-market gate cannot be evaluated says so
**Surface:** Runner
**Automated:** `tests/test_verified_location.py::TestTheBlindGateWarning::test_never_raises_on_malformed_scored_items`

**Do**

Check scored items with `gates: None`, missing `score_factors`, and non-dict items.

**Expect**

Returns without raising — the check runs before the scored callback and must never cost the run its results.

---

### Check: vloc-wire-silent-when-unverified

**Requirement:** A lead without a usable verified address keeps discovery's location and sends none
**Surface:** Event mapping
**Automated:** `tests/test_verified_location.py::TestTheWireCarriesOnlyVerifiedLocations::test_an_unverified_lead_sends_no_location`

**Do**

Map a scored item WITHOUT a marker that still has the engine's own `city`/`state` keys.

**Expect**

The payload item has no `city`, `state` or `zip_code` key. Any of them present fails the check.

---

### Check: vloc-normalise-state

**Requirement:** State names and codes are normalised the way the gateway accepts them
**Surface:** US state normalisation
**Automated:** `tests/test_verified_location.py::TestNormalisation::test_state_names_and_codes_normalise`

**Do**

Normalise "north carolina", " NC ", "nc", "District of Columbia", "Ontario", "", None.

**Expect**

"NC", "NC", "NC", "DC", None, None, None. The module's 51 codes equal the gateway's list.

---

### Check: vloc-normalise-zip-city

**Requirement:** State names and codes are normalised the way the gateway accepts them
**Surface:** US state normalisation
**Automated:** `tests/test_verified_location.py::TestNormalisation::test_zip_and_city_normalise`

**Do**

Normalise zips "28202", "28202-1234", "2820", "ABCDE", and a 256-character city.

**Expect**

Valid zips kept, the rest None; the over-long city None; a normal city trimmed.

---

### Check: vloc-rejected-unchanged

**Requirement:** A rejected lead is unchanged
**Surface:** Geography loop
**Automated:** `tests/test_verified_location.py::TestGeoLoopAppliesTheVerifiedAddress::test_a_rejected_lead_is_recorded_as_before`

**Do**

Run the loop with a stub verifier answering a place outside the area.

**Expect**

The lead is in the rejections with `verified_location` in its validation data, exactly as before this change.

---

### Check: vloc-warns-when-gate-blind

**Requirement:** A run whose target-market gate cannot be evaluated says so
**Surface:** Runner
**Automated:** `tests/test_verified_location.py::TestTheBlindGateWarning::test_warns_when_most_leads_have_no_state`

**Do**

Run the warning check on gated scored items under a config with a target-market gate, where most items have no state (and, separately, where none passes the gate).

**Expect**

One warning naming the run and the counts. No warning fails the check.

---

### Check: vloc-quiet-when-healthy

**Requirement:** A run whose target-market gate cannot be evaluated says so
**Surface:** Runner
**Automated:** `tests/test_verified_location.py::TestTheBlindGateWarning::test_is_quiet_when_the_gate_can_be_evaluated`

**Do**

Run the same check where most leads have a state and at least one passes the gate; and on a legacy (non-gated) run.

**Expect**

No warning in either case.

---

### Check: vloc-e2e-stored

**Requirement:** A kept lead is located by its verified address
**Surface:** Scanner geography → scoring → local gateway (with aeo-backend `fca4d0e`) → Postgres
**Automated:** Manual

**Do**

Seed a running test scan run on the local stack. Produce leads reported in Austin, TX, run the real
geography loop with a stub verifier (one lead verified elsewhere, one unverifiable), score them, and
post the prospects and scored events through the scanner's own sink to the local gateway. Read back.

**Expect**

The verified lead is stored with the verified location; the unverifiable lead keeps Austin; every
lead has its score. Remove the seeded rows afterwards.
