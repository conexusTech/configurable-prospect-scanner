"""Resolve R12 `context_ref` bindings, then score stored prospects. Still spends nothing.

Why this exists separately from `evaluate_config_offline.py`: a builder-authored config
binds org-specific positions rather than hard-coding them, e.g.

    "allowed_states": {"context_ref": "home_markets"}

The SCANNER resolves those at `runner.py:599` before the pipeline runs. An offline
evaluation that skips resolution hands `in_target_market` a dict where a list belongs, gets
an empty allow-list, and reports **G1 dead / 0 qualified** — a confident, wrong verdict that
looks exactly like the real dead-gate defect this project spent a day fixing.

So resolution is not a nicety here; without it the harness manufactures the very bug it is
meant to detect. It uses the scanner's OWN resolver against the org's OWN runtime context
(`GET /runtime/organizations/:id/context`), so the inputs are the real code path's inputs.

Usage
-----
    python scripts/resolve_and_evaluate.py --rows rows.json --context context.json
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aeo.context_refs import UnresolvedRefError  # noqa: E402
from aeo.context_refs import resolve as resolve_refs  # noqa: E402
from aeo.rescore import RescoreRefused, plan_rescore, summarise  # noqa: E402

EMPTY_LOW, EMPTY_HIGH = 46, 79


def _load(path: str, label: str) -> Any:
    try:
        return json.loads(io.open(path, encoding="utf-8").read())
    except FileNotFoundError:
        raise SystemExit(f"{label} not found: {path}")


def _hist(scores: list[int], label: str) -> None:
    print(f"\n{label}")
    for lo, hi in [(0, 19), (20, 39), (40, 45), (EMPTY_LOW, EMPTY_HIGH), (80, 89), (90, 100)]:
        n = sum(1 for s in scores if lo <= s <= hi)
        flag = "   <-- MUST BE EMPTY" if (lo, hi) == (EMPTY_LOW, EMPTY_HIGH) and n else ""
        print(f"   {lo:3}-{hi:3}  {'#' * min(n, 40):<40} {n}{flag}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", required=True)
    ap.add_argument("--context", required=True, help="runtime context JSON (carries skill.config)")
    ap.add_argument("--as-of")
    ap.add_argument("--show", type=int, default=6)
    args = ap.parse_args()

    rows = _load(args.rows, "--rows")
    ctx = _load(args.context, "--context")
    raw_config = (ctx.get("skill") or {}).get("config") or {}

    raw_gate = ((raw_config.get("scoring") or {}).get("gate") or {}).get("target_market") or {}
    print("BEFORE resolution:")
    print("   allowed_states =", json.dumps(raw_gate.get("allowed_states")))
    print("   exclude_rules  =", json.dumps(raw_gate.get("exclude_rules")))

    try:
        config = resolve_refs(raw_config, ctx)
    except UnresolvedRefError as exc:
        print(f"\nUNRESOLVED BINDING: {exc}")
        print("The scanner would fail the same way — this is a real config defect.")
        return 2

    scoring = config.get("scoring") or {}
    gate = (scoring.get("gate") or {}).get("target_market") or {}
    print("\nAFTER resolution (what the gate actually sees):")
    print("   allowed_states =", json.dumps(gate.get("allowed_states")))
    print("   exclude_rules  =", json.dumps(gate.get("exclude_rules"))[:100])

    aliases = gate.get("state_aliases") or {}
    source = str(scoring.get("signal_source") or "switching_signal")
    today = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()

    try:
        plans = plan_rescore(rows, scoring, aliases, today, signal_source=source)
    except RescoreRefused as exc:
        print(f"\nREFUSED: {exc}")
        return 1

    new = [int(p["score"]) for p in plans]
    _hist([int(p["old_score"] or 0) for p in plans], "BEFORE — stored scores")
    _hist(new, "AFTER — this config")

    in_band = [p for p in plans if EMPTY_LOW <= int(p["score"]) <= EMPTY_HIGH]
    print(
        f"\nempty band {EMPTY_LOW}-{EMPTY_HIGH}: {len(in_band)}"
        + ("   *** INVARIANT BROKEN ***" if in_band else "   OK")
    )
    print("lanes:", ", ".join(f"{k}={v}" for k, v in sorted(Counter(str(p.get('lane')) for p in plans).items())))
    print(f"at or above floor: {sum(1 for s in new if s >= 80)} of {len(new)}")

    # Which pipeline stages the qualified leads came from — the check that catches a
    # window_stages list including a rung where the deal is already lost.
    by_stage = Counter(
        str(next((r.get("pipeline_status") for r in rows if r["id"] == p["id"]), None))
        for p in plans
        if int(p["score"]) >= 80
    )
    print("\nqualified leads by pipeline stage:")
    for stage, n in by_stage.most_common():
        print(f"   {stage:28} {n}")

    print("\nsummary:", json.dumps(summarise(plans), default=str)[:320])
    print("\nNothing written; no grounded request made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
