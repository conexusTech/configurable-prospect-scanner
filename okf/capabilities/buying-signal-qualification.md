---
type: Capability
title: Only a recent, real buying signal qualifies a lead — and it is the timing the lead shows
description: How a gated skill can require that a lead be qualified by one of its own named buying signals inside its window, and how the timing a lead displays is the signal that qualified it.
---

# Only a recent, real buying signal qualifies a lead — and it is the timing the lead shows

A gated skill can require that the signal opening its buying-window gate be one of the buying
signals the skill names, dated inside the skill's window. The research step is told those signals
and that window, the stage judge sees the same signals the score reads, and a qualified lead
displays the signal that qualified it.

**Seeded 2026-10-06** by `qualify-only-on-real-buying-signals`, after a customer reported prospects
"with super late timing" and "dud prospects ranked in the upper 80s to 90s".

Related: [scoring](/lib/scoring.md) · [runner](/lib/runner.md) · [target-market states](/capabilities/target-market-states.md)

## Scenarios

#### Scenario: A signal the skill does not name cannot qualify a lead

- GIVEN a gated skill that requires a configured buying signal
- AND a lead in its target market, at an in-window stage, whose only fresh signal is an award
- WHEN the lead is scored
- THEN it is not qualified, and scores no higher than the target-market-only ceiling
- AND the same lead under the same skill without the requirement is qualified

**Checked by:** bsq-unnamed-signal-refused, bsq-without-requirement-control

#### Scenario: A named signal inside the window qualifies the lead, and older ones do not

- GIVEN a gated skill that requires a configured buying signal, with a six-month window
- WHEN a lead's only named signal is eight months old
- THEN it is not qualified
- AND WHEN the same signal falls inside the window, it is

**Checked by:** bsq-named-signal-admits, bsq-stale-named-signal-refused

#### Scenario: The signal a lead reports is one that could have qualified it

- GIVEN a lead carrying a fresher unnamed signal beside an older named one
- WHEN it is scored under the requirement
- THEN its selected signal is the named one
- AND selection uses the window the gate used, never a longer default

**Checked by:** bsq-selected-is-admitting, bsq-selection-uses-gate-window

#### Scenario: A qualified lead displays the signal that qualified it

- GIVEN a gated lead whose selected signal is fresh and dated
- WHEN the run reports it
- THEN its displayed timing event and date are that signal's
- AND a lead not qualified by a fresh signal keeps the stage judge's event
- AND a signal with no date never blanks the timing

**Checked by:** bsq-timing-is-scoring-signal, bsq-gated-out-keeps-judge-timing

#### Scenario: The stage judge is shown the signals the score reads

- GIVEN a gated skill whose signals come from an enrichment lane
- WHEN prospects are judged for a pipeline stage
- THEN each prospect's dated lane signals are among the events the judge sees

**Checked by:** bsq-judge-sees-lane-signals

#### Scenario: The research step is told the skill's buying signals and window

- GIVEN a gated skill that requires a configured buying signal
- WHEN its signal lane runs
- THEN the instruction lists exactly the skill's signal labels with their definitions, the earliest
  acceptable date, and what does not count
- AND no other lane receives that instruction, and the authored config is not altered

**Checked by:** bsq-lane-told-signals-and-window

#### Scenario: A skill that has not opted in is unchanged

- GIVEN a gated skill without the requirement
- WHEN its signal lane runs
- THEN the lanes it receives are exactly the authored ones

**Checked by:** bsq-opt-in-untouched
