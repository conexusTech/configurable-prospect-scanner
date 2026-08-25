"""Fidelity gate: reproduce a real published run using only config.

Phase 6.2 + 6.3 of the EAP-parity work. The source artifact
(`tests/fixtures/eap-parity/`) is a config authored for a sibling engine, plus the
scored output of its second test run. This asserts our engine reproduces that output
**from configuration alone, with no EAP vocabulary in engine code.**

Two things make this a real gate rather than a restatement of the implementation:

1. **Inputs come out of the published artifact**, parsed as CSV — not hand-transcribed.
   If the fixture changes, this test changes with it.
2. **The stale signals are fed back IN.** The expected output lists them in its `Flags`
   column as `stale-signal-excluded`, meaning the reference harness dropped them before
   scoring. Handing our engine a pre-filtered set would test nothing about recency, so
   every excluded signal is put back and the engine has to drop it itself. That is the
   difference between checking arithmetic and checking the recency gate.

⚠️ Per D1 the contract is **ranking + band parity**, not digit-for-digit scores — an
exact-score gate would freeze arithmetic the reference run itself revised once, and would
forbid ever enabling the AI adjustment layer. The per-factor assertions below are
therefore the strict part, and they are stricter than the totals: a compensating pair of
errors can produce a right total, but not a right breakdown.

The reference run recorded `AI Adjustment` as empty on every row ("AI adjustment layer
intentionally NOT run"), so no adjustment is supplied here.
"""
from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path

import av_lead_scanner as als

FIXTURE = Path(__file__).parent / "fixtures" / "eap-parity" / "expected-v2"
SCAN_DATE = date(2026, 7, 20)

# ── the config, expressed entirely in OUR vocabulary ──────────────────────────
# Every number here comes from the source config's own tables. The engine knows none of
# the words: `standalone-recent` is a config value, `county` is a keyword, and the size
# curve is a list of thresholds.

SIZE_TIERS = [
    {"threshold": 5000, "points": 10},
    {"threshold": 1000, "points": 18},
    {"threshold": 500, "points": 20},
    {"threshold": 200, "points": 18},
    {"threshold": 100, "points": 10},
    {"threshold": 0, "points": 0},
]

VERTICAL_KEYWORDS = [
    {"name": "Local Government / Public Sector", "points": 20,
     "keywords": ["county", "municipality", "city government", "town",
                  "school district", "housing authority", "utility authority",
                  "public sector", "government"]},
    {"name": "Healthcare Systems", "points": 18,
     "keywords": ["hospital", "health system", "medical center", "healthcare",
                  "long-term care", "behavioral health"]},
    {"name": "Education", "points": 16,
     "keywords": ["university", "college", "k-12", "education", "charter school"]},
    {"name": "First Responders", "points": 16,
     "keywords": ["police", "sheriff", "fire department", "ems", "public safety"]},
    {"name": "Manufacturing", "points": 14,
     "keywords": ["manufacturing", "industrial", "factory"]},
    {"name": "Nonprofits", "points": 12, "keywords": ["nonprofit", "non-profit"]},
    {"name": "Professional Services", "points": 10,
     "keywords": ["professional services", "consulting", "law firm", "accounting"]},
]

INCUMBENT_KEYWORDS = [
    {"keyword": "bundled", "points": 20},
    {"keyword": "none", "points": 14},
    {"keyword": "unknown", "points": 8},
    {"keyword": "standalone", "points": 6},
    # Present so the table is complete; the hard rule fires first. Longest-match means
    # this cannot be shadowed by `standalone` regardless of ordering.
    {"keyword": "standalone-recent", "points": 0},
]

TITLE_KEYWORDS = {
    "vice president, human resources": 15,
    "vice president human resources": 15,
    "vp human resources": 15,
    "vp of human resources": 15,
    "director of human resources": 15,
    "human resources director": 15,
    "chief human resources officer": 14,
    "chief people officer": 14,
    "benefits manager": 13,
    "benefits administrator": 13,
    "total rewards director": 12,
    "county manager": 12,
    "city manager": 12,
    "human resources manager": 8,
    "hr manager": 8,
    "human resources analyst": 3,
    "hr analyst": 3,
}

