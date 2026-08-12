"""Offline test suite for av-lead-scanner.

Zero live cost: the grounded-search provider is faked, scoring is
deterministic, and "today" is pinned. Run: `pytest` from the skill dir.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL_DIR))

import av_lead_scanner as als  # noqa: E402

TODAY = date(2026, 7, 6)


# ── helpers ────────────────────────────────────────────────────────────────

class CollectSink(als.Sink):
    """Captures every emitted event for assertions."""

    def __init__(self):
        self.events: list[dict] = []

    def emit(self, event):
        self.events.append(event)

    def by_type(self, t):
        return [e for e in self.events if e.get("type") == t]


def fake_provider_shared(prompt, **_):
    """Returns the SAME company on every call, so a lead found by two sources
    dedupes to one prospect with source_count == 2."""
    return json.dumps([{
        "organization_name": "Shared Church",
        "firm_name": "Shared Church",
        "city": "Dallas", "state": "TX",
        "project_description": "new sanctuary construction",
        "project_phase": "capital campaign",
        "estimated_timeline": "completion 2027",
        "notes": "found here",
    }])


TWO_SOURCE_CTX = {
    "organization": {"name": "TestCo"},
    "product_description": "test product",
    "sources": {
        "src_a": {
            "name_field": "organization_name",
            "fields": ["organization_name", "city", "state", "project_description",
                       "project_phase", "estimated_timeline"],
            "queries": ["query a"],
        },
        "src_b": {
            "name_field": "firm_name",
            "fields": ["firm_name", "city", "state", "notes"],
            "queries": ["query b"],
        },
    },
}


# ── name normalization ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("Acme Corp", "acme"),
    ("Acme, Inc.", "acme"),
    ("Beta (Beta Group) LLC", "beta"),
    ("  First   Baptist  ", "first baptist"),
    ("St. Mark's!", "st marks"),
])
def test_normalize_name(raw, expected):
    assert als.normalize_name(raw) == expected


# ── JSON array parsing ──────────────────────────────────────────────────────

def test_parse_json_array_plain():
    assert als.parse_json_array('[{"a":1}]') == [{"a": 1}]


def test_parse_json_array_fenced():
    assert als.parse_json_array('```json\n[{"a":1}]\n```') == [{"a": 1}]


def test_parse_json_array_embedded_prose():
    assert als.parse_json_array('Here you go: [{"a":1}] thanks') == [{"a": 1}]


def test_parse_json_array_garbage():
    assert als.parse_json_array("not json at all") == []


def test_parse_json_array_object_wrapped():
    assert als.parse_json_array('{"a":1}') == [{"a": 1}]


# ── prompt construction ─────────────────────────────────────────────────────

def test_build_prompt_default_includes_fields_and_seeds():
    cfg = {"fields": ["firm_name", "city"], "seed_firms": ["Alpha", "Beta"]}
    p = als.build_prompt(source_cfg=cfg, query="find firms", n=4, product_description="widgets")
    assert "find firms" in p
    assert "firm_name, city" in p
    assert "EXACTLY 4" in p
    assert "Alpha, Beta" in p
    assert "find ADDITIONAL firms" in p


def test_build_prompt_custom_template():
    cfg = {"prompt": "Q={query} N={n} {seed_context}"}
    p = als.build_prompt(source_cfg=cfg, query="x", n=2, product_description="")
    assert p.startswith("Q=x N=2")


# ── discovery: dedup, multi-source, deterministic IDs, wire stripping ────────

def test_discover_dedup_and_multisource(monkeypatch):
    sink = CollectSink()
    prospects = als.discover(TWO_SOURCE_CTX, scan_run_id="scan-1",
                             provider=fake_provider_shared, emit=sink.emit)
    assert len(prospects) == 1
    p = prospects[0]
    assert p["company_name"] == "Shared Church"
    assert p["discovery_data"]["source_count"] == 2
    assert sorted(p["discovery_data"]["sources_found_in"]) == ["src_a", "src_b"]
    # deterministic id
    expected = str(uuid.uuid5(als._NAMESPACE, "scan-1:shared church"))
    assert p["id"] == expected


def test_discover_id_is_scan_scoped():
    a = als.discover(TWO_SOURCE_CTX, scan_run_id="scan-A", provider=fake_provider_shared, emit=lambda e: None)
    b = als.discover(TWO_SOURCE_CTX, scan_run_id="scan-B", provider=fake_provider_shared, emit=lambda e: None)
    assert a[0]["id"] != b[0]["id"]  # different scans → different ids


def test_discover_emits_progress_and_stripped_prospects():
    sink = CollectSink()
    als.discover(TWO_SOURCE_CTX, scan_run_id="scan-1", provider=fake_provider_shared, emit=sink.emit)
    assert len(sink.by_type("phase_start")) == 2
    assert len(sink.by_type("phase_complete")) == 2
    prospect_events = sink.by_type("prospects")
    assert len(prospect_events) == 1
    # wire prospects must NOT carry the internal scoring bag
    for item in prospect_events[0]["items"]:
        assert "_internal" not in item


def test_discover_skips_bad_query(monkeypatch):
    calls = {"n": 0}

    def flaky(prompt, **_):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return fake_provider_shared(prompt)

    prospects = als.discover(TWO_SOURCE_CTX, scan_run_id="s", provider=flaky, emit=lambda e: None)
    # one source failed, the other still produced the prospect
    assert len(prospects) == 1


# ── scoring: parity, factors, ranking, clamping ─────────────────────────────

def _score_one(lead, ctx=None):
    ctx = ctx or {}
    return als.score_prospects([lead], ctx, today=TODAY)[0]


def test_score_wire_reconstruction_matches_internal():
    """A discover() prospect scored directly (has _internal) must match the
    same prospect scored after the _internal bag is stripped (the file path)."""
    prospects = als.discover(TWO_SOURCE_CTX, scan_run_id="s", provider=fake_provider_shared, emit=lambda e: None)
    with_internal = als.score_prospects(prospects, {}, today=TODAY)
    stripped = [als._strip_internal(p) for p in prospects]
    without_internal = als.score_prospects(stripped, {}, today=TODAY)
    assert with_internal[0]["score"] == without_internal[0]["score"]
    assert with_internal[0]["score_factors"] == without_internal[0]["score_factors"]


def test_score_fit_keyword():
    r = _score_one({"organization_name": "X", "project_description": "new sanctuary build"})
    assert r["score_factors"]["fit"] == 25


def test_score_multi_source_tiers():
    assert _score_one({"organization_name": "X", "source_count": 3})["score_factors"]["multi_source"] == 10
    assert _score_one({"organization_name": "X", "source_count": 2})["score_factors"]["multi_source"] == 6
    assert _score_one({"organization_name": "X", "source_count": 1})["score_factors"]["multi_source"] == 0


def test_score_region_bonus():
    ctx = {"scoring": {"region_bonus": {"max": 10, "state_aliases": {"texas": "tx"},
                                        "regions": {"tx": ["dallas"]}}}}
    hit = _score_one({"organization_name": "X", "city": "Dallas", "state": "TX"}, ctx)
    miss = _score_one({"organization_name": "X", "city": "Miami", "state": "FL"}, ctx)
    assert hit["score_factors"]["region_bonus"] == 10
    assert miss["score_factors"]["region_bonus"] == 0


def test_score_ai_adjustment_clamped():
    hi = _score_one({"organization_name": "X", "ai_score_adjustment": 999})
    lo = _score_one({"organization_name": "X", "ai_score_adjustment": -999})
    assert hi["ai_score_adjustment"] == 15
    assert lo["ai_score_adjustment"] == -15


def test_score_capped_at_100():
    lead = {
        "organization_name": "Mega", "city": "dallas", "state": "TX",
        "project_description": "new sanctuary new construction",
        "project_type": "new build", "project_phase": "approved",
        # No estimated_timeline → pipeline uses the "approved" phase fallback
        # (score 30), maximizing every factor so the raw total exceeds 100.
        "campaign_goal": "$5M",
        "denomination": "x", "key_contact": "y", "av_opportunity_notes": "z",
        "permit_type": "building", "consultant_firm": "c",
        "source_count": 3, "ai_score_adjustment": 15,
    }
    # `completeness.fields` is now declared explicitly. Before 2026-08-12 the engine
    # supplied a hardcoded church-AV field list as the default, which this lead happened
    # to fill completely; completeness is now config-derived (see UPSTREAM.md), so with
    # no `sources` in ctx the fallback is the vertical-neutral identity set, which this
    # lead does NOT fill — 98, and the cap this test exists to check goes unexercised.
    # Declaring the fields keeps the test's intent intact.
    ctx = {
        "scoring": {
            "region_bonus": {"max": 10, "state_aliases": {}, "regions": {"tx": ["dallas"]}},
            "completeness": {
                "max": 15,
                "fields": [
                    "organization_name", "city", "state", "project_description",
                    "project_type", "project_phase", "campaign_goal", "denomination",
                    "key_contact", "av_opportunity_notes", "permit_type",
                    "consultant_firm",
                ],
            },
        }
    }
    assert _score_one(lead, ctx)["score"] == 100


def test_score_ranking_order():
    leads = [
        {"organization_name": "Low", "project_phase": "under construction",
         "estimated_timeline": "completion 2025"},
        {"organization_name": "High", "project_description": "new sanctuary",
         "project_phase": "feasibility", "source_count": 3},
    ]
    ranked = als.score_prospects(leads, {}, today=TODAY)
    assert ranked[0]["company_name"] == "High"
    assert ranked[0]["rank"] == 1
    assert ranked[1]["rank"] == 2
    assert ranked[0]["score"] > ranked[1]["score"]


# ── pipeline timing ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,year,month", [
    ("completion Fall 2027", 2027, 11),
    ("opens Spring 2026", 2026, 5),
    ("2025-2027", 2027, 6),
    ("mid-2028", 2028, 6),
    ("no date here", None, None),
])
def test_parse_estimated_date(text, year, month):
    d = als.parse_estimated_date(text)
    if year is None:
        assert d is None
    else:
        assert d == date(year, month, 1)


def test_pipeline_date_based_stage():
    cfg = als._DEFAULT_SCORING["pipeline"]
    # completion 2027-11, decision = -13mo = 2026-10, ~3 months out → Decision Imminent
    r = als.calculate_pipeline({"estimated_timeline": "completion Fall 2027"}, cfg, TODAY)
    assert r["pipeline_status"] == "5 - Decision Imminent"
    assert r["months_to_decision"] == 3


def test_pipeline_phase_fallback():
    cfg = als._DEFAULT_SCORING["pipeline"]
    r = als.calculate_pipeline({"project_phase": "feasibility"}, cfg, TODAY)
    assert r["pipeline_status"] == "1 - Early Discovery"
    assert r["months_to_decision"] is None


def test_pipeline_campaign_goal_floor():
    cfg = als._DEFAULT_SCORING["pipeline"]
    r = als.calculate_pipeline({"campaign_goal": "$1M"}, cfg, TODAY)
    assert r["pipeline_status"] == "2 - Relationship Building"


def test_pipeline_default_abstains_rather_than_inventing_a_stage():
    """Changed 2026-08-12 (see UPSTREAM.md): the default used to return the literal
    `"Unknown"` with 10 of 30 timing points.

    Both were harmful in ways a unit test could not see. AEO consumes
    `pipeline_status` as a **sales** stage that drives operator kanbans, so every
    prospect arrived in a column no human moved it to; and awarding 10 points on no
    evidence is what made a real production run's scores cluster at 11-12 regardless
    of the prospect. Abstaining leaves the column NULL and the axis silent.
    """
    cfg = als._DEFAULT_SCORING["pipeline"]
    r = als.calculate_pipeline({}, cfg, TODAY)
    assert r["pipeline_status"] is None, "must abstain, not invent a sales stage"
    assert r["score"] == 0, "no timing evidence must contribute no points"
    # The rest of the shape stays stable for consumers.
    assert r["months_to_decision"] is None
    assert r["estimated_completion"] == "Unknown"


# ── provider seam (gemini / claude / mock) ──────────────────────────────────

def test_pick_provider_by_name():
    assert als._pick_provider("gemini", False, False) is als.gemini_provider
    assert als._pick_provider("claude", False, False) is als.claude_provider
    assert als._pick_provider("mock", False, False) is als.mock_provider


def test_pick_provider_mock_overrides_name():
    # --mock wins regardless of the requested provider (offline, no key)
    assert als._pick_provider("claude", True, False) is als.mock_provider


def test_pick_provider_unknown_raises():
    with pytest.raises(ValueError):
        als._pick_provider("bogus", False, False)


def test_provider_config_defaults_per_provider():
    assert als._provider_config({}, "gemini")["model"] == "gemini-3-flash-preview"
    assert als._provider_config({}, "claude")["model"] == "claude-opus-4-8"


def test_provider_config_reads_block():
    ctx = {"claude": {"model": "claude-sonnet-5", "entries_per_query": 7}}
    pc = als._provider_config(ctx, "claude")
    assert pc["model"] == "claude-sonnet-5"
    assert pc["entries_per_query"] == 7


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-4-8", "web_search_20260209"),
    ("claude-sonnet-5", "web_search_20260209"),
    ("claude-3-5-haiku", "web_search_20250305"),
])
def test_claude_web_search_tool_selection(model, expected):
    assert als._claude_web_search_tool(model) == expected


def test_discover_uses_provider_config_model():
    """The model from provider_config reaches the provider (asserted via capture)."""
    seen = {}

    def capture(prompt, *, model, temperature, retry_attempts, timeout_s):
        seen["model"] = model
        return fake_provider_shared(prompt)

    als.discover(TWO_SOURCE_CTX, scan_run_id="s", provider=capture, emit=lambda e: None,
                 provider_config={"model": "claude-opus-4-8", "temperature": 0.1,
                                  "entries_per_query": 3, "retry_attempts": 3})
    assert seen["model"] == "claude-opus-4-8"


# ── validation ──────────────────────────────────────────────────────────────

def test_validate_context_ok():
    assert als.validate_context(TWO_SOURCE_CTX, need_sources=True) == []


def test_validate_context_missing_sources():
    problems = als.validate_context({}, need_sources=True)
    assert problems and "sources" in problems[0]


def test_validate_context_source_no_queries():
    ctx = {"sources": {"s": {"fields": ["a"]}}}
    problems = als.validate_context(ctx, need_sources=True)
    assert any("no queries" in p for p in problems)


# ── FileSink output shaping ─────────────────────────────────────────────────

def test_filesink_top_n_and_fields(tmp_path):
    out = tmp_path / "o.json"
    sink = als.FileSink(str(out), {"top_n": 2, "fields": ["rank", "company_name"]})
    sink.emit({"type": "scored", "items": [
        {"rank": 1, "company_name": "A", "score": 90, "extra": "drop"},
        {"rank": 2, "company_name": "B", "score": 80, "extra": "drop"},
        {"rank": 3, "company_name": "C", "score": 70, "extra": "drop"},
    ]})
    sink.emit({"type": "completed", "summary": {"total_scored": 3}})
    sink.close(organization="Org")
    data = json.loads(out.read_text())
    assert len(data["scored"]) == 2
    assert set(data["scored"][0].keys()) == {"rank", "company_name"}
    assert data["organization_name"] == "Org"


# ── CLI end-to-end (mock, subprocess) ───────────────────────────────────────

def test_cli_run_mock_writes_file(tmp_path):
    out = tmp_path / "cli.json"
    proc = subprocess.run(
        [sys.executable, str(SKILL_DIR / "av_lead_scanner.py"), "run",
         "--context", str(SKILL_DIR / "examples" / "organization.json"),
         "--mock", "--today", "2026-07-06", "--out", str(out)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text())
    assert data["scored"], "expected scored leads"
    assert data["scored"][0]["rank"] == 1
    assert data["summary"]["total_scored"] > 0


def test_cli_score_stream_mock(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(SKILL_DIR / "av_lead_scanner.py"), "score",
         "--context", str(SKILL_DIR / "examples" / "prospects.sample.json"),
         "--today", "2026-07-06", "--out", "-"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(l) for l in proc.stdout.splitlines() if l.strip()]
    scored = [e for e in events if e["type"] == "scored"]
    assert scored and scored[0]["items"][0]["rank"] == 1
