---
type: QA Checklist
title: Checks for target-market states
description: One check per requirement in the target-market-states capability, with the command that runs it and the condition that makes it fail.
---

# Checks for target-market states

Proves [target-market-states](/capabilities/target-market-states.md).

**Preconditions.** Python 3.12 with `requirements.txt` and `pytest`; no network, no model.
`mkt-zero-diff-on-real-leads` also needs the local copy of production on 5432.

Run with `bash scripts/gate.sh`, or one check with its `Automated:` path.

---

### Check: mkt-city-state-resolves

**Requirement:** A market written as a city and state admits that state
**Surface:** Market resolution
**Automated:** `tests/test_market_states.py::TestResolveMarketStates::test_codes_names_and_city_state_forms_resolve`

**Do**

Resolve "NC", "north carolina", "Concord, NC", "Charlotte, North Carolina", "DC".

**Expect**

NC, NC, NC, NC, DC — and nothing unresolved.

---

### Check: mkt-gate-admits-resolved

**Requirement:** A market written as a city and state admits that state
**Surface:** Gated scoring
**Automated:** `tests/test_market_states.py::TestTheGateUsesResolvedMarkets::test_a_city_state_home_market_admits_its_state`

**Do**

Score a producer-built NC lead under a gated config bound to home markets `["Concord, NC"]`, with no `state_aliases`.

**Expect**

The target-market gate passes. The same lead under the raw, unresolved list is the control and fails.

---

### Check: mkt-secondary-with-scope

**Requirement:** Secondary markets count when the org's scope includes them
**Surface:** Gated scoring
**Automated:** `tests/test_market_states.py::TestTheGateUsesResolvedMarkets::test_secondary_markets_count_with_home_secondary_scope`

**Do**

Home markets `["Utah"]`, secondary `["Atlanta, GA"]`, scope `HOME_SECONDARY`; score a GA lead.

**Expect**

The gate passes.

---

### Check: mkt-secondary-without-scope

**Requirement:** Secondary markets count when the org's scope includes them
**Surface:** Gated scoring
**Automated:** `tests/test_market_states.py::TestTheGateUsesResolvedMarkets::test_secondary_markets_do_not_count_with_home_scope`

**Do**

The same, with scope `HOME`.

**Expect**

The gate fails.

---

### Check: mkt-scope-matches-discovery

**Requirement:** Secondary markets count when the org's scope includes them
**Surface:** Runner / market resolution
**Automated:** `tests/test_market_states.py::TestTheGateUsesResolvedMarkets::test_scope_is_the_one_zip_discovery_uses`

**Do**

Run eight scope values (`HOME_SECONDARY` in any case and padding, `HOME`, `HOME_ONLY`, `NO_SECONDARY`, `HOME_ONLY_EXCL_SECONDARY`, …) through both the gate resolution and `zip_discovery.target_markets`.

**Expect**

They agree on every value: secondary markets count only for `HOME_SECONDARY`. Proven to fail with a substring check.

---

### Check: mkt-empty-home-admits-secondary

**Requirement:** Secondary markets count when the org's scope includes them
**Surface:** Runner / market resolution
**Automated:** `tests/test_market_states.py::TestTheGateUsesResolvedMarkets::test_empty_home_markets_with_secondary_scope_admits_secondary_states`

**Do**

Resolve an org with no home markets and secondary `["Atlanta, GA"]` under `HOME_SECONDARY`, and score a GA lead.

**Expect**

The gate passes — the org asked to prospect there; before this capability every lead failed.

---

### Check: mkt-unresolvable-not-guessed

**Requirement:** An entry that names no state is never guessed, and is reported
**Surface:** Market resolution
**Automated:** `tests/test_market_states.py::TestResolveMarketStates::test_regions_bare_cities_countries_and_sentences_are_not_guessed`

**Do**

Resolve "Southeast US", "Mountain West", "Atlanta", "Portland", "Canada", and a descriptive sentence.

**Expect**

No codes; every entry listed as unresolved.

---

### Check: mkt-unresolved-logged

**Requirement:** An entry that names no state is never guessed, and is reported
**Surface:** Runner
**Automated:** `tests/test_market_states.py::TestTheGateUsesResolvedMarkets::test_unresolved_entries_are_logged_once`

**Do**

Apply the resolution to a bound config whose markets include unresolvable entries, capturing the log.

**Expect**

Exactly one log line naming the unresolved entries.

---

### Check: mkt-literal-untouched

**Requirement:** A list written into the skill is used exactly as written
**Surface:** Runner
**Automated:** `tests/test_market_states.py::TestTheGateUsesResolvedMarkets::test_a_literal_allowed_list_is_used_as_authored`

**Do**

Apply the resolution to a config with a literal `allowed_states` list and an org with secondary markets and scope `HOME_SECONDARY`.

**Expect**

The list is byte-identical afterwards.

---

### Check: mkt-non-list-untouched

**Requirement:** A list written into the skill is used exactly as written
**Surface:** Runner / market resolution
**Automated:** `tests/test_market_states.py::TestTheGateUsesResolvedMarkets::test_a_dict_shaped_home_markets_is_left_exactly_as_before`

**Do**

Resolve a config whose home markets are a dict (`{state: [cities]}`), and separately None, a non-dict, a string, a non-list secondary and non-string entries.

**Expect**

`allowed_states` is identical before and after in every case, and an NC lead under the dict shape still passes. Proven to fail when the list guard is removed.

---

### Check: mkt-zero-diff-on-real-leads

**Requirement:** No current skill's results change
**Surface:** Gated scoring over stored production leads (local copy)
**Automated:** Manual

**Do**

For MYgroup and Matrix Frame — the gated skills bound to their org's markets — re-score every stored
gated lead with the scanner's own `gated_score`, once with the allowed list as resolved before this
capability and once after.

**Expect**

0 differences in the target-market verdict and 0 in the score, for every lead. Any difference fails
the check and stops the change for a decision.
