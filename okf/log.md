# OKF Log

## 2026-09-02

- **Update** — Bundle authored. This repo had no OKF bundle and was named in no process
  document; it is now onboarded as one of the workspace's bundled repos, and declares
  `aeo-backend`, `conqrse-queue` and `aeo-skill-builder-runtime` as siblings. Concepts
  written from the code: the runner, both mappings, context references, the two scoring
  models, the phase pipeline, four integrations, and the offline-evaluation runbook. Also
  added `scripts/okf-check.mjs` with its proof suite, a `gate` entry point, and CI running
  that same entry point.

- **Learning** — **`README.md`'s "Known gaps" section is wrong in a way that would change
  what someone builds.** It states that `validation` and `contacts` "do not run", that the
  engine exposes only discovery and scoring, and that a config authoring those sections
  "gets a loud warning at start-up, not silence". All three claims are false: both phases
  are fully implemented and executed by `aeo/runner.py`. Worse, the warning does not exist
  either — `unsupported_authored_sections()` and its `UNSUPPORTED_SECTIONS` constant are
  imported into the runner and **never called**. The same stale two-phase framing appears in
  `aeo/config_mapping.py`'s module comment and in a `Dockerfile` comment. Three documents
  and one dead import all agreeing with each other is exactly why a reader would believe
  them. Recorded, not silently edited: reconciling them is a code-bearing change with an
  owner, and the dead import should be deleted rather than wired up.

- **Learning** — `README.md` says `av_lead_scanner.py` is vendored and must not be edited,
  which is incomplete: the file imports from this repo's own `aeo` package and carries
  several edits explicitly marked as vendored-engine edits. Every one is logged in
  `UPSTREAM.md`'s edit table, so the practice is sound — but a reader of the README alone
  is misled, because the rule is stated without pointing at the table that qualifies it.

- **Learning** — `aeo/rescore.py` says of itself that it is planning only, that nothing in
  it re-scores anything, and that nothing calls it. That is accurate. Kept because the
  offline scripts do the equivalent job; noted so nobody assumes the runner has a re-score
  path.

- **Learning** — The gate notes `lint`, `typecheck` and `build` as absent: there is no
  linter, type checker or build step in this repo. There is a substantial pytest suite (34
  test files), which the gate runs. `pytest` appears only as a **comment** in
  `requirements.txt` and there is no `requirements-dev.txt` or `pyproject.toml`, so CI
  installs it explicitly.

- **Learning** — `tests/fixtures/eap-parity/` was uncommitted at the time of this bundle
  and was left untouched. It is a handed-over config, design notes and a real scored test
  run from a sibling scanner skill, used by a fidelity test that asserts this engine
  reproduces the same ranking and bands **from configuration alone**. That is the strongest
  evidence available that "many skills, one runtime" actually holds; it belongs committed.
  Owner: Joe.

## 2026-09-09

- **Update** — [/lib/scoring.md](/lib/scoring.md) concepts read; two stale claims corrected
  in `aeo/gated_config_healthcare.json` and `aeo/rescore.py`. Docs and one orphaned config
  document only: no statement, no engine behaviour, and no live config was touched.

- **Learning** — **`aeo/gated_config_healthcare.json` is referenced by nothing.** No import,
  no test, no doc, no Dockerfile line. It was added as "how a skill opts in", and a document
  nobody loads is a document nobody notices going stale — which is exactly what happened: it
  still carried the pre-ruling five-rung `window_stages` while all four live gated configs
  carry seven. Corrected, and the ruling recorded in its `$comment`. If it is meant to be the
  reference template, something should read it; if not, it should go.

