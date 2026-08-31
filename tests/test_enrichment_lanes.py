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
    """Answers per lane AND per entity, so a lane asking the wrong question fails visibly.

    🔑 **Updated for the fused call (bundling ruling §3).** The phase now sends ONE prompt
    covering every prospect in the batch and every signal group, and expects one object per
    prospect with a named array per group. A stub that ignored the prompt could not test
    either half, so this fake:

    - reads the NUMBERED INPUT LIST back out of the prompt, so a phase that failed to
      number its entities gets nothing and the §5 rule-1 test fails rather than passing by
      luck;
    - answers a group only when that group's own objective text is present, which is what
      keeps "each lane asked its own question" testable under fusion.
    """

    def __init__(self) -> None:
        self.prompts: list[str] = []

    @staticmethod
    def _names(prompt: str) -> list[str]:
        """Entity names in input order, parsed from the numbered list."""
        found = []
        for line in prompt.splitlines():
            line = line.strip()
            if not line or not line[0].isdigit() or "name: " not in line:
                continue
            after = line.split("name: ", 1)[1]
            found.append(after.split(";")[0].strip())
        return found

    def __call__(self, prompt: str, **_: Any) -> str:
        self.prompts.append(prompt)
        out = []
        for i, name in enumerate(self._names(prompt), 1):
            obj: dict[str, Any] = {"n": i, "company_name": name}
            if "timing events" in prompt:
                obj["in_market_signals"] = [
                    {"signal_type": "Benefits Renewal", "signal_date": "2025-08-04",
                     "source": "https://example.gov/board"},
                    {"signal_type": "RFP/RFQ", "signal_date": "2024-05-07", "source": ""},
                ]
            if "current supplier" in prompt:
                obj["incumbent"] = [
                    {"incumbent_type": "standalone", "provider_name": "Acme"}
                ]
            out.append(obj)
        return json.dumps(out)


def fused(prompt: str, **lane_rows: Any) -> str:
    """Build a correctly-shaped fused response for every entity in `prompt`.

    🔑 The doubles below used to return a FLAT array of rows, which was right for the
    old one-call-per-lane phase. Under fusion that array never reaches `_coerce_rows`
    at all — `obj.get("<lane>")` is None — so those tests passed while testing nothing.
    Anything asserting on coercion must go through this.
    """
    names = FakeProvider._names(prompt)
    return json.dumps(
        [
            {"n": i, "company_name": name, **lane_rows}
            for i, name in enumerate(names, 1)
        ]
    )


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
            # `signal_class` is added beside the free-text `signal_type` (2026-08-31).
            # Asserted as an exact dict deliberately: the point of the canonical class is
            # that it is PRESENT and CORRECT, and a subset check would pass just as well
            # if it silently stopped being emitted.
            assert lanes["in_market_signals"][0] == {
                "signal_type": "Benefits Renewal",
                "signal_date": "2025-08-04",
                "source": "https://example.gov/board",
                "signal_class": "benefits_change",
            }
            # A lane with no `signal_type` field gains nothing — the enrichment keys on
            # the field, not on the lane, so a vertical that has no switching signal is
            # untouched rather than given an empty class.
            assert lanes["incumbent"] == [
                {"incumbent_type": "standalone", "provider_name": "Acme"}
            ]

    def test_a_lane_asks_its_own_question_and_lists_its_own_fields(self):
        provider = FakeProvider()
        _run([PROSPECTS[0]], [SIGNALS_LANE, INCUMBENT_LANE], provider=provider)
        joined = "\n".join(provider.prompts)
        assert "timing events" in joined and "current supplier" in joined
        assert '"signal_date"' in joined and '"incumbent_type"' in joined
        # 🔑 Under fusion both lanes share ONE prompt, so "its own prompt" is no longer
        # the isolation boundary — the GROUP section is. A lane's fields must appear
        # under its own group and nowhere else, which is a stricter check than before:
        # the old test would have passed on a prompt that listed every field twice.
        prompt = provider.prompts[0]
        sections = prompt.split('GROUP "')
        signals_section = next(x for x in sections if x.startswith("in_market_signals"))
        incumbent_section = next(x for x in sections if x.startswith("incumbent"))
        assert '"signal_date"' in signals_section
        assert '"incumbent_type"' not in signals_section
        assert '"incumbent_type"' in incumbent_section
        assert '"signal_date"' not in incumbent_section

    def test_declared_fields_are_the_vocabulary_and_extras_are_dropped(self):
        class Chatty(FakeProvider):
            def __call__(self, prompt, **kw):
                super().__call__(prompt, **kw)
                return fused(prompt, incumbent=[
                    {"incumbent_type": "bundled", "provider_name": "X",
                     "confidence": "high", "notes": "chatty"}
                ])

        out = _run([PROSPECTS[0]], [INCUMBENT_LANE], provider=Chatty())
        assert out[0]["lanes"]["incumbent"] == [
            {"incumbent_type": "bundled", "provider_name": "X"}
        ]

    def test_a_missing_declared_field_becomes_empty_not_absent(self):
        class Partial(FakeProvider):
            def __call__(self, prompt, **kw):
                super().__call__(prompt, **kw)
                return fused(prompt, incumbent=[{"incumbent_type": "none"}])

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
        sections = provider.prompts[0].split('GROUP "')
        signals = next(x for x in sections if x.startswith("in_market_signals"))
        incumbent = next(x for x in sections if x.startswith("incumbent"))
        assert "state procurement portals" in signals
        assert "WHERE TO LOOK" in signals
        # The lane that declared no sources must not inherit the other lane's block —
        # the failure fusion makes possible, and the reason this is asserted per section.
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
                return fused(prompt, incumbent=[
                    {"incumbent_type": "", "provider_name": "  "}
                ])

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


