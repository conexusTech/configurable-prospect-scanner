"""Drive `aeo.runner.main()` end to end with no network, no queue and no model.

Not a test module (pytest collects only `test_*.py`); a helper the runner-level tests
share. Everything `main()` reaches outside the process is replaced:

- the queue bootstrap answers "not in queue mode";
- `fetch_context` returns the context the test supplies;
- `fetch_source_records` returns the feed answer the test supplies (or raises);
- `post_event` records what would have gone to the gateway, instead of sending it;
- the provider is `StubProvider`, which answers from the prompt alone and counts
  every call by the `phase` label each phase passes.

The process-wide quota breaker and cost meter are swapped for fresh ones, so a run
here cannot be tripped or priced by whatever another test left behind.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import av_lead_scanner as als
from aeo import runner

SCAN_RUN_ID = "11111111-2222-4333-8444-555555555555"
ORG_ID = "22222222-3333-4444-8555-666666666666"
TENANT_ID = "33333333-4444-4555-8666-777777777777"
SKILL_SLUG = "restaurant-gaskets"
TODAY = "2026-11-19"

#: The pipeline vocabulary the gateway publishes; minimal but real.
PIPELINE = {
    "stages": [
        {"key": "1 - Early Discovery", "label": "Early Discovery", "order": 1,
         "kind": "timing", "min_months": 18, "max_months": 999,
         "requires_contact": False, "description": "18+ months out."},
        {"key": "4 - Active Pursuit", "label": "Active Pursuit", "order": 2,
         "kind": "timing", "min_months": 4, "max_months": 8,
         "requires_contact": False, "description": "4-8 months out."},
        {"key": "7 - Too Late", "label": "Too Late", "order": 3,
         "kind": "timing", "min_months": -999, "max_months": -4,
         "requires_contact": False, "description": "Decided 4+ months ago."},
    ],
}

#: What zip discovery answers for every market.
ZIP_ROWS = [
    {"zip_code": "23220", "city": "Richmond", "state": "VA"},
    {"zip_code": "23111", "city": "Mechanicsville", "state": "VA"},
]

#: A searched skill, as every skill was before feeds existed: one source of market-bound
#: queries, validation and contacts authored, the org already holding one company.
SEARCHED_CONTEXT: dict[str, Any] = {
    "organization": {"name": "Seal Pros of Virginia"},
    "geography": {"home_markets": ["Richmond, VA"]},
    "skill": {"config": {
        "product_description": "Commercial refrigeration gasket replacement",
        "discovery": {
            "sources": {
                "restaurants": {
                    "name_field": "company_name",
                    "fields": ["company_name", "location_address", "city", "state", "zip_code"],
                    "queries": ["restaurants in {market}", "diners in {market}"],
                },
            },
            "max_prospects": 10,
            "target_prospects": 3,
        },
        "validation": {"in_market_signals": ["walk-in cooler"], "disqualifiers": ["closed"]},
        "contacts": {"max_prospects": 10, "titles": ["Owner", "General Manager"]},
        "scoring": {"region_bonus": {"max": 10}},
    }},
    "pipeline": PIPELINE,
    "known_companies": ["Diners Two"],
}

#: The gateway's answer for the feed source, as both repos pin it.
FEED_ANSWER_PATH = Path(__file__).parent / "fixtures" / "source-records" / "health_inspections.json"

#: A feed source: its rows come from the gateway's health-inspection findings.
FEED_SOURCE: dict[str, Any] = {
    "name_field": "establishment_name",
    "fields": [
        "establishment_name", "location_address", "city", "state", "zip_code", "industry",
        "signal_type", "signal_date", "signal_description", "source_url",
    ],
    "queries": ["gasket"],
    "records": {"feed": "health_inspections", "window_days": 30},
}

#: A feed skill as the plan authors one: contacts with `max_prospects`, a prospect
#: ceiling, and no `validation` section.
FEED_CONTEXT: dict[str, Any] = {
    "organization": {"name": "Seal Pros of Virginia"},
    "geography": {"home_markets": ["Richmond, VA"]},
    "skill": {"config": {
        "product_description": "Commercial refrigeration gasket replacement",
        "discovery": {"sources": {"inspections": FEED_SOURCE}, "max_prospects": 60},
        "contacts": {"max_prospects": 60, "titles": ["Owner", "General Manager"]},
        "scoring": {"region_bonus": {"max": 10}},
    }},
    "pipeline": PIPELINE,
    "known_companies": [],
}


def feed_answer() -> dict[str, Any]:
    return json.loads(FEED_ANSWER_PATH.read_text())


def feed_context(**changes: Any) -> dict[str, Any]:
    """A deep copy of FEED_CONTEXT; `config_*` keys replace skill-config sections."""
    ctx = json.loads(json.dumps(FEED_CONTEXT))
    for key, value in changes.items():
        if key.startswith("config_"):
            ctx["skill"]["config"][key[len("config_"):]] = value
        else:
            ctx[key] = value
    return ctx


_NUMBERED = re.compile(r"^\s*(\d+)\.\s*name:\s*([^;\n]+)", re.MULTILINE)
_REPORTED_AS = re.compile(r"reported as:\s*(.+)")
_SEARCH_FOR = re.compile(r"Search for:\s*(.+)")


class StubProvider:
    """A provider that answers from the prompt and counts calls per phase.

    Deterministic and thread-safe: every per-prospect phase calls it from a pool.
    `answers` overrides the reply for a phase; a callable receives the prompt.
    """

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.answers = answers or {}
        self.calls: dict[str, int] = {}
        self.prompts: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def __call__(self, prompt: str, **kwargs: Any) -> str:
        phase = str(kwargs.get("phase") or "unknown")
        with self._lock:
            self.calls[phase] = self.calls.get(phase, 0) + 1
            self.prompts.setdefault(phase, []).append(prompt)
        override = self.answers.get(phase)
        if callable(override):
            return override(prompt)
        if override is not None:
            return override
        handler = getattr(self, f"_answer_{phase}", None)
        return handler(prompt) if handler else "[]"

    def count(self, phase: str) -> int:
        return self.calls.get(phase, 0)

    # ── default answers ─────────────────────────────────────────────────────
    @staticmethod
    def _answer_zip_discovery(prompt: str) -> str:
        return json.dumps(ZIP_ROWS)

    @staticmethod
    def _answer_discovery(prompt: str) -> str:
        """Three firms per query: two in Richmond, one in Norfolk (out of area).

        Each row carries the full query it answered, so the query a run actually sent
        (market bound or not) reaches `discovery_data` and so the wire.
        """
        m = _SEARCH_FOR.search(prompt)
        searched = m.group(1).strip() if m else "prospects"
        query = searched.split(" in ")[0].title()
        return json.dumps([
            {"company_name": f"{query} One", "location_address": "1 Main St, Richmond, VA 23220",
             "city": "Richmond", "state": "VA", "zip_code": "23220", "search_query": searched},
            {"company_name": f"{query} Two", "location_address": "2 Main St, Richmond, VA 23220",
             "city": "Richmond", "state": "VA", "zip_code": "23220", "search_query": searched},
            {"company_name": f"{query} Far", "location_address": "9 Bay St, Norfolk, VA 23510",
             "city": "Norfolk", "state": "VA", "zip_code": "23510", "search_query": searched},
        ])

    @staticmethod
    def _answer_geography(prompt: str) -> str:
        """Verifies each firm where discovery said it was."""
        m = _REPORTED_AS.search(prompt)
        parts = [p.strip() for p in (m.group(1).split(",") if m else [])]
        city = parts[0] if parts else ""
        state = parts[1] if len(parts) > 1 else ""
        zip_code = {"Richmond": "23220", "Norfolk": "23510"}.get(city, "")
        return json.dumps([{"city": city, "state": state, "zip_code": zip_code,
                            "confidence": "high", "source_url": "https://example.org/a"}])

    @staticmethod
    def _answer_validation(prompt: str) -> str:
        return json.dumps([{"validated": True, "signals_found": ["walk-in cooler"],
                            "disqualifiers_hit": [], "reasoning": "stub"}])

    @staticmethod
    def _answer_contacts(prompt: str) -> str:
        """Three named people for every numbered organization, in order."""
        out = []
        for n, name in _NUMBERED.findall(prompt):
            slug = re.sub(r"[^a-z0-9]", "", name.lower())
            out.append({
                "n": int(n),
                "company_name": name.strip(),
                "people": [
                    {"contact_name": f"{first} {name.strip()}",
                     "contact_title": title,
                     "contact_email": f"{first.lower()}@{slug}.example",
                     "source_url": f"https://{slug}.example/team",
                     "confidence": "high"}
                    for first, title in (("Avery", "Owner"), ("Blake", "General Manager"),
                                         ("Casey", "Kitchen Manager"))
                ],
            })
        return json.dumps(out)


@dataclass
class RunResult:
    code: int
    #: Every event the run handed its sink, before mapping (progress events included).
    emitted: list[dict[str, Any]] = field(default_factory=list)
    #: Every callback that would have reached the gateway: (event type, payload).
    posted: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    #: Every source-records request the run made: the source name asked for.
    record_requests: list[str] = field(default_factory=list)
    provider: StubProvider | None = None

    def posted_of(self, event_type: str) -> list[dict[str, Any]]:
        return [p for t, p in self.posted if t == event_type]

    def items_of(self, event_type: str) -> list[dict[str, Any]]:
        return [item for p in self.posted_of(event_type) for item in p.get("data", [])]

    def error_message(self) -> str | None:
        errors = self.posted_of("error")
        return errors[-1]["message"] if errors else None


def run_main(
    monkeypatch: Any,
    context: dict[str, Any],
    *,
    records: Callable[[str], dict[str, Any]] | dict[str, Any] | None = None,
    provider: StubProvider | None = None,
    env: dict[str, str] | None = None,
) -> RunResult:
    """Run `runner.main()` once against `context`, with nothing leaving the process.

    `records` answers the source-records fetch: a dict is returned for every source, a
    callable is called with the source name (and may raise). `None` makes any fetch
    fail the test, which is what a searched skill must never do.
    """
    stub = provider or StubProvider()
    result = RunResult(code=-1, provider=stub)

    for name in ("TASK_RECORD_ID", "PHASES", "SCAN_IS_TEST", "CI_USER", "CI_PASSWORD",
                 "SCANNER_TOP_N", "AV_SCANNER_MOCK", "SCANNER_CUSTOM_MODULES_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    base_env = {
        "ORGANIZATION_ID": ORG_ID,
        "TENANT_ID": TENANT_ID,
        "SCAN_RUN_ID": SCAN_RUN_ID,
        "AEO_BACKEND_URL": "http://gateway.invalid",
        "SKILL_SLUG": SKILL_SLUG,
        "AV_SCANNER_PROVIDER": "gemini",
        "AV_SCANNER_TODAY": TODAY,
    }
    for key, value in {**base_env, **(env or {})}.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setattr(runner, "bootstrap", lambda: {})
    monkeypatch.setattr(runner, "fetch_context", lambda *a, **k: json.loads(json.dumps(context)))

    def _fetch_records(backend_url: str, org_id: str, skill_slug: str, source: str) -> dict[str, Any]:
        result.record_requests.append(source)
        if records is None:
            raise AssertionError(f"this run must not read source records (asked for {source!r})")
        if callable(records):
            return records(source)
        return json.loads(json.dumps(records))

    # `raising=False`: the harness must also drive a runner that has no feed path, so a
    # searched run can be recorded against the code as it was before feeds existed.
    monkeypatch.setattr(runner, "fetch_source_records", _fetch_records, raising=False)

    def _post(backend_url: str, scan_run_id: str, event_type: str, payload: dict[str, Any],
              *, tenant_id: str, organization_id: str) -> None:
        assert scan_run_id == SCAN_RUN_ID and tenant_id == TENANT_ID and organization_id == ORG_ID
        result.posted.append((event_type, json.loads(json.dumps(payload))))

    monkeypatch.setattr(runner, "post_event", _post)

    original_emit = runner.AeoEventSink.emit

    def _emit(self: Any, event: dict[str, Any]) -> None:
        result.emitted.append(json.loads(json.dumps(event, default=str)))
        original_emit(self, event)

    monkeypatch.setattr(runner.AeoEventSink, "emit", _emit)
    monkeypatch.setattr(als, "_pick_provider", lambda *a, **k: stub)
    monkeypatch.setattr(als, "_quota_breaker", als._QuotaBreaker(als.QUOTA_TRIP_THRESHOLD))
    monkeypatch.setattr(als, "_cost_meter", als._CostMeter())

    result.code = runner.main()
    return result


def prompt_digests(result: RunResult) -> dict[str, list[str]]:
    """Every prompt the run sent, per phase, as sorted sha256 digests.

    Sorted because the per-prospect phases run on a pool; the set of prompts is what a
    run pays for, not the order a pool happened to issue them in.
    """
    assert result.provider is not None
    return {
        phase: sorted(hashlib.sha256(p.encode()).hexdigest() for p in prompts)
        for phase, prompts in sorted(result.provider.prompts.items())
    }


def wire_snapshot(result: RunResult) -> list[dict[str, Any]]:
    """The gateway-bound callbacks in a comparable form.

    Callback order is the run's; items inside one callback are sorted by their key,
    because discovery and every per-prospect phase run on a thread pool and so collect
    in completion order. What the gateway stores does not depend on that order.
    """
    def _key(item: Any) -> str:
        if not isinstance(item, dict):
            return json.dumps(item, sort_keys=True)
        return str(item.get("id") or item.get("prospect_id") or item.get("zip_code") or "")

    out = []
    for event_type, payload in result.posted:
        snap = dict(payload)
        if isinstance(snap.get("data"), list):
            snap["data"] = sorted(snap["data"], key=_key)
        out.append({"type": event_type, "payload": snap})
    return out