- **Learning** — 🔁 **The all-rungs buying window was real, ruled, and then REVERSED — all on
  2026-09-09.** It was a genuine PO ruling of 2026-09-01, committed as `aeo-backend` 30b2fec
  and taken against a real measurement (36 -> 63 qualified on MYgroup; re-measured
  independently as 63 of 69, which agrees). The PO reversed it after seeing it applied to a
  rescored book. `window_stages` is rungs 1-5 again.

  ⚠️ **This entry asserted three different things in one day, and the sequence is the
  lesson.** First: the gate looked like a defect, read from the config alone. Second: a
  gitignored comment claimed a PO ruling, so it was recorded here as "by decision and not by
  defect" — correct conclusion, but on a source with no provenance. Third: a broader search
  found the ruling committed in two other repos, so the conclusion held for a better reason
  than the one first given. Fourth: the PO reversed it. 🔑 **The failure was searching for
  one phrasing** — "EVERY rung", "terminal ones included" — and concluding from its absence
  that no record existed. `aeo-backend` said "ALL SEVEN stages" and the skill-builder test
  said "defaults to ALL". **A negative result from a grep is evidence about the grep.**

  🔑 **And the decision unit mattered more than the decision.** "36 to 63 qualified" reads
  like an improvement. The same fact as "43% of MYgroup's qualified list, and 79% of Matrix
  Frame's, are `7 - Too Late`" is what triggered the reversal. Price a scoring change as a
  share of the list somebody has to work, not as a delta.

- **Learning** — **`stage` feeds no bonus band, only the gate**, so within the window rank
  follows signal strength and recency rather than deal stage. `stage` appears in
  `gated_score.py` only inside `in_buying_window`.

  ✅ **A stage term in the bonus was raised with the PO on 2026-09-09 and declined, and the
  window reversal later that day made the decline better-founded rather than worse.** While
  every rung was in the window, "rank cannot tell an active deal from a decided one" was a
  real defect that a bonus term would have papered over — the honest fix was always to stop
  admitting decided deals. With `window_stages` back to rungs 1-5, every admitted lead is
  legitimately in-window, so ranking on signal strength and recency is defensible and the
  declined term is simply unnecessary. **Fixing the gate removed the reason to want it.**

  ⚠️ **This entry originally read "a consequence of that ruling … and it is INTENDED", and
  cited AP Emissions Technologies ("7 - Too Late") re-scoring to 92 above Kriya Therapeutics
  ("4 - Active Pursuit") at 89 as intended behaviour.** Under the reverted window AP
  Emissions is not admitted at all. The example was correct; calling it intended was not.

  🔑 **The reopening condition named here was met within hours of being written.** It said:
  *"the one thing that would reopen it is evidence rather than opinion — a salesperson
  working the list top-down and wasting time on decided accounts. That is an observation
  nobody has made yet."* The PO made exactly that observation the same day, and it overturned
  the ruling. Worth keeping as evidence the mechanism works: a named, falsifiable reopening
  condition is what let a report of "the scoring seems off" resolve to a specific decision
  instead of a re-litigation.

- **Learning** — MYgroup's stored runs ARE re-scorable and the question is now purely a
  decision, not a feasibility problem. Both 2026-08-27 runs (69 scored rows) carry dated
  `switching_signal` objects, every prospect is `NC` against the org's `["North Carolina"]`
  with the alias map reconciling both spellings, `plan_rescore` returns a plan rather than
  refusing, and the structurally-empty 46-79 band stays empty. 26 of the 69 currently sit
  INSIDE 46-79, which is direct proof they are on the pre-gated scale. Cost is zero — no
  grounded request.

  ⚠️ **This entry ended "forward-only remains the standing ruling; nothing was written" when
  it was first appended, hours before that stopped being true.** Corrected rather than left
  standing, for the same reason the `rescore.py` banner was: an entry stating the old ruling
  gets read as the current one. Reversed later the same day, and the rescore is now in
  production.

- **Learning** — `RescoreRefused`'s docstring cites "306 flooring prospects". Stored rows now
  show `commercial-flooring-prospect-scanner` at 498 scored / 348 with `signals_found`;
  neither is 306. Left alone deliberately — it is an illustrative figure in an exception
  docstring, not a claim anyone builds on, and correcting it was outside what was asked.
  Flagged so the next reader knows it is approximate. Owner: Joe.

