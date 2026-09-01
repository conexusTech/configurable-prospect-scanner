"""Re-judge stored prospects, then score them under the gated model. Writes nothing.

Completes the offline chain that `measure_signal_yield.py` starts:

    validation  ->  judgement  ->  scoring
    (grounded)      (ungrounded)   (free, pure)

🔑 **Why re-judging changes the answer, not just the prose.** The stage a prospect sits in
is HALF of gate 2 — `in_buying_window` needs both an active stage AND a fresh signal. A
scoring pass that reuses stored stages is therefore provisional: it answers "what would
these scores be if the staging never moved", which is not the question.

🔴 **It takes a runtime CONTEXT, not a bare config, and that is load-bearing twice over.**
Both defects it prevents were live in this file's first version, and each reported a clean,
believable, wrong number:

  1. `allowed_states` is usually `{"context_ref": "home_markets"}`. Scored unresolved, G1
     compares a state string to a dict, fails for every lead, and the whole book lands in
     `neither` — a confident 0-qualified that looks like a scoring verdict and is a harness
     bug. `aeo/context_refs.resolve` is what the scanner itself calls at `runner.py:599`.
  2. The stage vocabulary is NOT `config.scoring.pipeline` (that is a weight, `{max: N}`).
     `judge_prospects` returns `{}` outright when it gets no `stages` — silently, so the
     run prints "judged 0 of N" and then scores on stored stages anyway. The vocabulary
     comes from `build_tool_context`, which merges the engine defaults with the config.

Both are the same lesson: derive every input from the real code path, never from a
plausible-looking reconstruction of it.

🔴 **Touches no database.** Rows and context come from files, results go to a file. Whether
any of it is persisted stays a separate decision with a separate tool — the standing rule is
forward-only, and a harness that quietly wrote would be deciding that on its own.

⚠️ Costs money: one UNGROUNDED model call per batch for judgement. Cheaper than validation.

Usage
-----
    python scripts/rejudge_and_score.py --rows rows.json --context context.json \\
        [--verdicts verdicts.json] --out judged.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import av_lead_scanner as als  # noqa: E402
from aeo.config_mapping import build_tool_context  # noqa: E402
from aeo.context_refs import UnresolvedRefError  # noqa: E402
from aeo.context_refs import resolve as resolve_refs  # noqa: E402
from aeo.gated_score import score as gated_score  # noqa: E402
from aeo.phases.ai_judgment import judge_prospects  # noqa: E402
from aeo.signal_class import classify  # noqa: E402

EMPTY_LOW, EMPTY_HIGH = 46, 79


def _signals_as_production_sees_them(
    row: dict[str, Any], source: str
) -> list[dict[str, Any]]:
    """Mirror `av_lead_scanner._gated_total` exactly — including the classify step.

    🔑 Two things here are easy to leave out and neither announces itself.

    `signal_class` is attached at enrichment only from 2026-08-31, so every row written
    before that carries the free text alone. Production re-derives it per row; a harness
    that skips the step hands the bonus bands an unclassified signal, they match nothing,
    and the lead loses its signal-strength points — a lower score that is entirely legal
    and looks like a scoring result rather than a missing line of harness code.

    The gate also reads `validation_data` and **never** `discovery_data`. Advertising's
    36 dated `signal_type`/`signal_date` pairs live in the latter, so they are invisible
    here by design, not by oversight — do not "fix" that by widening the lookup.
    """
    validation = row.get("validation_data")
    raw = (validation or {}).get(source) if isinstance(validation, dict) else None
    return [
        dict(s, signal_class=s.get("signal_class") or classify(s.get("signal_type")))
        for s in (raw or [])
        if isinstance(s, dict)
    ]


def _aliases_as_production_sees_them(scoring: dict[str, Any]) -> dict[str, str]:
    """The gate's own table, falling back to `region_bonus`'s — as production does.

    A skill that dropped `region_bonus` (reasonable, since the gate replaces it) would
    otherwise lose state normalisation entirely and fail G1 closed for every lead.
    """
    target = (scoring.get("gate") or {}).get("target_market") or {}
    return target.get("state_aliases") or (
        (scoring.get("region_bonus") or {}).get("state_aliases") or {}
    )


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


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    """Lift `discovery_data.by_source.*` onto the lead, as scoring does at run time.

    `employee_count` and the like live there, not in a column. Without this the size band
    reads nothing and every prospect scores the unknown midpoint.
    """
    flat = dict(row)
    by_source = (row.get("discovery_data") or {}).get("by_source")
    if isinstance(by_source, dict):
        for src in by_source.values():
            if isinstance(src, dict):
                for k, v in src.items():
                    if v is not None and str(v).strip() and k not in flat:
                        flat[k] = v
    return flat


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", required=True)
    ap.add_argument("--context", required=True, help="runtime context JSON (carries skill.config)")
    ap.add_argument(
        "--verdicts",
        help="output of measure_signal_yield.py. When given, these REPLACE each row's "
        "stored validation_data — which is the point for a vertical whose stored signals "
        "carry no dates. Omit for a book that already has them.",
    )
    ap.add_argument("--out", required=True)
    ap.add_argument("--as-of", help="YYYY-MM-DD, default today")
    ap.add_argument("--provider", default="gemini")
    ap.add_argument(
        "--stages-from",
        help="a previous --out file. Reuses its judged stages instead of re-judging, so "
        "a scoring fix costs nothing. Judgement is the only paid step here; re-buying it "
        "to correct arithmetic downstream of it would be waste, not rigour.",
    )
    args = ap.parse_args()

    _load_env()
    rows = _read(args.rows, "--rows")
    ctx = _read(args.context, "--context")
    today = args.as_of or date.today().isoformat()

    if args.verdicts:
        fresh = {
            v["prospect_id"]: v["validation_data"] for v in _read(args.verdicts, "--verdicts")
        }
        replaced = 0
        for r in rows:
            if r["id"] in fresh:
                r["validation_data"] = fresh[r["id"]]
                replaced += 1
        print(f"  replaced validation_data on {replaced} of {len(rows)} rows")

    raw_config = (ctx.get("skill") or {}).get("config") or {}
    try:
        config = resolve_refs(raw_config, ctx)
    except UnresolvedRefError as exc:
        print(f"UNRESOLVED BINDING: {exc}\nThe scanner would fail the same way.")
        return 2

    scoring = config.get("scoring") or {}
    if str(scoring.get("model") or "").lower() != "gated":
        raise SystemExit("config is not `model: gated` — nothing to score against")

    gate = (scoring.get("gate") or {}).get("target_market") or {}
    allowed = gate.get("allowed_states")
    if not isinstance(allowed, list) or not allowed:
        raise SystemExit(f"allowed_states did not resolve to a list: {allowed!r}")
    print(f"  allowed_states resolved -> {allowed}")

    # The vocabulary the runner judges against — engine defaults merged with the config.
    ctx_for_build = json.loads(json.dumps(ctx))
    ctx_for_build.setdefault("skill", {})["config"] = config
    tool_context = build_tool_context(ctx_for_build)
    pipeline = tool_context.get("pipeline") or {}
    stages = [s for s in (pipeline.get("stages") or []) if isinstance(s, dict) and s.get("key")]
    if not stages:
        raise SystemExit(
            "no pipeline stages in the built tool context — judge_prospects would "
            "return {} silently and every stage would fall back to the stored one"
        )
    print(f"  pipeline vocabulary     -> {len(stages)} stages")

    if args.stages_from:
        prior = _read(args.stages_from, "--stages-from")
        judged = {
            r["id"]: {"pipeline_status": r["stage_after"], "ai_analysis": r.get("ai_analysis")}
            for r in prior
            if r.get("stage_from_model")
        }
        print(f"  reusing {len(judged)} judged stages — no model call, nothing spent")
    else:
        provider = als._pick_provider(args.provider, mock=False, dry_run=False)
        pconf = als._provider_config(tool_context, args.provider)
        print(f"\n{len(rows)} prospects · judging (ungrounded) then scoring (free)\n")
        judged = judge_prospects(
            rows,
            pipeline=pipeline,
            product_description=str(config.get("product_description") or ""),
            today=today,
            provider=provider,
            provider_config=pconf,
            parse_json_array=als.parse_json_array,
        )
        print(f"  judged {len(judged)} of {len(rows)}")
    if not judged:
        raise SystemExit("judgement returned nothing — refusing to report scores on stale stages")

    aliases = _aliases_as_production_sees_them(scoring)
    source = str(scoring.get("signal_source") or "switching_signal")
    as_of = date.fromisoformat(today)
    # Production scores against `{**scoring, "score_cap": cap}`, cap being the resolved
    # `score_cap`. Passing `scoring` bare would silently drop a config that set its own.
    cap = int(scoring.get("score_cap", als._DEFAULT_SCORING["score_cap"]))
    scoring_capped = {**scoring, "score_cap": cap}

    out: list[dict[str, Any]] = []
    for row in rows:
        lead = _flatten(row)
        j = judged.get(row["id"]) or {}
        # A prospect the model did not judge is ABSENT from the result, never a guess —
        # so the stored stage is the fallback, and it is recorded as one.
        stage = j.get("pipeline_status") or row.get("pipeline_status")
        signals = _signals_as_production_sees_them(row, source)
        bd = gated_score(lead, signals, stage, scoring_capped, aliases, as_of)
        out.append(
            {
                "id": row["id"],
                "company_name": row.get("company_name"),
                "old_score": row.get("score"),
                "score": bd["total"],
                "lane": bd["lane"],
                "stage_before": row.get("pipeline_status"),
                "stage_after": stage,
                "stage_from_model": bool(j.get("pipeline_status")),
                "gates": bd.get("gates"),
                "ai_analysis": j.get("ai_analysis"),
            }
        )

    io.open(args.out, "w", encoding="utf-8").write(json.dumps(out, indent=1))

    new = [r["score"] for r in out]
    qualified = [r for r in out if r["score"] >= 80]
    in_band = [r for r in out if EMPTY_LOW <= r["score"] <= EMPTY_HIGH]
    moved = [r for r in out if r["stage_before"] != r["stage_after"]]
    fell_back = [r for r in out if not r["stage_from_model"]]
    print(f"\n  qualified (>= 80)   : {len(qualified)} of {len(out)}")
    print(f"  in the empty band   : {len(in_band)}" + ("   *** BROKEN ***" if in_band else "   OK"))
    print(f"  stage CHANGED       : {len(moved)}")
    print(f"  stage fell back     : {len(fell_back)} (model returned no verdict)")
    print("  lanes               : " + ", ".join(
        f"{k}={v}" for k, v in sorted(Counter(r["lane"] for r in out).items())))
    print(f"  score range         : {min(new)}-{max(new)}")
    print(f"\n  saved -> {args.out}")
    print("\nNothing was written to any database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