class TestFusionActuallyReducesCalls:
    """🔴 The point of the change. Without this the fusion could regress silently.

    Measured motivation, run `711b6652`: `enrichment` spent **171 grounded calls** on
    57 prospects × 3 lanes — the Cartesian product the bundling ruling §3 exists to
    remove. Cost is call COUNT, so a test that checks output shape and not call count
    would pass on the version that costs 14x more.
    """

    THIRD_LANE = {
        "key": "hiring",
        "objective": "Find open roles that imply growth.",
        "fields": [{"key": "role"}],
    }

    def _many(self, n: int) -> list[dict[str, Any]]:
        return [
            {"id": f"p{i}", "company_name": f"Co {i}", "city": "Atlanta", "state": "GA"}
            for i in range(n)
        ]

    def test_one_call_per_BATCH_not_per_prospect_times_lane(self):
        provider = FakeProvider()
        lanes = [SIGNALS_LANE, INCUMBENT_LANE, self.THIRD_LANE]
        _run(self._many(12), lanes, provider=provider)

        # 3 groups -> batch 5 -> ceil(12/5) = 3 calls. The old shape was 12 x 3 = 36.
        assert len(provider.prompts) == 3, (
            f"expected 3 fused calls, got {len(provider.prompts)} "
            "— the Cartesian product is back"
        )

    def test_every_prospect_still_gets_every_lane(self):
        # Fewer calls must not mean fewer answers: the saving is worthless if it drops
        # entities, which is exactly how an oversized batch fails (ruling §4).
        lanes = [SIGNALS_LANE, INCUMBENT_LANE]
        out = _run(self._many(7), lanes)
        assert len(out) == 7
        for entry in out:
            assert set(entry["lanes"]) == {"in_market_signals", "incumbent"}
            assert entry["lanes"]["in_market_signals"], "a prospect lost its rows"

    def test_batch_size_follows_the_group_count(self):
        # §4's table. 3+ groups is the only measured size and must not drift upward.
        assert enrichment.batch_size_for(3) == 5
        assert enrichment.batch_size_for(9) == 5
        assert enrichment.batch_size_for(2) == 6
        assert enrichment.batch_size_for(1) == 8

    def test_the_five_guardrails_reach_the_prompt(self):
        provider = FakeProvider()
        _run(self._many(3), [SIGNALS_LANE, INCUMBENT_LANE], provider=provider)
        prompt = provider.prompts[0]
        # §5.1 numbered input list
        assert "1. name: Co 0" in prompt and "2. name: Co 1" in prompt
        # §5.2 exact official name, nothing else
        assert "exact official name and NOTHING else" in prompt
        # §5.3 one object per entity, in input order
        assert "IN THE SAME ORDER" in prompt
        assert "even for entries you found nothing for" in prompt
        # §5.4 empty array, never omission
        assert "EMPTY, never a missing key" in prompt

    def test_a_short_response_is_REPORTED_not_silently_empty(self):
        """§5.5 — 'do not let a batch of 8 silently return 5'."""

        class Short(FakeProvider):
            def __call__(self, prompt, **kw):
                super().__call__(prompt, **kw)
                names = self._names(prompt)
                # Answer for the first entity only, and drop the rest.
                return json.dumps(
                    [{"n": 1, "company_name": names[0], "incumbent": []}]
                )

        events: list[dict[str, Any]] = []
        logs: list[str] = []
        enrichment.enrich_prospects(
            self._many(4),
            lanes=[INCUMBENT_LANE],
            provider=Short(),
            provider_config={"model": "m"},
            parse_json_array=lambda raw: json.loads(raw),
            scan_date="2026-07-20",
            emit=events.append,
            log=logs.append,
        )
        miss = next(e for e in events if e["type"] == "enrichment_unmatched")
        assert miss["prospects"] == 3 and miss["of"] == 4
        assert any("no object in the fused response" in m for m in logs)

    def test_name_matching_recovers_a_response_returned_out_of_order(self):
        """Position is the contract; a wrong-length response falls back to names."""

        class Shuffled(FakeProvider):
            def __call__(self, prompt, **kw):
                super().__call__(prompt, **kw)
                names = self._names(prompt)
                objs = [
                    {"n": i, "company_name": n, "incumbent": [{"incumbent_type": n}]}
                    for i, n in enumerate(names, 1)
                ]
                # Reversed AND one extra, so the length check cannot save it.
                return json.dumps(list(reversed(objs)) + [{"company_name": "Ghost Inc"}])

        out = enrichment.enrich_prospects(
            self._many(3),
            lanes=[INCUMBENT_LANE],
            provider=Shuffled(),
            provider_config={"model": "m"},
            parse_json_array=lambda raw: json.loads(raw),
            scan_date="2026-07-20",
        )
        by_id = {e["prospect_id"]: e["lanes"]["incumbent"] for e in out}
        # Each prospect got ITS OWN row back, not its neighbour's. `provider_name` is
        # the declared field the model omitted, filled empty by `_coerce_rows` — so this
        # also shows coercion still runs through the fused path.
        for i in range(3):
            assert by_id[f"p{i}"] == [
                {"incumbent_type": f"Co {i}", "provider_name": ""}
            ]