BANDS = [
    {"range": [80, 100], "label": "Critical", "action": "Call today"},
    {"range": [60, 79], "label": "High", "action": "Call this week"},
    {"range": [40, 59], "label": "Medium", "action": "Research"},
    {"range": [20, 39], "label": "Low", "action": "Nurture"},
    {"range": [0, 19], "label": "Skip", "action": "Do not pursue"},
]

SCORING = {
    "score_cap": 100,
    "factors_max": 100,
    # The axes this vertical does not author. `region_bonus` at 0 is load-bearing:
    # delivery is remote nationwide, so location must never affect fit.
    "completeness": {"max": 0},
    "fit": {"max": 0},
    "region_bonus": {"max": 0},
    "multi_source": {"max": 0},
    "pipeline": {"max": 0},
    "priority_bands": BANDS,
    "disqualify_rules": [
        {
            "key": "locked_in",
            "source_field": "incumbent_type",
            "keywords": ["standalone-recent"],
            "reason": "Standalone EAP contract awarded within ~18 months (standalone-recent)",
        }
    ],
    "factors": [
        {"key": "size_fit", "name": "Size Fit", "weight": 20,
         # Two discovery lanes named the same quantity differently.
         "source_field": ["estimated_headcount", "employee_estimate"],
         "tiers": SIZE_TIERS},
        {"key": "vertical_fit", "name": "Vertical Fit", "weight": 20,
         "source_field": ["industry", "entity_type"], "keywords": VERTICAL_KEYWORDS},
        {"key": "in_market_signals", "name": "In-Market Signals", "weight": 25,
         "source_field": "signals", "base_points": 10, "bonus_points": 15,
         "bonus_keywords": ["rfp", "rfq", "solicitation", "procurement",
                            "invitation to bid"],
         "date_field": "signal_date", "recency_months": 12},
        {"key": "incumbent_status", "name": "Incumbent EAP Status", "weight": 20,
         "source_field": "incumbent_type", "keywords": INCUMBENT_KEYWORDS},
        {"key": "decision_maker", "name": "Decision Maker", "weight": 15,
         "source_field": "contact_title", "keywords": TITLE_KEYWORDS},
    ],
}

# Column → factor key, for the per-factor assertions.
EXPECTED_COLUMNS = {
    "size_fit": "Size Fit (max 20)",
    "vertical_fit": "Vertical Fit (max 20)",
    "in_market_signals": "In-Market Signals (max 25)",
    "incumbent_status": "Incumbent Status (max 20)",
    "decision_maker": "Decision Maker (max 15)",
}
FACTOR_WEIGHT = {"size_fit": 20, "vertical_fit": 20, "in_market_signals": 25,
                 "incumbent_status": 20, "decision_maker": 15}

_DATED = re.compile(r"([^;(]+?)\s*\((\d{4}-\d{2}-\d{2})\)")
_STALE = re.compile(r"stale-signal-excluded:\s*([^;]+?)\s+(\d{4}-\d{2}-\d{2})")
_TITLE = re.compile(r"\(([^)]+)\)")


def _rows() -> list[dict[str, str]]:
    """The published scored output, parsed AS CSV.

    Not split on newlines: fields here contain commas and quoted text, and counting rows
    by `\\n` has produced three different wrong answers on this project before.
    """
    with (FIXTURE / "scored-prospects.csv").open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _prospect(row: dict[str, str]) -> dict[str, object]:
    """Rebuild the engine's input from the published row."""
    industry = row["Industry / Entity Type"]
    signals: list[dict[str, str]] = [
        {"signal_type": kind.strip(), "signal_date": when}
        for kind, when in _DATED.findall(row.get("Signal Detail") or "")
    ]
    # 🔑 Put the excluded signals BACK. The reference harness dropped these before
    # scoring; handing our engine a pre-filtered set would test nothing about recency.
    for kind, when in _STALE.findall(row.get("Flags") or ""):
        signals.append({"signal_type": kind.strip(), "signal_date": when})

    title_match = _TITLE.search(row.get("Decision Maker Detail") or "")
    return {
        "id": row["Employer Name"],
        "company_name": row["Employer Name"],
        # Public-sector rows carry the OTHER lane's field names, which is the cross-lane
        # case the factor's `source_field` list exists for.
        **(
            {"employee_estimate": row["Estimated Headcount"], "entity_type": industry}
            if "1B" in row["Source Phase"]
            else {"estimated_headcount": row["Estimated Headcount"], "industry": industry}
        ),
        "signals": signals,
        "incumbent_type": row.get("Incumbent Type") or "",
        "contact_title": title_match.group(1) if title_match else "",
    }


