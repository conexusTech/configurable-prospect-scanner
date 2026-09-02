#!/usr/bin/env bash
# The gate — one entry point, five checks, and CI runs this same script.
# The name 'gate' is fixed even where the runner is not, so that CI, the engineer and
# .claude/rules/methodology.md all say the same word.
#
# Steps this repo has no toolchain for are NOTED, not skipped quietly. Replace a note
# with a real command as soon as the toolchain exists.
#
# Note: pytest appears only as a comment in requirements.txt; there is no requirements-dev.txt.
set -euo pipefail
cd "$(dirname "$0")/.."

absent=0
note() { echo "GATE: $1 — no toolchain in this repo; not checked"; absent=1; }

note "lint"

note "typecheck"

note "build"

echo "== test =="
python -m pytest tests/ -q

echo "== okf:check =="
node scripts/okf-check.mjs

if [ "$absent" = "1" ]; then
  echo "gate: the steps noted above are unimplemented in this repo; everything else is green."
fi
