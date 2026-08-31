"""A/B the validation phase at batch 1 vs batch N, judged on CORRECTNESS.

§6 of the bundling ruling permits batching a hard-disqualifier signal only after
*"A/B-ing classification accuracy against a known sample — judged on correctness, never
on row count."* This is that A/B.

**What is at stake.** Batching validation is worth 111 grounded calls on the reference
run — 20% of it, taking the total saving from 35% to 55%. It is not taken by default
because a false verdict here REMOVES the lead, and `validation.py` says the failure mode
plainly:

    "Recording an unjudged prospect as invalid would silently shrink every result set,
    and nothing would look wrong."

A degraded enrichment call returns fewer rows — visible. A degraded validation call
returns fewer PROSPECTS — indistinguishable from a thin market.

**The bar this measures.** Not "did the batch return the same number of rows" — that is
exactly the row-count judgement §6 forbids. It measures whether each prospect gets the
SAME VERDICT, and reports every disagreement individually so a human can read them.

**Cost.** `n` calls at batch 1, plus `ceil(n / batch)` at batch N. For the default n=20,
batch=8: 23 grounded calls, a few dollars.

Usage
-----
    python scripts/validation_batch_ab.py --rows sample.json [--batch 8] [--repeat 1]

`--rows` is a JSON array of prospect dicts, each needing at least `id`, `company_name`,
`city`, `state` and ideally `discovery_data`. `scripts/` has no fixture on purpose: the
sample must be YOUR prospects, because §6's bar is a *known* sample and a synthetic one
proves nothing about a real vertical.

Exit code is 0 when the arms agree on every prospect, 1 when they do not — so CI can gate
on it if the constant is ever raised.
"""

from __future__ import annotations

import argparse
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
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def _verdicts(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    provider: Any,
    provider_config: dict[str, Any],
    batch: int,
) -> dict[str, dict[str, Any]]:
    out = validate_prospects(
        rows,
        validation_config=config,
        provider=provider,
        provider_config=provider_config,
        parse_json_array=als.parse_json_array,
        batch_size=batch,
    )
    return {r["prospect_id"]: r["validation_data"] for r in out}


def _compare(
    control: dict[str, dict[str, Any]],
    variant: dict[str, dict[str, Any]],
    rows: list[dict[str, Any]],
) -> tuple[int, list[str]]:
    """Agreement on the DECISION, plus a readable line per disagreement."""
    names = {r["id"]: r.get("company_name") or r["id"] for r in rows}
    agree = 0
    notes: list[str] = []
    for pid in control:
        a, b = control[pid], variant.get(pid, {})
        if a.get("validated") == b.get("validated"):
            agree += 1
            # Same decision but different grounds is worth seeing, not failing on.
            if set(a.get("disqualifiers_hit") or []) != set(
                b.get("disqualifiers_hit") or []
            ):
                notes.append(
                    f"  ~ {names[pid]}: same verdict, different disqualifiers\n"
                    f"      batch1: {a.get('disqualifiers_hit')}\n"
                    f"      batchN: {b.get('disqualifiers_hit')}"
                )
            continue
        notes.append(
            f"  X {names[pid]}: validated {a.get('validated')!r} -> "
            f"{b.get('validated')!r}\n"
            f"      batch1 reasoning: {str(a.get('reasoning'))[:160]}\n"
            f"      batchN reasoning: {str(b.get('reasoning'))[:160]}"
        )
    return agree, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", required=True, help="JSON array of prospect dicts")
    ap.add_argument("--config", help="JSON file with the skill's `validation` section")
    ap.add_argument("--batch", type=int, default=8, help="variant batch size")
    ap.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="run both arms N times; the model is not deterministic, so a single "
        "disagreement may be noise and N>1 separates noise from a real shift",
    )
    ap.add_argument("--provider", default="gemini")
    args = ap.parse_args()

    _load_env()

    def _read_json(path: str, label: str) -> Any:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"{label} not found: {path}", file=sys.stderr)
            raise SystemExit(2)
        except json.JSONDecodeError as exc:
            print(f"{label} is not valid JSON ({path}): {exc}", file=sys.stderr)
            raise SystemExit(2)

    rows = _read_json(args.rows, "--rows")
    if not isinstance(rows, list) or not rows:
        print("--rows must be a non-empty JSON array of prospect dicts", file=sys.stderr)
        return 2
    missing = [i for i, r in enumerate(rows) if not isinstance(r, dict) or not r.get("id")]
    if missing:
        # `validate_prospects` silently skips a prospect with no id, so a sample with
        # one would quietly shrink the arm and make the agreement percentage a
        # different denominator than it looks.
        print(
            f"--rows entries {missing[:5]} have no `id` — every prospect needs one or "
            "the arms compare different sets",
            file=sys.stderr,
        )
        return 2
    config = _read_json(args.config, "--config") if args.config else {}

    provider = als._pick_provider(args.provider, mock=False, dry_run=False)
    pconf = als._provider_config({}, args.provider)

    calls_control = len(rows) * args.repeat
    calls_variant = -(-len(rows) // args.batch) * args.repeat
    print(
        f"sample {len(rows)} prospects · batch 1 vs {args.batch} · {args.repeat} run(s)\n"
        f"grounded calls: {calls_control} control + {calls_variant} variant = "
        f"{calls_control + calls_variant}\n"
    )

    totals = Counter()
    all_notes: list[str] = []
    for run in range(1, args.repeat + 1):
        control = _verdicts(rows, config, provider, pconf, 1)
        variant = _verdicts(rows, config, provider, pconf, args.batch)
        agree, notes = _compare(control, variant, rows)
        totals["agree"] += agree
        totals["total"] += len(control)
        # An unjudged prospect in the VARIANT is the specific harm: batch 1 judged it,
        # batch N dropped it, and a dropped prospect leaves the run.
        totals["variant_unjudged"] += sum(
            1
            for pid, v in variant.items()
            if v.get("validated") is None and control.get(pid, {}).get("validated") is not None
        )
        if args.repeat > 1:
            print(f"run {run}: {agree}/{len(control)} agree")
        all_notes.extend(notes)

    pct = 100.0 * totals["agree"] / max(1, totals["total"])
    print(f"\nAGREEMENT: {totals['agree']}/{totals['total']}  ({pct:.1f}%)")
    print(
        f"PROSPECTS THE BATCH LEFT UNJUDGED (control judged them): "
        f"{totals['variant_unjudged']}"
    )
    if all_notes:
        print("\ndisagreements and grounds-differences:")
        print("\n".join(all_notes))

    ok = totals["agree"] == totals["total"] and totals["variant_unjudged"] == 0
    print(
        "\nVERDICT: "
        + (
            f"PASS — raise DEFAULT_VALIDATION_BATCH to {args.batch}."
            if ok
            else "FAIL — leave DEFAULT_VALIDATION_BATCH at 1. The saving is not worth a "
            "verdict that moved."
        )
    )
    print(
        "\nRead the disagreements before acting on the percentage. §6 judges this on "
        "correctness: one lead wrongly removed is a worse outcome than 111 calls is a "
        "good one, and the percentage cannot tell you which lead."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
