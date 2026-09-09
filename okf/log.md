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

- **Learning** — **The buying-window gate admits every rung, by decision and not by defect.**
  PO ruling 2026-09-01 (recorded in `aeo-backend/.temp/verify/propose-gated-generic.js:196`):
  the fresh signal decides in-market, and a recently-decided account is still worth
  surfacing. Measured at the time as 36 -> 63 qualified on MYgroup; re-measured independently
  on 2026-09-09 as 63 of 69, which agrees. Anyone re-deriving this from the config alone will
  read a gate that can never exclude and conclude "defect" — the ruling is the missing half,
  and it lives in a gitignored scratch script.

- **Learning** — **A consequence of that ruling nothing else states, and it is INTENDED.**
  `stage` appears in `gated_score.py` only inside `in_buying_window`. It is binary admission
  and feeds no bonus band, so ranking cannot distinguish an active deal from a decided one.
  On MYgroup's last run AP Emissions Technologies ("7 - Too Late") re-scores to 92, above
  Kriya Therapeutics ("4 - Active Pursuit") at 89.

  ✅ **Raised with the PO on 2026-09-09 and declined: no stage term in the bonus.** So the
  ruling means what it says — a fresh signal decides in-market, and rank follows signal
  strength and recency rather than deal stage. **Closed, not open.** Recorded because
  anyone re-deriving this from the code will read it as a defect and re-raise it, which is
  precisely what happened on 2026-09-09 before the ruling was found; it lives in a
  gitignored scratch script and nowhere a reader would look.

  ⚠️ **The one thing that would reopen it** is evidence rather than opinion: a salesperson
  working the list top-down and wasting time on decided accounts. That is an observation
  nobody has made yet, and it is the only kind that should overturn a deliberate call.

- **Learning** — MYgroup's stored runs ARE re-scorable and the question is now purely a
  decision, not a feasibility problem. Both 2026-08-27 runs (69 scored rows) carry dated
  `switching_signal` objects, every prospect is `NC` against the org's `["North Carolina"]`
  with the alias map reconciling both spellings, `plan_rescore` returns a plan rather than
  refusing, and the structurally-empty 46-79 band stays empty. 26 of the 69 currently sit
  INSIDE 46-79, which is direct proof they are on the pre-gated scale. Cost is zero — no
  grounded request. **Forward-only remains the standing ruling**; nothing was written.

- **Learning** — `RescoreRefused`'s docstring cites "306 flooring prospects". Stored rows now
  show `commercial-flooring-prospect-scanner` at 498 scored / 348 with `signals_found`;
  neither is 306. Left alone deliberately — it is an illustrative figure in an exception
  docstring, not a claim anyone builds on, and correcting it was outside what was asked.
  Flagged so the next reader knows it is approximate. Owner: Joe.

- **Update** — MYgroup's two 2026-08-27 runs rescored onto the gated model, 69 rows,
  local database only. Concepts read: [/lib/scoring.md](/lib/scoring.md),
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

- **Learning** — **Re-judging buys no score change under the current gate, so never pay
  for it as part of a rescore.** `pipeline_status` appears in `gated_score.py` only inside
  `in_buying_window`; the PO's 2026-09-01 ruling put every rung in the window, and no bonus
  band reads stage. So stage is binary admission that admits everything. The paid
  `rejudge_and_score.py` path would have cost real money and moved not one score.

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