- **Update** — MYgroup's two 2026-08-27 runs rescored onto the gated model, 69 rows.
  **Local first, then production the same day** — this line read "local database only" until
  the production sync landed. Concepts read: [/lib/scoring.md](/lib/scoring.md),
  [/playbooks/offline-evaluation.md](/playbooks/offline-evaluation.md). No engine file
  changed; the writer lives outside this repo in `aeo-backend/.temp/verify/`.

- **Update** — 🔴 **`aeo/rescore.py`'s `NOT APPROVED` banner was stale and is now
  corrected.** The PO reversed forward-only on 2026-09-09; the banner still cited
  2026-08-31's *"we do not need to rescore anything"*, so it had become wrong in the
  opposite direction — worse than wrong in the original one, because a reader would have
  refused work the PO asked for and cited this file doing it. Docstring only, proven by an
  AST comparison that was itself shown to fail on a one-constant edit before being
  trusted; no engine file in the diff.

  ⚠️ **This entry replaces one written earlier the same day** which said the banner was
  "left in place … Owner: Joe". That was true for about an hour and would have sent the
  next reader looking for finished work. Rewritten rather than appended-to because the log
  is read top-down for current state, and two entries on one date disagreeing about
  whether a thing is done is the same defect the roadmap has now made three times.

  The banner's own warning — a file claiming its own authorisation is the drift nobody
  re-reads for — has now been demonstrated in **both** directions by that one docblock: it
  once asserted a reversal that had not happened, and then denied one that had. Both
  instances are kept visible in the file, and it now points at the roadmap row and this log
  rather than asserting anything itself.

- **Learning** — **Re-judging buys no score change, so never pay for it as part of a
  rescore.** `pipeline_status` appears in `gated_score.py` only inside `in_buying_window`
  and no bonus band reads stage, so within the window rank follows signal strength and
  recency, not deal stage. The paid `rejudge_and_score.py` path would have cost real money
  and moved not one score. ⚠️ **This entry originally justified that with "the ruling put
  every rung in the window, so stage admits everything" — which stopped being true when the
  window was reverted to rungs 1-5 the same day.** The conclusion survives on the narrower
  and more durable reason above: stage feeds no band. **After the reversal stage DOES reject
  again**, so a re-judge can change whether a lead is admitted at all — it still cannot move
  the score of one already admitted.

- **Learning** — **Three defects in `apply-rescore.js`, all found before it ran, all in the
  same class: the writer and the engine disagreeing about a shape.** (1) It reads
  `stage_after` per row — produced only by the paid re-judge — with no guard, so a
  rescore-only input writes `undefined`, which pg stores as NULL, **wiping
  `pipeline_status` on every row**. (2) It set `score_factors.gated` to the boolean `true`
  where the engine stores the full breakdown object; that makes a rescored row structurally
  unlike an engine-scored one, and breaks `_rank_key`, which does `factors.get("gated") or
  {}` then `.get("bands")` — a bool has no `.get`. (3) It backed up neither `priority_band`
  nor `rank` while the fixed version writes both. Four refusal guards were added and each
  **proven to fire** against a deliberately broken input, with an untouched control still
  accepted — the guards are worth nothing until they have been seen to refuse.

- **Learning** — **`priority_band` and `rank` are computed by calling this repo's own
  `av_lead_scanner.priority_band()` and `_rank_key()`, never reimplemented downstream.**
  `_rank_key` is a five-key tie-break cascade that exists because a single-key sort let
  tied prospects reorder between runs of the same data — measured on run `741b7b3b` as 4
  ties covering 8 of 24 prospects. A JS port of a determinism guarantee is a second
  implementation of the exact thing it guarantees.

- **Learning** — `aeo/gated_config_healthcare.json`'s `$comment_bands` describes a **third**
  band table — `Hot 75-100 / Warm 50-74 / Cold 0-49` — which is neither the live config's
  (`80-100 / 46-79 / 0-45`) nor the legacy stored labels. Not corrected: the session that
  found it was scoped to the `window_stages` claim in the same file. Same orphaned-document
  problem as that one. Owner: Joe.

