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