def _scored() -> dict[str, dict]:
    rows = _rows()
    out = als.score_prospects(
        [_prospect(r) for r in rows], {"scoring": SCORING}, today=SCAN_DATE
    )
    return {item["company_name"]: item for item in out}


class TestPerFactorFidelity:
    """Stricter than the totals: a compensating pair of errors cannot pass this."""

    def test_every_factor_of_every_qualified_prospect_matches(self):
        scored = _scored()
        checked = 0
        for row in _rows():
            if row["Disqualified"] == "YES":
                continue
            item = scored[row["Employer Name"]]
            detail = item["score_factors"]["factors"]
            for key, column in EXPECTED_COLUMNS.items():
                expected_points = float(row[column])
                # Credit is a fraction of the factor's weight; the published column is
                # the points. Compare in points so a weight change cannot hide here.
                got_points = round(detail[key] * FACTOR_WEIGHT[key], 3)
                assert got_points == expected_points, (
                    f"{row['Employer Name']} {key}: expected {expected_points} points, "
                    f"got {got_points} (credit {detail[key]})"
                )
                checked += 1
        # Guard against a silently empty loop — 6 prospects x 5 factors.
        assert checked == 30, f"only {checked} factor assertions ran"

    def test_the_recency_gate_is_what_drops_the_stale_signals(self):
        """The two rows whose only procurement notice is out of window.

        Both were handed a stale RFP alongside (or instead of) a current signal. If the
        gate were inert, each would earn base + the 15-point procurement bonus.
        """
        scored = _scored()
        # Hall County: stale RFP 2024-05-07 plus a current renewal → base only.
        assert scored["Hall County"]["score_factors"]["factors"]["in_market_signals"] == 0.4
        # City of Alpharetta: a stale RFP and nothing else → nothing.
        assert scored["City of Alpharetta"]["score_factors"]["factors"]["in_market_signals"] == 0.0


class TestRankingAndBandParity:
    """The D1 contract."""

    def test_the_ranking_matches_the_published_order(self):
        scored = _scored()
        published = [r["Employer Name"] for r in _rows()]
        ours = sorted(
            published, key=lambda name: (-scored[name]["score"], published.index(name))
        )
        assert ours == published

    def test_every_band_matches(self):
        scored = _scored()
        for row in _rows():
            got = scored[row["Employer Name"]].get("priority_band")
            assert got == row["Priority Band"], (
                f"{row['Employer Name']}: expected band {row['Priority Band']}, got {got}"
            )

    def test_the_totals_also_match_though_only_the_band_is_contractual(self):
        scored = _scored()
        for row in _rows():
            assert scored[row["Employer Name"]]["score"] == int(row["Total Score"])


class TestDisqualification:
    def test_the_ruled_out_prospect_is_zeroed_banded_and_explained(self):
        scored = _scored()
        row = next(r for r in _rows() if r["Disqualified"] == "YES")
        item = scored[row["Employer Name"]]
        assert item["score"] == 0
        assert item["disqualified"] is True
        assert item["priority_band"] == row["Priority Band"] == "Skip"
        assert item["disqualifier_reason"] == row["Disqualifier Reason"]

    def test_it_would_otherwise_have_scored_well(self):
        """Why a score cutoff cannot express this.

        The disqualified row is a 22,000-employee school district — the top vertical band
        and a real size. Remove the rule and it scores on every axis it qualifies for, so
        no threshold on the score could ever have excluded it.
        """
        no_rules = {**SCORING}
        del no_rules["disqualify_rules"]
        row = next(r for r in _rows() if r["Disqualified"] == "YES")
        item = als.score_prospects(
            [_prospect(row)], {"scoring": no_rules}, today=SCAN_DATE
        )[0]
        assert item["score"] > 0
        assert "disqualifier_reason" not in item


