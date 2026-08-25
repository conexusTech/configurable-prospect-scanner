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
| 2026-08-22 | `av_lead_scanner.py` | **The scored item now carries `pipeline_source`, and `score` is rounded.** Two one-line changes in `score_prospects`'s emit. (1) `pipeline_source` propagates `pipeline.get("pipeline_source", "derived")`, so AEO can tell a model judgement from `calculate_pipeline`'s date arithmetic. Needed because AEO **re-derives a customer skill's stage unconditionally** — correct while the engine's only stage came from the date ladder, but it silently discarded every model verdict: a stage of `4 - Active Pursuit` persisted as `7 - Too Late`. A bare stage cannot carry that distinction and the engine is where both values are produced. `calculate_pipeline` is still untouched and returns no such key, so **absent == derived** and no default had to be invented. (2) `round(total)`: the total is `... + ai_adj` with no rounding, and AEO's `prospects.score` is an INTEGER column whose driver sends a JS float as TEXT — so `75.5` arrives as `'75.5'` and 500s the callback for every prospect of the run. Held for months only because `ai_score_adjustment` had no producer and `0` kept every total integral; the judgment phase activated it. |
| 2026-08-21 | `av_lead_scanner.py` | **A model-decided pipeline stage now wins over `calculate_pipeline`'s date ladder.** Added `_pipeline_from_judgment` and one line in `score_prospects` that prefers it. `calculate_pipeline` itself is UNTOUCHED and remains the fallback for any prospect the judge did not reach, so a failed model call degrades to previous behaviour rather than to nothing. **Why the engine had to change rather than a post-process:** the scored item carries both the stage AND `score_factors.pipeline_timing`, and overwriting only the stage afterwards leaves the axis scored from the ladder we are replacing — a prospect reading "Decision Imminent" while carrying the 2-point "Too Late" weight. Recomputing the total outside the engine would duplicate its cap/sum logic and drift from it. **Why it was needed at all:** the ladder parses ONE date and buckets the months to it, so on a real run (`9f9fe2d7`) a `Commercial building permit` dated 2026-02-20 and a `Lease` from December 2019 received the same "7 - Too Late" verdict — 40.5% of prospects landed in the two "you missed it" rungs. The event TYPE decides what a date implies and the ladder never reads it, though `transaction_type`/`trigger_type` were collected all along. The stage's scoring weight comes from the vocabulary AEO ships, falling back to the engine's own `statuses` table. Judgment lives in `aeo/phases/ai_judgment.py`; tests in `tests/test_ai_judgment.py` and `tests/test_event_mapping.py::TestWhitelistParity`. |
| 2026-08-12 | `av_lead_scanner.py` | **Replaced the hardcoded field vocabulary with a config-derived one.** Removed `_FIELD_ALIASES` (20 canonical fields written for church/AV: `denomination`, `campaign_goal`, `amount_raised`, `av_opportunity_notes`, `project_phase`, `consultant_firm`, …). Added `_IDENTITY_ALIASES` (vertical-neutral: name/address/city/state/zip/website/contact), `_norm_key` (case- and punctuation-insensitive matching, replacing per-field synonym lists) and `canonical_fields_from_sources` (the union of `fields` authored across the skill's discovery sources). `_merge_raw_rows` / `_merge_for_scoring` / `_scoring_input` / `_assemble_prospects` take the derived field set; **absent config falls back to every key present in the data, never a fixed list.** Regression tests in `tests/test_config_derived_fields.py`. |

| 2026-08-12 | `av_lead_scanner.py` | **Scoring: the skill's own authored factors now score, and completeness is config-derived.** Added `score_config_factors` / `_factor_credit` / `_first_number` — the authored `scoring.factors` occupy the `fit` axis (same `fit.max`, so the 0-100 scale is unchanged) with `min`/`max` turning an operator's ICP bound into a real scoring input; presence is the evidence, deliberately **not** a keyword table. `score_prospects` derives `completeness.fields` from the authored discovery fields when the config does not declare them, and the scored item now emits `fields` (the authored fields) in place of six hardcoded church-AV keys (`project_description`/`_type`/`_phase`, `campaign_goal`, `denomination`, `estimated_timeline`). |
| 2026-08-12 | `tests/test_av_lead_scanner.py` | `test_score_capped_at_100` now declares `scoring.completeness.fields` explicitly. It previously relied on the hardcoded church-AV field list being the default, which its lead happened to fill completely; with completeness config-derived the total is 98 and the cap the test exists to check goes unexercised. **The test's intent was preserved rather than the assertion relaxed.** |

| 2026-08-12 | `av_lead_scanner.py` | **Region and pipeline timing are now sourced from the target organization.** Added `regions_from_markets` — the region map is built from the org's own service areas, replacing `regions = {}` (empty, so no region bonus was reachable) and a `state_aliases` table covering only Texas, Colorado and Indiana. `score_region` also matches **market cities**, so a market written "Nashville, Tennessee" still resolves against a lead's "TN" **without introducing a state-name table**. `score_prospects` takes `pipeline.decision_lead_months` from the org's `sales_cycle_months` when the config does not set it. `pipeline.default` now **abstains** (`status: None`, `score: 0`) instead of returning the literal `"Unknown"` with 10 free points. |
| 2026-08-12 | `tests/test_av_lead_scanner.py` | `test_pipeline_default_unknown` → `test_pipeline_default_abstains_rather_than_inventing_a_stage`, asserting the new contract (abstain, zero points) with the reasoning inline. |

| 2026-08-12 | `av_lead_scanner.py` | **Authored factors are the dominant scoring axis, and `disqualify_below` is implemented as a FLAG.** When a skill authors `scoring.factors` and does not set `fit.max`, the factors axis defaults to **40** — larger than pipeline timing's 30 — because the operator's stated ICP is the best available signal about who is worth calling, and the legacy weight of 25 made it a minority of a score dominated by axes nobody authored. Every axis max stays config-overridable. `disqualify_below` now sets a `disqualified` boolean on the scored item and **never filters**: the operator picks that threshold before seeing any score distribution, and on the first real run every prospect scored 11-12 against a threshold of 20 — filtering would have deleted a scan's whole yield with no error anywhere. |
| 2026-08-12 | `aeo/runner.py` | **Run limits come from the skill config, not the environment.** `SCANNER_TOP_N` alone drove three unrelated things — the discovery sufficiency target, contact-enrichment coverage, and the output cut. Added `_config_limit`: `discovery.target_prospects` and `contacts.max_prospects` win, the env var is the deployment fallback. **An environment variable is static configuration too**, and at `SCANNER_TOP_N=1` a real 15-market scan stopped after one round of discovery and enriched 1 prospect of 14 — which reads as "the model found little" rather than "we stopped looking". |

| 2026-08-12 | `av_lead_scanner.py` | **Self-exclusion, plus discovery contacts and provenance on the prospect record.** Added `excluded_prospect_names` (org name + `aliases` + `exclusions`, matched on `normalize_name`) and an `excluded_names` filter in `_assemble_prospects`, because on the first real run the commissioning org appeared in its own prospect list and was rejected only by luck of an unrelated disqualifier. The prospect record now also carries `contact_name` / `contact_title` (a discovery source had a title for 12 of 36 prospects, stranded in `discovery_data`) and `sources` (never populated, so nothing could say where a prospect came from). ⚠️ Exclusion matching is on the **full normalized name, not a substring** — substring matching would let an exclusion of `Lee` delete `Leeds Property Group`, and a false exclusion removes a real prospect with no error and no trace. |

| 2026-08-24 | `av_lead_scanner.py` | **Authored factors can own their own axis, and a factor's binding is now declarable.** Three changes, all default-inert. (1) New `scoring.factors_max`: absent, factors borrow the `fit` axis at 40 exactly as before; present, factors are an independent axis, so a skill can put its whole `score_cap` on its own criteria and zero the axes it never authored — a vertical whose fit is not geographic sets `region_bonus.max: 0`. Needed because a config whose factors sum to 100 was being compressed into 40 while the other 60 came from axes it never wrote, including a geography bonus one delivery model explicitly forbids. (2) 🔴 **Fixed a falsy guard**: the `fit.max` override tested truthiness, so an authored `fit.max: 0` — the only way to say "this axis contributes nothing" — was read as *unset* and silently restored to 40. (3) `factors[].key` / `factors[].source_field` split identity from binding from label; `source_field` accepts a **list** in priority order, so one factor reads two discovery sources that named the same quantity differently (`estimated_headcount` vs `employee_estimate`). Motivated by a measured defect: a skill authored three factor names that matched nothing it collected and they scored zero for every prospect, forever, with no error. ⚠️ **Fuzzy matching still deliberately absent** — the author can now *say* what they meant; the engine does not guess. Verified: the AV sample scores **byte-identical** on the whole scored item (not just the total), and a config with no authored factors gains no new `score_factors` key. |

| 2026-08-24 | `av_lead_scanner.py` | **Graded factor credit — `tiers` (numeric) and `keywords` (text).** Factor credit was BINARY, which could not express what live factor descriptions already promise, and `max` made one actively inverted: a production factor reads *"managing at least 2 types earns partial credit; 5 or more earns full credit"* while carrying `min: 2, max: 5`, so partial credit was impossible **and** a firm managing 8 types tripped the bound and scored **0** while one managing 4 scored full — more earned less. A second reads *"matches qualifying categories rather than **disqualified** types"*, which presence cannot distinguish, so a disqualified type earned full credit. `tiers` grades on a numeric value, highest threshold met wins, and is **deliberately non-monotonic-capable** (a size band that peaks in the middle is the normal case). `keywords` grades on text with **longest-match-wins**, which removes the substring hazard the source config warned about (`standalone` sits inside `standalone-recent`, scoring 6 instead of 0) and makes authored order irrelevant. Points are RELATIVE — credit is `matched_points / max(points in table)` — so `weight` remains the factor's share of the axis and an existing config's per-factor `max_points` maps straight onto it. **The mode is DERIVED from which table is authored, not from a `type` field**, so a table can never be authored and silently ignored; both tables present resolves to `tiers` and is logged. `min`/`max` apply to presence mode only. Verified: the ported five-factor table reproduces all six published per-prospect totals of a real run exactly (69/69/63/59/59/52), and the AV sample stays **byte-identical**. |

| 2026-08-24 | `av_lead_scanner.py`, `aeo/phases/enrichment.py` (new), `aeo/runner.py` | **N authored enrichment lanes, and dated-event factors.** `validation.py` is one lane answering "is this qualified?" in a fixed four-field shape — right for a verdict, wrong for anything else a skill needs to learn about a prospect it already accepted. `validation.lanes` now declares any number of passes with authored output fields; rows are stored under each lane's `key` inside `validation_data` (already-open JSONB, **no migration**) and a factor binds to a lane by naming that key. Lanes run only on survivors, a failed call records no rows rather than a fabricated one, and a misauthored lane is skipped **with a logged reason** — silently dropped, it is indistinguishable from one that ran and found nothing. New `base_points`/`bonus_points`/`bonus_keywords`/`date_field`/`recency_months` factor mode grades dated events with a recency window; the best surviving row wins because two current signals are not twice as in-market as one. **Three defects found while building it, all silent-by-construction:** (1) a first cut of the strict date parser clamped any day past 28 "to be safe" and turned a valid `2026-07-30` into `2026-07-28` — inventing a different date, the exact failure the parser exists to prevent, so it now refuses an impossible day instead; (2) recency age was computed as `_months_between(dated, today)`, which is NEGATIVE for a past date and therefore never exceeded the window — **the gate was completely inert and every stale signal scored full**; (3) `_merge_raw_rows` stringifies every value (it exists to feed keyword matching), so a lane's list of rows reached the scorer as `"{'signal_type': ...}"` and an event factor saw one string instead of rows, scoring every prospect identically. Structured values are now overlaid onto the scoring input, leaving the merge itself untouched. ⚠️ `parse_signal_date` is deliberately NOT `parse_estimated_date`: the latter ends in a bare-year fallback that invents June, which for recency changes the answer rather than blurring it. Verified: AV sample **byte-identical**, 442 tests pass. |

| 2026-08-24 | `av_lead_scanner.py`, `aeo/event_mapping.py` | **Hard disqualifier rules and priority bands.** `disqualify_below` is a statement about RANK — the operator's cutoff, flagged not filtered. A rule here states INELIGIBILITY: too small, wrong market, a multi-year contract signed with someone else last month. No threshold on a score can express that, because a disqualified prospect may still score well on every other axis. `scoring.disqualify_rules` takes four industry-neutral primitives (`below`, `above`, `keywords`, `required_keywords`), is evaluated BEFORE any factor work, zeroes the score and records the reason. ⚠️ **A rule never fires on a field the prospect does not carry** — absence of evidence is not exclusion, or every prospect whose country the sources failed to return would vanish. `scoring.priority_bands` maps a score to the verdict an operator acts on, with a coverage advisory that names the first gap or overlap (a score falling in a gap has no verdict, invisible until the one prospect that lands there renders blank). 🔴 **`disqualified` was already computed and reaching nobody:** the engine has emitted it since 2026-08-12 and all three production skills author `disqualify_below: 40`, but it was absent from `SCORED_PASSTHROUGH` — computed on every prospect of every real run and dropped one line before the wire, the FIFTH instance of that tuple's own documented omission. And the parity test built to catch exactly this listed `disqualified` under a **false** justification ("carried on the validations event, not scoring") on a fixture that never produced the field, so the guard passed while the defect shipped. An untested exclusion entry is a comment, not a guard; it is now whitelisted with a named test that asserts the value arrives. AEO declares all three on `ScanScoredItemDto` first (migration 103 + COALESCEd UPSERT) — `forbidNonWhitelisted` is global, so gateway → scanner is not a preference. Verified: AV sample **byte-identical**, 470 tests. |

| 2026-08-24 | `av_lead_scanner.py` | 🔴 **Fix: the default church-AV keyword table leaked into other verticals, via own-axis mode.** `_DEFAULT_SCORING["fit"]["keyword_scores"]` is the ORIGINAL customer's vocabulary (`new construction: 25`, `renovation: 12`). It was unreachable for any skill with authored factors, because factors REPLACED the fit axis — until `factors_max` made `fit` a separate axis and the default became live again. **Measured**: a commercial-flooring prospect whose text reads "office renovation and tenant build-out" collected **12 points from a church keyword list**, in a plausible range, with nothing logged. That is precisely the static-industry coupling the PO ruled out, reintroduced by a change that looked purely additive. Now: a config that authors factors gets the `fit` axis **only if it authored `fit.keyword_scores`** — a skill that wants a keyword axis alongside its factors asks for one; a skill that does not gets zero rather than someone else's vertical. A config with NO factors is the legacy shape the default exists for and is untouched (the AV sample stays byte-identical). Pinned by three tests including the legacy case, because narrowing this further would silently zero the fit axis for every pre-factors config. |
| 2026-08-25 | `av_lead_scanner.py` | **A factor binds to a FIELD of an enrichment lane, not only to the lane.** Lane output was exposed only under its own key, holding a LIST of row dicts, while `_lookup_field` is a FLAT lookup — so a factor bound to a lane field resolved to `None` and scored 0 for every prospect, forever, with no error. Every other layer agreed it was valid: the schema advertises lanes as producing fields, the skill builder authors field-level bindings, and the gateway`s `bindableFieldNames` validates them. Measured on a real MYgroup draft (all default suggestions): two factors bound to `switching_likelihood` and `headcount_trend`, both lane fields, both 0 on all 15 prospects, the entire 35-point ICP axis dead, and **13 of 15 prospects reported ineligible** against a floor the surviving axes could not reach. Lanes are now exposed TWICE — under their key, because an event factor (`base_points`) binds to the lane wholesale and walks the rows via `_event_rows`, and flattened per row field. Three rules: `setdefault` so a DISCOVERY field of the same name wins (discovery observed it, a lane inferred it); the first NON-EMPTY value wins across rows in order, so a blank row cannot shadow a later real one; and the qualification verdict`s own keys are never overwritten by a row field sharing the name (`RESERVED_LANE_KEYS` forbade them as lane KEYS, nothing stopped a row field — the mirrored constant is asserted by a test). ⚠️ The mismatch was introduced the same day by widening the gateway`s `bindableFieldNames` to accept lane fields, signed off in an audit as *strictly a superset, can only accept more, never less* — for a lint whose job is rejecting bad bindings, accepting more means catching less. Verified: 5 new tests, 2 of which fail when the flattening is reverted; 506 total. |

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
