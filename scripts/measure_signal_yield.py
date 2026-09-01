"""Re-run VALIDATION ONLY against stored prospects, to measure dated-signal yield.

The question this answers, per vertical: **now that the validator asks for a date, can the
model actually find one?** That is the gate on stage 5 — a skill whose signals never date
cannot be gated, because G2 is a recency test and every lead would fail it, capping the
book at 45 rather than improving it.

**Why validation-only rather than a scan.** A full run pays for discovery and geography
first — on the reference shape those are 63 of 81 grounded calls, roughly 69% of the spend
— and then discovers *new* prospects whose dedup behaviour muddies the sample. Validation
takes rows, so pointing it at an existing book skips all of that and measures the customers
the org already has.

🔴 **It touches no database.** No connection exists in this file, so it cannot alter a stored
prospect however it is invoked. That matters beyond tidiness — the standing rule is
forward-only ("existing run results are never touched"), and a harness that measured by
updating `validation_data` would quietly re-score the book it was only meant to inspect.

`--out` saves the verdicts to a FILE. Persisting them is not a step toward writing — it is
what stops the write costing twice. Each verdict is a paid grounded call; discarding them
means re-buying every one when the decision is made to persist. Deciding whether to write
stays a separate act, with a separate tool, and a separate approval.

⚠️ It DOES spend money: one grounded call per prospect at batch 1. `--limit` samples; pass
`--limit 0` for a whole book, which is the right call when the same verdicts will be reused.

Usage
-----
    python scripts/measure_signal_yield.py --rows rows.json --config config.json \
        --limit 0 --out verdicts.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import av_lead_scanner as als  # noqa: E402
from aeo.phases.validation import validate_prospects  # noqa: E402


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _read(path: str, label: str) -> Any:
    try:
        return json.loads(io.open(path, encoding="utf-8").read())
    except FileNotFoundError:
        raise SystemExit(f"{label} not found: {path}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", required=True, help="JSON array of stored prospects")
    ap.add_argument("--config", required=True, help="the skill config JSON")
    ap.add_argument("--limit", type=int, default=20, help="sample size (0 = all)")
    ap.add_argument("--provider", default="gemini")
    ap.add_argument(
        "--out",
        help="save the verdicts here. One paid pass then serves BOTH the measurement and "
        "a later write — without it, re-validating to persist means paying twice for "
        "calls already made.",
    )
    args = ap.parse_args()

    _load_env()
    rows = _read(args.rows, "--rows")
    config = _read(args.config, "--config")
    if not isinstance(rows, list) or not rows:
        raise SystemExit("--rows must be a non-empty JSON array")
    if args.limit:
        rows = rows[: args.limit]

    validation = config.get("validation") or {}
    provider = als._pick_provider(args.provider, mock=False, dry_run=False)
    pconf = als._provider_config({}, args.provider)

    print(
        f"{len(rows)} prospects · one grounded call each · provider {args.provider}\n"
        f"nothing will be written\n"
    )

    out = validate_prospects(
        rows,
        validation_config=validation,
        provider=provider,
        provider_config=pconf,
        parse_json_array=als.parse_json_array,
        # 🔑 A heartbeat. Without one a slow phase and a wedged one are the same
        # observation from outside — `map_bounded`'s own docblock says so.
        log=lambda m: print(f"    {m}", flush=True),
    )

    judged = [r for r in out if (r.get("validation_data") or {}).get("validated") is not None]
    with_sig, with_date, dated_rows = 0, 0, []
    types: Counter[str] = Counter()
    for r in out:
        sigs = (r.get("validation_data") or {}).get("signals_found") or []
        if sigs:
            with_sig += 1
        dated = [s for s in sigs if isinstance(s, dict) and s.get("signal_date")]
        if dated:
            with_date += 1
            dated_rows.append((r["prospect_id"], dated[0]))
        for s in sigs:
            if isinstance(s, dict) and s.get("signal_type"):
                types[str(s["signal_type"])] += 1

    if args.out:
        io.open(args.out, "w", encoding="utf-8").write(json.dumps(out, indent=1))
        print(f"\nverdicts saved -> {args.out} ({len(out)} rows)")

    n = len(out)
    pct = (100.0 * with_date / n) if n else 0.0
    print(f"\nprospects judged        : {len(judged)} of {n}")
    print(f"carrying any signal     : {with_sig}")
    print(f"carrying a DATED signal : {with_date}  ({pct:.0f}%)")
    print(f"\nsignal_type vocabulary the model chose (from the seller's own list):")
    for t, c in types.most_common(8):
        print(f"   {t:32} {c}")

    print("\nsample dated signals:")
    for pid, s in dated_rows[:5]:
        name = next((r.get("company_name") for r in rows if r["id"] == pid), pid)
        print(f"   {str(name)[:30]:30} {s.get('signal_date')}  {str(s.get('signal_type'))[:24]}")

    print(
        "\nverdict: "
        + (
            f"GATEABLE — {pct:.0f}% dated (healthcare, which works, is 43%)"
            if pct >= 20
            else f"NOT gateable on this evidence — {pct:.0f}% dated. Gating would fail G2 "
            "for nearly every lead and cap the book at 45."
        )
    )
    print("\nNothing was written. No database connection was opened.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
