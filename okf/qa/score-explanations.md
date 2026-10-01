---
type: QA Checklist
title: Checks for score explanations
description: One check per requirement in the score-explanations capability, with the command that runs it and the condition that makes it fail.
---

# Checks for score explanations

Proves [score-explanations](/capabilities/score-explanations.md).

**Preconditions.** Python 3.12 with `requirements.txt` and `pytest`. The automated checks need
no network and no model; they inject a stub. `expl-e2e-stored` additionally needs the local
stack from the workspace playbook (gateway on `localhost:3000`, Postgres forwarded on 5432).

Run everything automated with `bash scripts/gate.sh`, or one check with its `Automated:` path.

⚠️ The decisive checks here build their leads **through the real producer**, not a hand-written
dict. The defect this list was written for survived because every existing test used a
fixture keyed `id` while the engine emits `prospect_id`; a check built from a fixture would
pass against that defect again.

---

### Check: expl-producer-shape-attaches

**Requirement:** Every gated lead with an accepted paragraph carries it to the gateway
**Surface:** Explanation phase + runner attachment
**Automated:** `tests/test_score_explanation.py::TestAttachesToTheProducerShape::test_every_lead_with_an_accepted_paragraph_carries_it`

**Do**

Produce gated scored leads through the assembler and the scorer, explain them with a stub model
that returns a valid paragraph, and attach the results the way the runner does.

**Expect**

Every lead carries a non-empty `score_explanation`. Any lead without one fails the check.

---

### Check: expl-runner-step-attaches

**Requirement:** Every gated lead with an accepted paragraph carries it to the gateway
**Surface:** Runner
**Automated:** `tests/test_score_explanation.py::TestAttachesToTheProducerShape::test_the_runner_step_attaches_to_producer_output`

**Do**

Call the runner's `explain_scored` step with `gated=True` on producer-shaped leads and a stub model.

**Expect**

Every lead carries its own paragraph and the step returns that count. Proven to fail when the step
looks leads up by `id` (it then logs "explained 0/3").

---

### Check: expl-wire-carries-field

**Requirement:** Every gated lead with an accepted paragraph carries it to the gateway
**Surface:** Event mapping
**Automated:** `tests/test_score_explanation.py::TestAttachesToTheProducerShape::test_the_scored_callback_carries_the_paragraph`

**Do**

Map a scored event whose items carry `score_explanation` (and one that does not) to the
gateway payload.

**Expect**

The payload item for the explained lead has the identical text; the unexplained lead's item has
no `score_explanation` key. A missing, altered or empty-string value fails the check.

---

### Check: expl-e2e-stored

**Requirement:** Every gated lead with an accepted paragraph carries it to the gateway
**Surface:** Scanner → local gateway callback → Postgres
**Automated:** Manual

**Do**

Seed a test scan run on the local stack. Post its prospects, then its scored leads — produced,
scored and explained by the scanner's own code with a stub model, mapped by `map_event` — to the
gateway's real `POST /runtime/scans/:runId/events`. Read `prospects.score_explanation` back.

**Expect**

Both callbacks return 2xx, and every seeded lead's `score_explanation` equals the paragraph the
stub produced for it. A 4xx on the scored callback, or any lead stored empty, fails the check.

---

### Check: expl-no-cross-attach

**Requirement:** A paragraph never lands on a lead it was not written for
**Surface:** Explanation phase + runner attachment
**Automated:** `tests/test_score_explanation.py::TestAttachesToTheProducerShape::test_two_leads_never_swap_paragraphs`

**Do**

Explain two producer-shaped leads, each with a distinct observed signal, using a stub that
echoes the observed signal from its prompt into the paragraph, then attach.

**Expect**

Each lead's paragraph contains its own observed signal and not the other lead's.

---

### Check: expl-keyless-skipped

**Requirement:** A paragraph never lands on a lead it was not written for
**Surface:** Explanation phase
**Automated:** `tests/test_score_explanation.py::TestAttachesToTheProducerShape::test_a_lead_with_no_identifier_is_not_explained`

**Do**

Explain a gated lead carrying neither `prospect_id` nor `id` beside one that has an id, with a
stub that counts calls.

