---
type: Capability
title: Score explanations for gated leads
description: What happens to the short "why this lead scored what it scored" paragraph the scanner writes for every lead scored by the gated model — which lead it lands on, when it is withheld, and how it reaches the gateway.
---

# Score explanations for gated leads

After a gated run scores its leads, the scanner asks the model for one short paragraph per
lead explaining its score, built only from facts already on the salesperson's screen, and
sends each accepted paragraph to the gateway with that lead's score.

**Seeded 2026-10-01** by `store-score-explanations`, the change that made these paragraphs
reach the gateway at all — until then every one was generated, billed and dropped. Only the
behaviour that change verified is described here. Whether a paragraph is *well written* is not
a scenario: the validator polices invented numbers, not prose, and a paragraph can pass it
while being about the wrong company (observed on Wheelhouse run `abfabc01`, where a client's
announcement was the lead's selected signal).

Related: [scoring](/lib/scoring.md) · [event mapping](/lib/event-mapping.md) · [runner](/lib/runner.md)

## Scenarios

#### Scenario: Every gated lead with an accepted paragraph carries it to the gateway

- GIVEN a gated run whose scored leads have the shape the scoring engine actually produces
- WHEN explanations are generated and the model's paragraph for a lead passes validation
- THEN that lead's scored item carries the paragraph
- AND the scored callback sent to the gateway includes it for that lead

**Checked by:** expl-producer-shape-attaches, expl-runner-step-attaches, expl-wire-carries-field, expl-e2e-stored

#### Scenario: A paragraph never lands on a lead it was not written for

- GIVEN two or more gated leads in one run
- WHEN their paragraphs come back
- THEN each lead carries only the paragraph written from its own facts
- AND a lead with no identifier is not sent to the model and receives no paragraph
- AND where a lead carries both identifiers, the engine's own (`prospect_id`) is the one used

**Checked by:** expl-no-cross-attach, expl-keyless-skipped, expl-key-precedence

#### Scenario: A rejected or failed paragraph leaves the lead without one

- GIVEN a lead whose paragraph fails validation, or whose model call fails
- WHEN the run completes
- THEN that lead carries no explanation, never a substitute string
- AND the other leads in the run keep theirs
- AND the failure event the scanner emits names that lead's own id (the event is logged by the
  scanner only; it has no gateway destination)

**Checked by:** expl-rejected-absent, expl-failure-isolated, expl-events-name-the-lead

#### Scenario: A paragraph that could harm the run or the reader is withheld

- GIVEN a paragraph that contains a control character, or a link, domain or email address
- WHEN it is validated
- THEN it is withheld and the lead carries no explanation
- AND an ordinary sentence with abbreviations such as "Inc." or "St. Louis" is not withheld

**Checked by:** expl-control-char-withheld, expl-link-withheld, expl-abbreviations-kept

#### Scenario: A lead scored by the legacy model gets no explanation call

- GIVEN a lead with no gated score breakdown
- WHEN explanations are generated
- THEN no model call is made for it and it carries no explanation

**Checked by:** expl-legacy-skipped, expl-runner-step-legacy-noop
