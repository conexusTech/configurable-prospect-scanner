# Upstream provenance

This repo is a **derivation**, not an original. Its discovery-and-scoring engine
was copied from a live customer skill.

| | |
|---|---|
| **Upstream repo** | `skill-av-lead-scanner` (`git@github.com:conexusTech/skill-av-lead-scanner.git`) |
| **Upstream commit** | `3791a718b385f80b3565e38a7246fa8165bc519d` |
| **Copied on** | 2026-08-04 |
| **Files taken verbatim** | `av_lead_scanner.py`, `requirements.txt`, `tests/` |
| **Files kept for reference only** | everything under `reference/` |

## Why a copy and not a dependency

`skill-av-lead-scanner` is a **live customer skill** (church/AV integration). The
platform's generic scanner and one customer's scanner must not be the same
artifact — a change made for the platform would reach a paying customer's
production runs, and vice versa. Upstream is also understood to be feature-frozen
going forward, which is what makes a copy affordable rather than a slow leak.

## The rule that keeps this affordable

**Do not edit `av_lead_scanner.py`.**

It is vendored verbatim so that pulling an upstream fix stays a diff rather than an
archaeology exercise. Everything this platform needs lives in separate modules
(`aeo/`), exactly the boundary upstream themselves drew — their
`examples/aeo_integration.py` header states that the core tool "stays generic — it
knows nothing about AEO" and that all AEO-specific behaviour belongs in a wrapper.
That file is preserved at `reference/upstream-aeo_integration.py`; our `aeo/`
package is that example promoted to production.

If the tool genuinely must change, prefer contributing upstream and re-vendoring.
If that is impossible, record the edit in the table below so the next person
pulling upstream knows where the conflicts will be.

