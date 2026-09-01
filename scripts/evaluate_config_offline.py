"""Score STORED prospects under a candidate config, spending nothing.

Tier 1 of the verification strategy. `aeo/rescore.py` is deliberately the pure half — "no
database layer and no CLI, so it cannot be executed against real data even by accident".
This harness keeps that intact: it reads ROWS AND CONFIG FROM FILES and never opens a
connection. Export them with `aeo-backend/.temp/verify/export-rows.js`, which does the
SELECT where a driver already exists.

🔑 **Why run this before paying for anything.** Re-scoring makes ZERO grounded requests —
the inputs are the `validation_data` and `discovery_data` the original scan already paid
for. Reference run `741b7b3b` cost $58.08 across 223 calls and 3,183 grounded queries;
scoring those same rows again costs none of it. So "did the scoring change work, and did
quality regress" is answerable for free, and only bundling needs a paid run.

**What it can and cannot tell you.**
  can:    the gated arithmetic on real rows · whether 46-79 stays empty · how the
          distribution moves between two configs over the SAME prospects
  cannot: anything about discovery quality, enrichment content, or bundling — those are
          claims about calls this makes none of

⚠️ Only a vertical whose scanner emits DATED signals can be evaluated. The four legacy
skills carry `signals_found` as undated strings, so `plan_rescore` refuses them rather
than reporting a book that fails G2 for a reason the config did not cause.

**It never writes.** There is no UPDATE here and no connection to put one through.

Usage
-----
    python scripts/evaluate_config_offline.py --rows rows.json --config config.json
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

from aeo.rescore import RescoreRefused, plan_rescore, summarise  # noqa: E402

EMPTY_LOW, EMPTY_HIGH = 46, 79


def _load(path: str, label: str) -> Any:
    try:
        return json.loads(io.open(path, encoding="utf-8").read())
    except FileNotFoundError:
        raise SystemExit(f"{label} not found: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} is not valid JSON ({path}): {exc}")


def _histogram(scores: list[int], label: str) -> None:
    buckets = [(0, 19), (20, 39), (40, 45), (EMPTY_LOW, EMPTY_HIGH), (80, 89), (90, 100)]
    print(f"\n{label}")
    for lo, hi in buckets:
        n = sum(1 for s in scores if lo <= s <= hi)
        bar = "#" * min(n, 40)
        flag = "   <-- MUST BE EMPTY" if (lo, hi) == (EMPTY_LOW, EMPTY_HIGH) and n else ""
        print(f"   {lo:3}-{hi:3}  {bar:<40} {n}{flag}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", required=True, help="JSON array from export-rows.js")
    ap.add_argument("--config", required=True, help="skill config JSON")
    ap.add_argument("--as-of", help="YYYY-MM-DD; defaults to today")
    ap.add_argument("--show", type=int, default=8)
    args = ap.parse_args()

    rows = _load(args.rows, "--rows")
    config = _load(args.config, "--config")
    if not isinstance(rows, list) or not rows:
        raise SystemExit("--rows must be a non-empty JSON array")

    scoring = (config or {}).get("scoring") or {}
    gate = (scoring.get("gate") or {}).get("target_market") or {}
    aliases = gate.get("state_aliases") or (
        (scoring.get("region_bonus") or {}).get("state_aliases") or {}
    )
    source = str(scoring.get("signal_source") or "switching_signal")
    today = (
        datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of else date.today()
    )

    print(f"{len(rows)} prospects · model={scoring.get('model')} "
          f"floor={scoring.get('floor')} bonus.max={(scoring.get('bonus') or {}).get('max')} "
          f"signal_source={source} · as of {today}")

    try:
        plans = plan_rescore(rows, scoring, aliases, today, signal_source=source)
    except RescoreRefused as exc:
        print(f"\nREFUSED: {exc}")
        return 1

    old = [int(p["old_score"] or 0) for p in plans]
    new = [int(p["score"]) for p in plans]
    _histogram(old, "BEFORE — stored scores")
    _histogram(new, "AFTER — this config")

    in_band = [p for p in plans if EMPTY_LOW <= int(p["score"]) <= EMPTY_HIGH]
    print(
        f"\nempty band {EMPTY_LOW}-{EMPTY_HIGH}: {len(in_band)} prospects"
        + ("   *** INVARIANT BROKEN ***" if in_band else "   OK")
    )
    lanes = Counter(str(p.get("lane")) for p in plans)
    print("lanes:", ", ".join(f"{k}={v}" for k, v in sorted(lanes.items())))
    print(f"at or above floor: {sum(1 for s in new if s >= 80)} of {len(new)}")

    # ⚠️ `plan_rescore` does NOT return a band, despite its docstring listing one in
    # `{id, old_score, score, lane, band, breakdown}`. The LIVE engine computes
    # `priority_band(score, bands)` separately and ships it through SCORED_PASSTHROUGH,
    # so a real run does label these — the planning function simply does not. Computing
    # it here from the same config is what a run would show, and it is the only way to
    # see whether the band ladder and the gated scale actually agree.
    bands = scoring.get("priority_bands") or []

    def band_of(score: int) -> str:
        for b in bands:
            lo, hi = b.get("min"), b.get("max")
            if lo is not None and hi is not None and int(lo) <= score <= int(hi):
                return str(b.get("label"))
        return "(no band)"

    if bands:
        print("\nbands a run would assign, from scoring.priority_bands:")
        for label, n in Counter(band_of(int(p["score"])) for p in plans).most_common():
            print(f"   {label:26} {n}")
        floor_val = int(scoring.get("floor") or 80)
        for b in bands:
            lo, hi = b.get("min"), b.get("max")
            if lo is None or hi is None:
                continue
            if int(lo) < floor_val <= int(hi):
                print(
                    f"   NOTE: band '{b.get('label')}' spans {lo}-{hi}, straddling the "
                    f"floor {floor_val}. Its lower half is inside the structurally-empty "
                    f"band, so the label reads as a range far wider than it can occupy"
                )

    names = {r["id"]: (r.get("company_name") or r["id"]) for r in rows}
    movers = sorted(plans, key=lambda p: abs(int(p["score"]) - int(p["old_score"] or 0)))
    print(f"\nbiggest movers:")
    for p in list(reversed(movers))[: args.show]:
        print(
            f"   {str(names.get(p['id']))[:38]:38} "
            f"{int(p['old_score'] or 0):3} -> {int(p['score']):3}"
            f"   lane={p.get('lane')} band={band_of(int(p['score']))}"
        )

    print("\nsummary:", json.dumps(summarise(plans), default=str)[:400])
    print("\nNothing was written; no connection was opened; no grounded request was made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