- **Update** — The MYgroup rescore is **in production** as of 2026-09-09, applied and verified
  locally first. 69 rows, four columns (`score`, `priority_band`, `rank`, `score_factors`),
  no stage written and no row deleted. Verified in production after the write: bands
  consistent with scores (63 `Hot - Ready Now` at 86-94, 6 `Cold - Monitor` at 27-35), 0 in
  the forbidden band, 0 legacy labels, 69/69 carrying `gated.bands`, ranks total and
  contiguous per run, and the 90 never-scored rows untouched.

- **Learning** — 🔴 **`kguser` is not a reliable name for production, and the rescore writer
  believes it is.** `aeo-backend/.temp/verify/apply-rescore.js:75` decides production with
  `current_user === 'kguser'`. The local Docker container `aeo-pg-prodcopy` on
  `127.0.0.1:5433` runs as `kguser`/`kgdb` — measured, not assumed. So a run against that
  container prints `*** PRODUCTION ***`, demands `--production`, accepts it, and reports the
  rows committed while production is untouched; the inverse guard ("`--production` against a
  non-production database → refuse") cannot fire either, because the container looks like
  production. **One fact defeats both halves**, and it fails toward confidence rather than
  toward caution — the direction that costs the most. The production sync therefore used a
  separate script taking its connection from the running production Deployment's own
  Kubernetes Secret: identity by **provenance**, which a stale env file cannot redirect.
  Roadmap row `fix-prod-detection-in-rescore-writer`, and its acceptance criterion is the
  **refusal being demonstrated**, not the rule being written. Owner: Joe.

- **Learning** — **Bringing a verified local result to production is a delta, never a
  replace.** Production held 5 `read` flags on these rows and its own stage history; the
  local copy could not have either. Replacing the two runs wholesale would have destroyed
  that silently while looking like a faithful copy. Two integrity checks made the delta safe
  and both are cheap enough to repeat: every target row still held its **pre**-re-score
  value (so the local result genuinely described production), and local's `score_factors`
  was proven a **superset** of production's (so replacing that column loses nothing).

- **Learning** — **The `.temp/verify` prodcopy container is an OLDER copy than the working
  database.** `localhost:5433` holds 14 orgs / 1,399 prospects; production and
  `localhost:5432` both hold 16 / 1,564. Anything reasoning about "the production copy" needs
  to say which port it means.

- **Update** — 🔴 **The gate no longer opens on a signal dated in the future**, and both books
  are re-scored and in production. `fresh_signals` now refuses a future date while
  `age_months` still clamps it to 0 — the split is deliberate: a known upcoming event is fair
  to CREDIT in `band_recency`, and unfair to ADMIT on, because it has not happened.
  MYgroup 36 -> **35** qualified, Matrix Frame stays 20 (one lead moved Top -> Standard
  Priority because the fresh pool no longer offers a future signal to select).

- **Learning** — **Found in production, by looking at rank 1.** MYgroup's `Andrew Bateman`
  was rank 1 at score 94 on a signal dated 2026-09-15 against a run of 2026-08-27 —
  nineteen days ahead — with **no other fresh signal at all**, so the clamp was the only
  thing admitting it. Six such signals existed across the two books. Rank 1 is now
  `David Grigg`, admitted on real past evidence. 🔑 **`gated_score.py` already stated the
  rule this broke:** `_parse_partial` resolves an imprecise date to its earliest instant so
  that *"the only thing an imprecise date can do is CLOSE the gate, never open one."* The
  future clamp was the one place in the module that violated its own principle — worth
  remembering as a search pattern, because a module that states a rule is a module that can
  be checked against it.

- **Learning** — **Two of my own verification queries were wrong before the third was right,
  and each was wrong in the direction of alarm.** The first counted the 46-79 forbidden band
  across the WHOLE prospects table and returned 102 — all of them legacy-scored rows on four
  non-gated skills, where 46-79 is a legitimate score. The second looked for admitted leads
  with no usable signal and returned 1: `Apiture`, whose dates are `2025-10` and `2026-01` —
  **partial dates**, which the engine parses and my `^\d{4}-\d{2}-\d{2}$` regex rejected.
  ⚠️ **A check written against a narrower grammar than the code accepts reports defects that
  do not exist**, and it is the same class of error as a check too broad to fail. Both were
  caught by reading the offending row rather than by trusting the count.

- **Learning** — The eight new tests were **proven to fail against the unfixed engine**
  before being trusted, and the four that failed are named: the admission test, the
  `fresh_signals` filter, the partial-future case, and `selected_from_fresh`. The four that
  passed either way are the controls — a past date still admits, a future date beside a
  fresh one still admits, an unparseable date is unknown rather than future, and
  `age_months` still clamps. **A test class where every test fails without the fix has no
  control in it**, and would pass just as well if the gate closed on everyone.

- **Learning** — 🔴 **The review pass ran AFTER landing, and it found a blocker that
  invalidated an assurance repeated six times: the production backups had no working
  restore.** `sync-rescore-to-prod.js` writes `{target, rows:[...]}`; the only restore tool,
  `restore-rescore.js`, reads `raw.prospects` and exits 3 — so all nine production restore
  points were unreadable. Forcing one through would have written `ai_analysis` and
  `pipeline_updated_at` (absent from the backup, so both to NULL — real data loss) while
  never restoring `priority_band` or `rank`, the columns the sync actually wrote. Six
  production writes were made while "restore point kept" was being said. Fixed by
  `restore-sync-backup.js`, which derives its writable column set FROM the backup file and
  refuses a file of the wrong shape — and **proven by a round-trip on 69 real rows in both
  directions** rather than asserted. ⚠️ **The process lesson is the ordering:** gates and
  self-written tests are not a review, and the methodology puts the review before landing
  for exactly this reason.

- **Learning** — ⚠️ **The production-writing tools live in `.temp/verify`, gitignored, so
  they cannot be committed, reviewed, CI-tested or caught by a PR** — and one has now
  written to production nine times. Every security blocker found traces to that. Worth its
  own change: move this tooling into a repo with tests. Owner: Joe.

- **Learning** — **`is_future` has a measured, deliberate limitation: a PARTIAL date whose
  ambiguity window straddles the run date is admitted.** `_parse_partial` resolves to the
  earliest instant, so `"2026-08"` on a 2026-08-27 run reads as past. The conservative
  alternative (judge by the LATEST possible instant) was rejected on cost, not principle:
  it would refuse every bare year in the run's own year, discarding up to twelve months of
  legitimately past evidence. 🔑 **The two cases differ in kind** — a precise future date is
  someone recording a date that has not arrived; a partial date is the validator finding a
  real event it could not pin. Measured across both gated books: 68 year-month and 28
  bare-year signals, 8 admitted leads carry a straddling one, and **0 leads' admission
  depends on one**. Pinned by a test that names the trade-off, so a future "tidy-up" to
  latest-instant fails loudly. Re-measure before changing it.

- **Learning** — ⚠️ **The two scoring models now disagree about future-dated signals, on
  purpose, and nothing said so.** `av_lead_scanner.py:1889` treats a future-dated row as
  CURRENT for the legacy additive event axis — deliberately, pinned by
  `test_a_future_date_is_current_not_out_of_window` — on the reasoning that a scheduled
  renewal is a timing signal. Gated now refuses one at the gate. Both models are live and
  opt-in per skill, so **a skill's behaviour toward a future date depends on which model it
  runs**, in opposite directions. Not introduced by this change and not a defect in either
  model; recorded because the divergence is invisible from inside either one.

- **Learning** — **I over-counted my own controls.** The commit message for the future-date
  fix said "four are controls"; one of the four was a byte-for-byte duplicate of a
  pre-existing assertion in `TestBoundaryAsymmetry` and added no coverage. Replaced with a
  test that pins the SPLIT — the same date credited by the band and refused by the gate,
  which is the property neither half pins alone. **6 of 9 now fail without the fix, and the
  three genuine controls are named.** A control that duplicates an existing test inflates
  the appearance of rigour without adding any.
