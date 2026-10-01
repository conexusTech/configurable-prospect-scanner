---
type: Capability
title: An org's markets become the states its target-market gate admits
description: How a gated skill bound to the org's own markets turns free-text market entries into US states, when secondary markets count, and what happens to an entry that names no state.
---

# An org's markets become the states its target-market gate admits

A gated skill can take its allowed states from the organisation's own markets instead of a list
written into the skill. Those markets are free text — "North Carolina", "Concord, NC",
"Southeast US", "Atlanta". The scanner turns each into a US state where the text itself names one,
and admits leads in those states.

**Seeded 2026-10-01** by `resolve-market-bindings-to-states`.

Related: [scoring](/lib/scoring.md) · [runner](/lib/runner.md) · [verified location](/capabilities/verified-location.md)

## Scenarios

#### Scenario: A market written as a city and state admits that state

- GIVEN a gated skill whose allowed states are the org's home markets
- AND a home market written "Concord, NC" or "Charlotte, North Carolina"
- WHEN a lead in NC is scored
- THEN it passes the target-market gate

**Checked by:** mkt-city-state-resolves, mkt-gate-admits-resolved

#### Scenario: Secondary markets count when the org's scope includes them

- GIVEN the org's scope is exactly home-and-secondary (`HOME_SECONDARY`, the same test zip discovery
  applies, so the gate and the search never disagree about scope)
- WHEN a lead is in a state named only in a secondary market
- THEN it passes the target-market gate
- AND WHEN the org's scope is anything else, the same lead does not
- AND an org with no home markets but secondary markets under that scope admits its secondary
  states, where before every lead failed the gate

**Checked by:** mkt-secondary-with-scope, mkt-secondary-without-scope, mkt-scope-matches-discovery, mkt-empty-home-admits-secondary

#### Scenario: An entry that names no state is never guessed, and is reported

- GIVEN market entries such as "Southeast US", "Atlanta", "Canada" or a descriptive sentence
- WHEN they are resolved
- THEN they add no state
- AND the run logs them once, so a market list that places nothing is visible

**Checked by:** mkt-unresolvable-not-guessed, mkt-unresolved-logged

#### Scenario: A list written into the skill is used exactly as written

- GIVEN a gated skill with a literal list of allowed states
- WHEN leads are scored
- THEN that list is used unchanged, and the org's secondary markets are never added to it
- AND markets stored in any shape other than a list are left exactly as before

**Checked by:** mkt-literal-untouched, mkt-non-list-untouched

#### Scenario: No current skill's results change

- GIVEN the stored gated leads of every skill bound to the org's markets
- WHEN they are scored before and after this capability
- THEN every target-market verdict and every score is identical

**Checked by:** mkt-zero-diff-on-real-leads