class TestTheEngineLearnedNoNouns:
    """I1, as an executable gate rather than a review habit."""

    #: Words from this vertical that must never appear in engine source. Each is real
    #: config vocabulary above, which is exactly why none of it belongs in the code.
    FORBIDDEN = (
        "eap", "employer", "incumbent", "standalone", "bundled", "rfp", "rfq",
        "procurement", "benefits", "headcount", "employee assistance", "wellbeing",
        "school district", "municipality",
    )

    #: Files that ARE the engine. Test files legitimately name the vertical; so do
    #: fixtures and this module.
    ENGINE_FILES = (
        Path(__file__).parent.parent / "av_lead_scanner.py",
        Path(__file__).parent.parent / "aeo" / "runner.py",
        Path(__file__).parent.parent / "aeo" / "event_mapping.py",
        Path(__file__).parent.parent / "aeo" / "config_mapping.py",
        Path(__file__).parent.parent / "aeo" / "phases" / "enrichment.py",
        Path(__file__).parent.parent / "aeo" / "phases" / "validation.py",
        Path(__file__).parent.parent / "aeo" / "phases" / "contacts.py",
    )

    @staticmethod
    def _code_tokens(path: Path) -> list[tuple[str, str, int]]:
        """Identifiers and non-docstring string literals — i.e. the engine's BEHAVIOUR.

        ⚠️ A first cut of this gate grepped raw lines and measured the wrong thing. It
        reported eleven "offences", every one of them a docstring giving a concrete
        example (`{"bundled": 20, "none": 14}` illustrating a keyword table), plus two
        outright false positives: `eap` inside "ch**eap**", and `standalone` in a comment
        about image results. Prose explaining a primitive with an example is good writing;
        the invariant is that the engine cannot BRANCH on a vertical noun.
        %
        String literals are deliberately KEPT — `if kind == "bundled"` is exactly the
        coupling worth catching, and it is a literal. Only docstrings and comments are
        excluded, because neither can change behaviour.
        """
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = getattr(node, "body", None) or []
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))

        found: list[tuple[str, str, int]] = []
        for node in ast.walk(tree):
            line = getattr(node, "lineno", 0)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    found.append(("literal", node.value, line))
            elif isinstance(node, ast.Name):
                found.append(("name", node.id, line))
            elif isinstance(node, ast.Attribute):
                found.append(("attribute", node.attr, line))
            elif isinstance(node, ast.keyword) and node.arg:
                found.append(("argument", node.arg, line))
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                found.append(("definition", node.name, line))
        return found

    def test_no_vertical_noun_appears_in_engine_behaviour(self):
        offences: list[str] = []
        for path in self.ENGINE_FILES:
            if not path.exists():
                continue
            for kind, text, lineno in self._code_tokens(path):
                lowered = text.lower()
                for word in self.FORBIDDEN:
                    # Word-boundary, not substring: "cheap" is not "eap", and
                    # "standalone_flag" IS "standalone".
                    if re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", lowered):
                        offences.append(
                            f"{path.name}:{lineno}: {kind} '{text[:60]}' contains '{word}'"
                        )
        assert not offences, (
            "vertical vocabulary in engine BEHAVIOUR — these are CONFIG values, not "
            "code:\n" + "\n".join(offences)
        )

    def test_the_gate_can_actually_fail(self):
        """A gate never seen to fail is not known to work.

        Two of the three checks this class relies on were wrong on first writing — a
        substring match and an unbounded regex — and a passing assertion looked identical
        either way. So: prove the detector fires on a synthetic coupling, and prove it
        ignores a docstring saying the same word.
        """
        import tempfile

        coupling = 'def f(x):\n    return x == "bundled"\n'
        prose = 'def f(x):\n    """A table like {"bundled": 20}."""\n    return x\n'
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.py"
            bad.write_text(coupling, encoding="utf-8")
            good = Path(tmp) / "good.py"
            good.write_text(prose, encoding="utf-8")

            def hits(path: Path) -> list[str]:
                return [
                    text
                    for _, text, _ in self._code_tokens(path)
                    if "bundled" in text.lower()
                ]

            assert hits(bad) == ["bundled"], "the gate cannot see a real coupling"
            assert hits(good) == [], "the gate flags a docstring, which is prose"
