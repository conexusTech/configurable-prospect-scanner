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

- **Learning** — **A consequence of that ruling nothing currently states:** `stage` appears in
  `gated_score.py` only inside `in_buying_window`. It is binary admission and feeds no bonus
  band, so ranking cannot distinguish an active deal from a decided one. On MYgroup's last run
  AP Emissions Technologies ("7 - Too Late") re-scores to 92, above Kriya Therapeutics
  ("4 - Active Pursuit") at 89. If the ruling meant *include* rather than *rank equally*, that
  needs a stage term in the bonus. Open question for the PO. Owner: Joe.

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
