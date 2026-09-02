---
type: Playbook
title: Tune a config without spending anything
description: Re-score, re-judge and A/B stored scan data offline, so a config change is measured before it is paid for.
resource: scripts
tags: [ops, evaluation, cost, config]
timestamp: 2026-09-02
---

# Tune a config without spending anything

Discovery is the expensive phase. Nearly every config question — did this scoring change
help? does this validation batch size lose accuracy? — can be answered against **already
stored** scan data instead of a fresh run.

The scripts in `scripts/` are standalone, write no database rows, and run no scan:

| Question | Script |
|---|---|
| How would stored prospects score under a candidate config? | `evaluate_config_offline.py` — spends nothing |
| Same, but with the org's context references resolved first | `resolve_and_evaluate.py` |
| How many dated signals does this validation config actually find? | `measure_signal_yield.py` — **this one spends**: one grounded call per prospect. Resumable |
| How would re-judging then re-scoring stored rows turn out? | `rejudge_and_score.py` — resumable, saves judged partials per chunk |
| Does a larger validation batch lose classification accuracy? | `validation_batch_ab.py` — exits non-zero on regression, so it can gate |

Two of these are resumable and one saves partials per chunk, which tells you something
about their runtime: assume a long job and a possible interruption rather than a quick
check.

**Note which one costs money.** Signal-yield measurement makes a real provider call per
prospect; the others read what is already stored.

For a genuinely free full-pipeline run, use the mock provider —
[integrations/llm-providers](/integrations/llm-providers.md).
