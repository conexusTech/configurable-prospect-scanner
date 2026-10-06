---
type: QA Checklist
title: Checks for buying-signal qualification
description: One check per requirement in the buying-signal-qualification capability, with the command that runs it and the condition that makes it fail.
---

# Checks for buying-signal qualification

Proves [buying-signal-qualification](/capabilities/buying-signal-qualification.md).

**Preconditions.** Python 3.12 with `requirements.txt` and `pytest`; no network, no model.

Run with `bash scripts/gate.sh`, or one check with its `Automated:` path.

---

### Check: bsq-unnamed-signal-refused

**Requirement:** A signal the skill does not name cannot qualify a lead
**Surface:** Gated scoring
**Automated:** `tests/test_gated_score.py::TestOnlyAConfiguredSignalOpensTheGate::test_an_unconfigured_fresh_signal_does_not_qualify_with_it`

**Do**

Score an in-market, in-window lead whose only signal is a fresh award, under a config requiring a configured signal.

**Expect**

Lane `target_market_only`, total at most 45.

---

### Check: bsq-without-requirement-control

**Requirement:** A signal the skill does not name cannot qualify a lead
**Surface:** Gated scoring
**Automated:** `tests/test_gated_score.py::TestOnlyAConfiguredSignalOpensTheGate::test_an_unconfigured_fresh_signal_qualifies_without_the_option`

**Do**

Score the same lead with the requirement off.

**Expect**

Lane `qualified` — so the refusal above is the requirement's doing, not the fixture's.

---

### Check: bsq-named-signal-admits

**Requirement:** A named signal inside the window qualifies the lead, and older ones do not
**Surface:** Gated scoring
**Automated:** `tests/test_gated_score.py::TestOnlyAConfiguredSignalOpensTheGate::test_a_configured_fresh_signal_still_qualifies`

**Do**

Score an in-market, in-window lead carrying a fresh configured signal beside an award.

**Expect**

Lane `qualified`.

---

### Check: bsq-stale-named-signal-refused

**Requirement:** A named signal inside the window qualifies the lead, and older ones do not
**Surface:** Gated scoring
**Automated:** `tests/test_gated_score.py::TestOnlyAConfiguredSignalOpensTheGate::test_a_stale_configured_signal_does_not_qualify`

**Do**

Score a lead whose configured signal is eight months old, with a six-month window, then with an eighteen-month one.

**Expect**

Not qualified under six months; qualified under eighteen.

---

### Check: bsq-selected-is-admitting

**Requirement:** The signal a lead reports is one that could have qualified it
**Surface:** Gated scoring
**Automated:** `tests/test_gated_score.py::TestOnlyAConfiguredSignalOpensTheGate::test_the_selected_signal_is_the_one_that_opened_the_gate`

**Do**

Score a lead with a fresher award and an older configured signal, under the requirement.

**Expect**

The selected signal is the configured one and `selected_from_fresh` is true.

---

### Check: bsq-selection-uses-gate-window

**Requirement:** The signal a lead reports is one that could have qualified it
**Surface:** Gated scoring
**Automated:** `tests/test_gated_score.py::TestSelectionUsesTheGatesOwnWindow::test_selection_ignores_a_signal_older_than_the_window_the_gate_used`

**Do**

With the window set only under `buying_window` at six months, score a lead with a strong ten-month-old signal and a weak fresh one.

**Expect**

The weak fresh signal is selected and `selected_from_fresh` is true.

---

### Check: bsq-timing-is-scoring-signal

**Requirement:** A qualified lead displays the signal that qualified it
**Surface:** Run output to the gateway
**Automated:** `tests/test_timing_matches_score.py::TestTheDisplayedTimingIsTheScoringSignal::test_a_fresh_selected_signal_replaces_the_judges_event`

**Do**

Pass a scored item whose judge event is from 2024 and whose fresh selected signal is from 2026 through `show_scoring_signal`.

**Expect**

`signal_date` and `signal_event` are the selected signal's.

---

### Check: bsq-gated-out-keeps-judge-timing

**Requirement:** A qualified lead displays the signal that qualified it
**Surface:** Run output to the gateway
**Automated:** `tests/test_timing_matches_score.py::TestTheDisplayedTimingIsTheScoringSignal`

**Do**

Pass an item whose selected signal is not fresh, one whose selected signal is undated, and a legacy item.

**Expect**

All three keep their existing `signal_date`; nothing is counted as changed.

---

### Check: bsq-judge-sees-lane-signals

**Requirement:** The stage judge is shown the signals the score reads
**Surface:** Pipeline-stage judgment
**Automated:** `tests/test_timing_matches_score.py::TestTheJudgeSeesTheScorersSignals`

**Do**

Build the judge's prospect block with and without the signal lane named.

**Expect**

With the lane, the dated lane signal and its description appear and an undated one does not; without it, the lane's date is absent.

---

### Check: bsq-lane-told-signals-and-window

**Requirement:** The research step is told the skill's buying signals and window
**Surface:** Enrichment prompt
**Automated:** `tests/test_enrichment_lanes.py::TestTheGatesLaneIsToldWhatABuyingSignalIs`

**Do**

Narrow a two-lane config requiring a configured signal with a six-month window on 2026-10-06, and render the group briefs.

**Expect**

Only the signal lane carries the labels and definitions and `2026-04-06`; the brief names the exclusions once; the authored lanes are unmodified.

---

### Check: bsq-opt-in-untouched

**Requirement:** A skill that has not opted in is unchanged
**Surface:** Enrichment prompt
**Automated:** `tests/test_enrichment_lanes.py::TestTheGatesLaneIsToldWhatABuyingSignalIs::test_untouched_without_the_option`

**Do**

Narrow lanes under a config without the requirement.

**Expect**

The same list object is returned.
