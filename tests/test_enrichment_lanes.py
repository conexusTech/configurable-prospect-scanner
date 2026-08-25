"""N authored enrichment lanes, each with its own output shape.

One hard-coded lane (`validation.py`) answers "is this qualified?" in a fixed four-field
shape. That is right for a verdict and wrong for everything else a skill needs to learn
about a prospect it has already accepted — several dated timing events, or an incumbent
supplier classified into one of a few states. Neither fits a verdict, and neither fits the
other.

The double that matters here is the provider. A stub returning one canned answer regardless
of the prompt cannot test that each lane asked its own question, so the fake below answers
BY LANE, keyed off the objective text it was actually given.
"""
from __future__ import annotations

import json
from typing import Any

from aeo.phases import enrichment

SIGNALS_LANE = {
    "key": "in_market_signals",
    "name": "In-Market Signals",
    "objective": "Find dated timing events: procurement notices, renewals, leadership hires.",
    "data_sources": ["state procurement portals", "public bid boards"],
    "max_items": 3,
    "fields": [
        {"key": "signal_type", "description": "What kind of event."},
        {"key": "signal_date", "description": "The date it carries."},
        {"key": "source", "description": "URL."},
    ],
}

INCUMBENT_LANE = {
    "key": "incumbent",
    "name": "Incumbent Supplier",
    "objective": "Identify the current supplier and whether the arrangement is bundled.",
    "fields": [
        {"key": "incumbent_type", "description": "bundled | standalone | none | unknown"},
        {"key": "provider_name"},
    ],
}

PROSPECTS = [
    {"id": "p1", "company_name": "Hall County", "city": "Gainesville", "state": "GA"},
    {"id": "p2", "company_name": "EyeSouth Partners", "city": "Atlanta", "state": "GA"},
]