| Date | File | What was changed and why |
|---|---|---|
| 2026-08-21 | `av_lead_scanner.py` | **A model-decided pipeline stage now wins over `calculate_pipeline`'s date ladder.** Added `_pipeline_from_judgment` and one line in `score_prospects` that prefers it. `calculate_pipeline` itself is UNTOUCHED and remains the fallback for any prospect the judge did not reach, so a failed model call degrades to previous behaviour rather than to nothing. **Why the engine had to change rather than a post-process:** the scored item carries both the stage AND `score_factors.pipeline_timing`, and overwriting only the stage afterwards leaves the axis scored from the ladder we are replacing — a prospect reading "Decision Imminent" while carrying the 2-point "Too Late" weight. Recomputing the total outside the engine would duplicate its cap/sum logic and drift from it. **Why it was needed at all:** the ladder parses ONE date and buckets the months to it, so on a real run (`9f9fe2d7`) a `Commercial building permit` dated 2026-02-20 and a `Lease` from December 2019 received the same "7 - Too Late" verdict — 40.5% of prospects landed in the two "you missed it" rungs. The event TYPE decides what a date implies and the ladder never reads it, though `transaction_type`/`trigger_type` were collected all along. The stage's scoring weight comes from the vocabulary AEO ships, falling back to the engine's own `statuses` table. Judgment lives in `aeo/phases/ai_judgment.py`; tests in `tests/test_ai_judgment.py` and `tests/test_event_mapping.py::TestWhitelistParity`. |
| 2026-08-12 | `av_lead_scanner.py` | **Replaced the hardcoded field vocabulary with a config-derived one.** Removed `_FIELD_ALIASES` (20 canonical fields written for church/AV: `denomination`, `campaign_goal`, `amount_raised`, `av_opportunity_notes`, `project_phase`, `consultant_firm`, …). Added `_IDENTITY_ALIASES` (vertical-neutral: name/address/city/state/zip/website/contact), `_norm_key` (case- and punctuation-insensitive matching, replacing per-field synonym lists) and `canonical_fields_from_sources` (the union of `fields` authored across the skill's discovery sources). `_merge_raw_rows` / `_merge_for_scoring` / `_scoring_input` / `_assemble_prospects` take the derived field set; **absent config falls back to every key present in the data, never a fixed list.** Regression tests in `tests/test_config_derived_fields.py`. |

| 2026-08-12 | `av_lead_scanner.py` | **Scoring: the skill's own authored factors now score, and completeness is config-derived.** Added `score_config_factors` / `_factor_credit` / `_first_number` — the authored `scoring.factors` occupy the `fit` axis (same `fit.max`, so the 0-100 scale is unchanged) with `min`/`max` turning an operator's ICP bound into a real scoring input; presence is the evidence, deliberately **not** a keyword table. `score_prospects` derives `completeness.fields` from the authored discovery fields when the config does not declare them, and the scored item now emits `fields` (the authored fields) in place of six hardcoded church-AV keys (`project_description`/`_type`/`_phase`, `campaign_goal`, `denomination`, `estimated_timeline`). |
| 2026-08-12 | `tests/test_av_lead_scanner.py` | `test_score_capped_at_100` now declares `scoring.completeness.fields` explicitly. It previously relied on the hardcoded church-AV field list being the default, which its lead happened to fill completely; with completeness config-derived the total is 98 and the cap the test exists to check goes unexercised. **The test's intent was preserved rather than the assertion relaxed.** |

| 2026-08-12 | `av_lead_scanner.py` | **Region and pipeline timing are now sourced from the target organization.** Added `regions_from_markets` — the region map is built from the org's own service areas, replacing `regions = {}` (empty, so no region bonus was reachable) and a `state_aliases` table covering only Texas, Colorado and Indiana. `score_region` also matches **market cities**, so a market written "Nashville, Tennessee" still resolves against a lead's "TN" **without introducing a state-name table**. `score_prospects` takes `pipeline.decision_lead_months` from the org's `sales_cycle_months` when the config does not set it. `pipeline.default` now **abstains** (`status: None`, `score: 0`) instead of returning the literal `"Unknown"` with 10 free points. |
| 2026-08-12 | `tests/test_av_lead_scanner.py` | `test_pipeline_default_unknown` → `test_pipeline_default_abstains_rather_than_inventing_a_stage`, asserting the new contract (abstain, zero points) with the reasoning inline. |

| 2026-08-12 | `av_lead_scanner.py` | **Authored factors are the dominant scoring axis, and `disqualify_below` is implemented as a FLAG.** When a skill authors `scoring.factors` and does not set `fit.max`, the factors axis defaults to **40** — larger than pipeline timing's 30 — because the operator's stated ICP is the best available signal about who is worth calling, and the legacy weight of 25 made it a minority of a score dominated by axes nobody authored. Every axis max stays config-overridable. `disqualify_below` now sets a `disqualified` boolean on the scored item and **never filters**: the operator picks that threshold before seeing any score distribution, and on the first real run every prospect scored 11-12 against a threshold of 20 — filtering would have deleted a scan's whole yield with no error anywhere. |
| 2026-08-12 | `aeo/runner.py` | **Run limits come from the skill config, not the environment.** `SCANNER_TOP_N` alone drove three unrelated things — the discovery sufficiency target, contact-enrichment coverage, and the output cut. Added `_config_limit`: `discovery.target_prospects` and `contacts.max_prospects` win, the env var is the deployment fallback. **An environment variable is static configuration too**, and at `SCANNER_TOP_N=1` a real 15-market scan stopped after one round of discovery and enriched 1 prospect of 14 — which reads as "the model found little" rather than "we stopped looking". |

| 2026-08-12 | `av_lead_scanner.py` | **Self-exclusion, plus discovery contacts and provenance on the prospect record.** Added `excluded_prospect_names` (org name + `aliases` + `exclusions`, matched on `normalize_name`) and an `excluded_names` filter in `_assemble_prospects`, because on the first real run the commissioning org appeared in its own prospect list and was rejected only by luck of an unrelated disqualifier. The prospect record now also carries `contact_name` / `contact_title` (a discovery source had a title for 12 of 36 prospects, stranded in `discovery_data`) and `sources` (never populated, so nothing could say where a prospect came from). ⚠️ Exclusion matching is on the **full normalized name, not a substring** — substring matching would let an exclusion of `Lee` delete `Leeds Property Group`, and a false exclusion removes a real prospect with no error and no trace. |

**Measured impact of the factor-axis change** — two prospects, same markets, one matching
both authored factors and one matching a single factor: **65 vs 40**. Before today every
prospect on the real run scored 11 or 12 regardless of its data, so the ranking carried no
information at all.

**Measured impact of the sales-cycle change** — same prospect, same data, completion
2027-06, today 2026-08-12:

| decision lead time | months out | stage | timing points |
|---|---|---|---|
| the org's real 4-month cycle | **6** | `4 - Active Pursuit` | **30 / 30** |
| the old hardcoded 13-month cycle | **−3** | `6 - Likely Awarded` | **8 / 30** |

A 22-point swing and the **opposite sales conclusion** — the engine declared the deal
already lost — from a number that had nothing to do with this organization.

⚠️ **Known remaining static binding:** `calculate_pipeline` still reads one hardcoded
input field name, `estimated_timeline`. A skill whose timing signal is `permit_date` or
`replacement_due` cannot score timing at all. Found by writing the test above.

### Why this edit was made despite the rule above

**PO ruling (2026-08-12): this engine must be fully flexible to whatever config the
Conversational Skill Builder produces — not static on any industry, organization or
geography — and anything it needs that is missing must be sourced from the target
organization rather than from a default.** `_FIELD_ALIASES` was not overridable through
the context, so there was no way to honour that from the `aeo/` wrapper.

The rule's own justification is what makes the edit affordable: this is a **copy**
precisely so platform changes cannot reach the church/AV customer's production runs, and
upstream is *"understood to be feature-frozen going forward"*, so the re-vendoring diff
this costs is unlikely ever to be paid.

**The defect it fixes, measured on the first real production run** (`c214e3d5`, an HVAC
skill): the model returned the data the operator's ICP depended on, and the merge threw it
away. `square_footage` was present on 17 of 36 prospects and `portfolio_size` on 10; both
were absent from `_FIELD_ALIASES` and so were dropped before scoring. `industry` and
`contact_title` were collected on 12 and stranded in `discovery_data` while their columns
stayed NULL. **The full 203-test suite passed before and after — nothing covered it.**

⚠️ **Still static in this file, and next on the list:** `_DEFAULT_SCORING` — `fit`'s
keyword table (`new sanctuary`, `new worship center`), `completeness.fields`,
`pipeline.phase_fallback` (14 church-construction stages), `campaign_goal_floor`,
`pipeline.statuses` (a hardcoded months-to-decision sales cycle while the org's own
`sales_cycle_months` goes unused), `region_bonus.state_aliases` (three states — the
previous customer's; Tennessee absent) and `region_bonus.regions = {}` (empty, so no
region bonus is reachable regardless of location data).

## How to re-vendor

```bash
git -C ../skill-av-lead-scanner fetch && git -C ../skill-av-lead-scanner log --oneline HEAD..origin/main
cp ../skill-av-lead-scanner/av_lead_scanner.py .   # then run the tests
```

Update the commit hash above in the same change.