**Expect**

The stub is called once, for the identified lead only, and the result has no `"None"` key.

---

### Check: expl-rejected-absent

**Requirement:** A rejected or failed paragraph leaves the lead without one
**Surface:** Explanation phase
**Automated:** `tests/test_score_explanation.py::TestThePass::test_a_rejected_explanation_is_ABSENT_not_a_fallback_string`

**Do**

Explain a lead with a stub returning a paragraph that states a number not in its inputs.

**Expect**

The lead has no entry in the result.

---

### Check: expl-failure-isolated

**Requirement:** A rejected or failed paragraph leaves the lead without one
**Surface:** Explanation phase
**Automated:** `tests/test_score_explanation.py::TestAttachesToTheProducerShape::test_the_scored_callback_carries_the_paragraph`

**Do**

Explain producer-shaped leads with a stub that raises for one of them, then map the scored event.

**Expect**

The other leads keep their paragraphs in the mapped payload; the failed lead has none; nothing raises.

---

### Check: expl-events-name-the-lead

**Requirement:** A rejected or failed paragraph leaves the lead without one
**Surface:** Explanation phase events
**Automated:** `tests/test_score_explanation.py::TestAttachesToTheProducerShape::test_failure_and_rejection_events_name_the_real_lead`

**Do**

Explain producer-shaped leads with a stub that raises for one and returns an invalid paragraph
for another, capturing emitted events.

**Expect**

Each `score_explanation_failed` / `score_explanation_rejected` event's `prospect_id` equals that
lead's real id. A `null` id fails the check.

---

### Check: expl-legacy-skipped

**Requirement:** A lead scored by the legacy model gets no explanation call
**Surface:** Explanation phase
**Automated:** `tests/test_score_explanation.py::TestThePass::test_a_legacy_prospect_is_skipped_entirely`

**Do**

Explain a lead whose `score_factors` has no `gated` breakdown, with a stub that counts calls.

**Expect**

Zero calls and an empty result.

---

### Check: expl-key-precedence

**Requirement:** A paragraph never lands on a lead it was not written for
**Surface:** Explanation phase
**Automated:** `tests/test_score_explanation.py::TestAttachesToTheProducerShape::test_prospect_id_wins_when_both_keys_are_present`

**Do**

Explain and attach a lead carrying `prospect_id="a"` and `id="b"`.

**Expect**

The paragraph is filed under "a" and attached. Proven to fail when the key order is swapped.

---

### Check: expl-control-char-withheld

**Requirement:** A paragraph that could harm the run or the reader is withheld
**Surface:** Validator
**Automated:** `tests/test_score_explanation.py::TestTheValidator::test_rejects_a_control_character`

**Do**

Validate a paragraph containing a NUL character.

**Expect**

It is rejected as containing a control character.

---

### Check: expl-link-withheld

**Requirement:** A paragraph that could harm the run or the reader is withheld
**Surface:** Validator
**Automated:** `tests/test_score_explanation.py::TestTheValidator::test_rejects_a_link_or_contact_detail`

**Do**

Validate paragraphs containing an http URL, a `www.` address, a bare domain and an email address.

**Expect**

Each is rejected as containing a link or contact detail.

---

### Check: expl-abbreviations-kept

**Requirement:** A paragraph that could harm the run or the reader is withheld
**Surface:** Validator
**Automated:** `tests/test_score_explanation.py::TestTheValidator::test_an_ordinary_sentence_with_abbreviations_is_not_a_link`

**Do**

Validate an ordinary sentence containing "Inc." and "St. Louis".

**Expect**

It is accepted. A rejection fails the check — the link rule must not cost ordinary prose.

---

### Check: expl-runner-step-legacy-noop

**Requirement:** A lead scored by the legacy model gets no explanation call
**Surface:** Runner
**Automated:** `tests/test_score_explanation.py::TestAttachesToTheProducerShape::test_the_runner_step_does_nothing_for_a_legacy_run`

**Do**

Call `explain_scored` with `gated=False` and a stub that counts calls.

**Expect**

Zero calls, no lead gains `score_explanation`, and the step returns 0.