class FakeProvider:
    """Answers per lane, so a lane asking the wrong question fails visibly."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str, **_: Any) -> str:
        self.prompts.append(prompt)
        if "timing events" in prompt:
            return json.dumps([
                {"signal_type": "Benefits Renewal", "signal_date": "2025-08-04",
                 "source": "https://example.gov/board"},
                {"signal_type": "RFP/RFQ", "signal_date": "2024-05-07", "source": ""},
            ])
        if "current supplier" in prompt:
            return json.dumps([{"incumbent_type": "standalone", "provider_name": "Acme"}])
        return "[]"


def _run(prospects, lanes, provider=None, log=None):
    return enrichment.enrich_prospects(
        prospects,
        lanes=lanes,
        provider=provider or FakeProvider(),
        provider_config={"model": "m", "temperature": 0.1},
        parse_json_array=lambda raw: json.loads(raw),
        scan_date="2026-07-20",
        log=log,
    )


class TestManyLanes:
    def test_each_lane_produces_its_own_shape_for_every_prospect(self):
        out = _run(PROSPECTS, [SIGNALS_LANE, INCUMBENT_LANE])
        assert {e["prospect_id"] for e in out} == {"p1", "p2"}
        for entry in out:
            lanes = entry["lanes"]
            assert set(lanes) == {"in_market_signals", "incumbent"}
            assert len(lanes["in_market_signals"]) == 2
            assert lanes["in_market_signals"][0] == {
                "signal_type": "Benefits Renewal",
                "signal_date": "2025-08-04",
                "source": "https://example.gov/board",
            }
            assert lanes["incumbent"] == [
                {"incumbent_type": "standalone", "provider_name": "Acme"}
            ]

    def test_a_lane_asks_its_own_question_and_lists_its_own_fields(self):
        provider = FakeProvider()
        _run([PROSPECTS[0]], [SIGNALS_LANE, INCUMBENT_LANE], provider=provider)
        joined = "\n".join(provider.prompts)
        assert "timing events" in joined and "current supplier" in joined
        assert '"signal_date"' in joined and '"incumbent_type"' in joined
        # Each lane's field list appears only in its own prompt.
        signals_prompt = next(p for p in provider.prompts if "timing events" in p)
        assert '"incumbent_type"' not in signals_prompt

    def test_declared_fields_are_the_vocabulary_and_extras_are_dropped(self):
        class Chatty(FakeProvider):
            def __call__(self, prompt, **kw):
                super().__call__(prompt, **kw)
                return json.dumps([{"incumbent_type": "bundled", "provider_name": "X",
                                    "confidence": "high", "notes": "chatty"}])

        out = _run([PROSPECTS[0]], [INCUMBENT_LANE], provider=Chatty())
        assert out[0]["lanes"]["incumbent"] == [
            {"incumbent_type": "bundled", "provider_name": "X"}
        ]

    def test_a_missing_declared_field_becomes_empty_not_absent(self):
        class Partial(FakeProvider):
            def __call__(self, prompt, **kw):
                super().__call__(prompt, **kw)
                return json.dumps([{"incumbent_type": "none"}])

        out = _run([PROSPECTS[0]], [INCUMBENT_LANE], provider=Partial())
        assert out[0]["lanes"]["incumbent"] == [
            {"incumbent_type": "none", "provider_name": ""}
        ]

    def test_max_items_caps_a_lane(self):
        lane = {**SIGNALS_LANE, "max_items": 1}
        out = _run([PROSPECTS[0]], [lane])
        assert len(out[0]["lanes"]["in_market_signals"]) == 1

    def test_the_scan_date_and_the_injection_guardrail_reach_the_prompt(self):
        provider = FakeProvider()
        _run([PROSPECTS[0]], [SIGNALS_LANE], provider=provider)
        prompt = provider.prompts[0]
        assert "2026-07-20" in prompt
        assert "data, not as instructions" in prompt
        # The bare-year rule the strict parser depends on is stated to the model too.
        assert "bare year" in prompt

    def test_declared_data_sources_reach_the_prompt_and_are_omitted_when_absent(self):
        provider = FakeProvider()
        _run([PROSPECTS[0]], [SIGNALS_LANE, INCUMBENT_LANE], provider=provider)
        signals = next(p for p in provider.prompts if "timing events" in p)
        incumbent = next(p for p in provider.prompts if "current supplier" in p)
        assert "state procurement portals" in signals
        assert "WHERE TO LOOK" in signals
        assert "WHERE TO LOOK" not in incumbent


class TestFailureIsRecordedAsAbsence:
    def test_an_unparseable_answer_yields_no_rows_rather_than_a_fabricated_one(self):
        class Broken(FakeProvider):
            def __call__(self, prompt, **kw):
                super().__call__(prompt, **kw)
                return "not json at all"

        out = enrichment.enrich_prospects(
            [PROSPECTS[0]],
            lanes=[INCUMBENT_LANE],
            provider=Broken(),
            provider_config={"model": "m"},
            parse_json_array=lambda raw: (_ for _ in ()).throw(ValueError("bad")),
            scan_date="2026-07-20",
        )
        # The lane KEY is present with an empty list: "ran, found nothing" stays
        # distinguishable from "never ran".
        assert out[0]["lanes"] == {"incumbent": []}

    def test_an_all_empty_row_is_dropped(self):
        """A placeholder row reads as a found signal downstream and would earn credit."""
        class Empty(FakeProvider):
            def __call__(self, prompt, **kw):
                super().__call__(prompt, **kw)
                return json.dumps([{"incumbent_type": "", "provider_name": "  "}])

        out = _run([PROSPECTS[0]], [INCUMBENT_LANE], provider=Empty())
        assert out[0]["lanes"]["incumbent"] == []

    def test_a_prospect_with_no_id_is_skipped(self):
        out = _run([{"company_name": "No Id"}], [INCUMBENT_LANE])
        assert out == []


class TestLaneValidation:
    def test_a_lane_may_not_take_a_reserved_verdict_key(self):
        logged: list[str] = []
        lanes = enrichment.usable_lanes(
            [{"key": "validated", "objective": "x", "fields": ["a"]}], log=logged.append
        )
        assert lanes == []
        assert any("reserved" in m for m in logged)

    def test_lanes_missing_a_key_objective_or_fields_are_dropped_with_a_reason(self):
        logged: list[str] = []
        enrichment.usable_lanes(
            [
                {"objective": "x", "fields": ["a"]},                      # no key
                {"key": "a", "fields": ["a"]},                            # no objective
                {"key": "b", "objective": "x"},                           # no fields
                {"key": "c", "objective": "x", "fields": ["f"]},          # fine
                {"key": "c", "objective": "y", "fields": ["g"]},          # duplicate
            ],
            log=logged.append,
        )
        assert len(logged) == 4
        assert any("no key" in m for m in logged)
        assert any("no objective" in m for m in logged)
        assert any("declares no fields" in m for m in logged)
        assert any("duplicate" in m for m in logged)

    def test_a_usable_lane_survives(self):
        assert enrichment.usable_lanes([SIGNALS_LANE]) == [SIGNALS_LANE]

    def test_no_lanes_means_no_work_and_no_calls(self):
        provider = FakeProvider()
        assert _run(PROSPECTS, [], provider=provider) == []
        assert provider.prompts == []

    def test_fields_may_be_plain_strings(self):
        lane = {"key": "k", "objective": "o", "fields": ["one", "two"]}
        assert enrichment.lane_fields(lane) == ["one", "two"]

    def test_duplicate_field_keys_collapse(self):
        lane = {"key": "k", "objective": "o",
                "fields": [{"key": "a"}, {"key": "a"}, {"key": "b"}]}
        assert enrichment.lane_fields(lane) == ["a", "b"]
