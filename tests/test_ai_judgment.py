"""Tests for the AI pipeline-stage judgment phase.

These pin the behaviour that makes the phase worth having over the date map it
replaces: the event TYPE reaches the model, an out-of-vocabulary stage cannot be
persisted, and an unjudged prospect stays visibly unjudged instead of acquiring a
plausible stage.

Every case is built from shapes taken off RFC's real run `9f9fe2d7`, not invented.
"""

from __future__ import annotations

import json

from aeo.phases.ai_judgment import (
    DEFAULT_BATCH_SIZE,
    JUDGMENT_FIELDS,
    judge_prospects,
)

PIPELINE = {
    "stages": [
        {
            "key": "1 - Early Discovery",
            "description": "18+ months out. Build awareness early.",
            "min_months": 18,
            "max_months": 999,
        },
        {
            "key": "4 - Active Pursuit",
            "description": "4-8 months out. Active evaluation.",
            "min_months": 4,
            "max_months": 8,
        },
        {
            "key": "5 - Decision Imminent",
            "description": "0-4 months out. Urgency is HIGH.",
            "min_months": 0,
            "max_months": 4,
        },
        {
            "key": "7 - Too Late",
            "description": "Decided 4+ months ago. Move on.",
            "min_months": -999,
            "max_months": -4,
        },
    ],
    "signal_fields": ["trigger_date", "transaction_date"],
}


def prospect(pid: str, **by_source):
    """A prospect in the shape the discovery phase leaves behind."""
    return {
        "id": pid,
        "company_name": f"Company {pid}",
        "_by_source": by_source or {},
    }


def responder(payload):
    """A provider that records the prompt and returns `payload` as its JSON body."""
    seen = {}

    def provider(promptext, **_kw):
        seen["prompt"] = promptext
        return payload if isinstance(payload, str) else json.dumps(payload)

    provider.seen = seen  # type: ignore[attr-defined]
    return provider


def run(prospects, provider, **over):
    kwargs = dict(
        pipeline=PIPELINE,
        product_description="commercial flooring",
        today="2026-08-21",
        provider=provider,
        provider_config={"model": "m"},
        parse_json_array=lambda s: json.loads(s),
    )
    kwargs.update(over)
    return judge_prospects(prospects, **kwargs)


class TestPromptContents:
    """What the model is shown. The date map failed because it never saw the type."""

    def test_pairs_the_event_TYPE_with_its_DATE(self):
        # The entire defect in one assertion. `transaction_type` beside
        # `transaction_date` is what lets "Lease, 2019" and "Permit, 2026-02" reach
        # opposite stages.
        p = prospect(
            "p1",
            permits={
                "trigger_type": "Commercial Building Permit Issued",
                "trigger_date": "2026-02-20",
            },
        )
        prov = responder([{"id": "p1", "stage": "4 - Active Pursuit", "reasoning": "r", "adjustment": 0}])
        run([p], prov)
        prompt = prov.seen["prompt"]
        assert "Commercial Building Permit Issued" in prompt
        assert "2026-02-20" in prompt
        # …and on the SAME line, so the pairing is unmissable rather than inferable.
        event_line = next(l for l in prompt.split("\n") if l.startswith("event:"))
        assert "Commercial Building Permit Issued" in event_line
        assert "2026-02-20" in event_line

    def test_says_so_when_a_type_is_missing_rather_than_omitting_the_event(self):
        p = prospect("p1", cre={"transaction_date": "2026-06-17"})
        prov = responder([{"id": "p1", "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": 0}])
        run([p], prov)
        line = next(l for l in prov.seen["prompt"].split("\n") if l.startswith("event:"))
        assert "(type not given)" in line and "2026-06-17" in line

    def test_states_plainly_when_there_is_no_dated_signal(self):
        # 82 of RFC's 131 prospects had no date at all. Silence would let the model
        # infer one from whatever else it saw.
        prov = responder([{"id": "p1", "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": 0}])
        run([prospect("p1", dir={"industry": "Retail"})], prov)
        assert "event: none found" in prov.seen["prompt"]

    def test_carries_the_other_discovery_evidence(self):
        # The meeting's "no WHY in the output" applies to the judge too: it cannot
        # weigh a signal it was never shown.
        p = prospect("p1", permits={"trigger_date": "2026-02-20", "square_footage": "120,000"})
        prov = responder([{"id": "p1", "stage": "4 - Active Pursuit", "reasoning": "r", "adjustment": 0}])
        run([p], prov)
        assert "square_footage: 120,000" in prov.seen["prompt"]

    def test_offers_every_rung_with_its_description(self):
        prov = responder([{"id": "p1", "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": 0}])
        run([prospect("p1")], prov)
        prompt = prov.seen["prompt"]
        assert PIPELINE["stages"], "fixture must not be empty or the loop below is vacuous"
        for s in PIPELINE["stages"]:
            assert s["key"] in prompt
            assert s["description"] in prompt

    def test_tells_the_model_the_bounds_are_guidance_not_a_formula(self):
        # Handing over the bounds as arithmetic reproduces the date map with extra steps.
        prov = responder([{"id": "p1", "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": 0}])
        run([prospect("p1")], prov)
        assert "not a formula" in prov.seen["prompt"]


class TestJudgement:
    def test_returns_the_stage_reasoning_and_adjustment(self):
        prov = responder(
            [{"id": "p1", "stage": "4 - Active Pursuit", "reasoning": "Permit six months ago; work follows.", "adjustment": 7}]
        )
        out = run([prospect("p1", s={"trigger_date": "2026-02-20"})], prov)
        assert set(out["p1"]) == set(JUDGMENT_FIELDS)
        assert out["p1"]["pipeline_status"] == "4 - Active Pursuit"
        assert out["p1"]["ai_analysis"].startswith("Permit six months ago")
        assert out["p1"]["ai_score_adjustment"] == 7.0

    def test_one_call_per_prospect_by_default(self):
        # DEFAULT_BATCH_SIZE is 1 by ruling: quality over throughput.
        assert DEFAULT_BATCH_SIZE == 1
        calls = []

        def prov(promptext, **_kw):
            calls.append(promptext)
            pid = next(l for l in promptext.split("\n") if l.startswith("id: ")).split("id: ")[1]
            return json.dumps([{"id": pid, "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": 0}])

        out = run([prospect("p1"), prospect("p2"), prospect("p3")], prov)
        assert len(calls) == 3
        assert set(out) == {"p1", "p2", "p3"}

    def test_batching_still_works_when_raised(self):
        # The trade stays available as a config change rather than a rewrite.
        calls = []

        def prov(promptext, **_kw):
            calls.append(promptext)
            ids = [l.split("id: ")[1] for l in promptext.split("\n") if l.startswith("id: ")]
            return json.dumps(
                [{"id": i, "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": 0} for i in ids]
            )

        out = run([prospect(f"p{i}") for i in range(5)], prov, batch_size=5)
        assert len(calls) == 1
        assert len(out) == 5


class TestRefusals:
    """The guardrails. Each one exists because the alternative is silent."""

    def test_an_out_of_vocabulary_stage_falls_to_the_entry_rung_AND_SAYS_SO(self):
        # A stage AEO would refuse at the callback. Correcting it silently would be
        # indistinguishable from a judged placement.
        prov = responder([{"id": "p1", "stage": "Warm Lead", "reasoning": "looks good", "adjustment": 3}])
        out = run([prospect("p1")], prov)
        assert out["p1"]["pipeline_status"] == "1 - Early Discovery"
        assert "not in this skill's vocabulary" in out["p1"]["ai_analysis"]
        assert "looks good" in out["p1"]["ai_analysis"]

    def test_an_invented_prospect_id_is_dropped_not_attached_to_someone(self):
        prov = responder([{"id": "does-not-exist", "stage": "7 - Too Late", "reasoning": "r", "adjustment": 0}])
        out = run([prospect("p1")], prov)
        assert out == {}, "a verdict for an unknown id must not land on a real company"

    def test_a_duplicate_id_keeps_the_first_verdict(self):
        prov = responder(
            [
                {"id": "p1", "stage": "4 - Active Pursuit", "reasoning": "first", "adjustment": 1},
                {"id": "p1", "stage": "7 - Too Late", "reasoning": "second", "adjustment": -9},
            ]
        )
        out = run([prospect("p1")], prov)
        assert out["p1"]["pipeline_status"] == "4 - Active Pursuit"

    def test_an_unparseable_response_leaves_the_prospect_UNJUDGED(self):
        # Absent from the result, not present with a guess — the caller decides the
        # fallback and records that it was one.
        def prov(_p, **_kw):
            return "not json at all"

        out = run([prospect("p1")], prov, parse_json_array=lambda s: json.loads(s) if s.startswith("[") else [])
        assert out == {}

    def test_a_provider_exception_leaves_the_prospect_unjudged_and_does_not_fail_the_phase(self):
        def prov(_p, **_kw):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

        out = run([prospect("p1"), prospect("p2")], prov)
        assert out == {}

    def test_one_failing_prospect_does_not_lose_the_others(self):
        def prov(promptext, **_kw):
            pid = next(l for l in promptext.split("\n") if l.startswith("id: ")).split("id: ")[1]
            if pid == "p2":
                raise RuntimeError("boom")
            return json.dumps([{"id": pid, "stage": "4 - Active Pursuit", "reasoning": "r", "adjustment": 0}])

        out = run([prospect("p1"), prospect("p2"), prospect("p3")], prov)
        assert set(out) == {"p1", "p3"}

    def test_no_vocabulary_means_no_judgement_rather_than_an_invented_one(self):
        prov = responder([{"id": "p1", "stage": "anything", "reasoning": "r", "adjustment": 0}])
        assert run([prospect("p1")], prov, pipeline={"stages": []}) == {}

    def test_a_prospect_with_no_id_is_skipped(self):
        prov = responder([{"id": "p1", "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": 0}])
        assert run([{"company_name": "No id"}], prov) == {}


class TestAdjustmentBounds:
    def test_clamps_rather_than_rejects_an_out_of_range_adjustment(self):
        # 40 means "very good fit". Honouring the sign while bounding the magnitude
        # keeps the signal; rejecting it discards a real judgement over a format slip.
        prov = responder([{"id": "p1", "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": 40}])
        assert run([prospect("p1")], prov)["p1"]["ai_score_adjustment"] == 15.0

        prov = responder([{"id": "p1", "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": -99}])
        assert run([prospect("p1")], prov)["p1"]["ai_score_adjustment"] == -15.0

    def test_honours_the_configured_bounds(self):
        prov = responder([{"id": "p1", "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": 10}])
        out = run([prospect("p1")], prov, adjustment_bounds=(-5.0, 5.0))
        assert out["p1"]["ai_score_adjustment"] == 5.0

    def test_a_non_numeric_adjustment_becomes_zero_not_an_error(self):
        prov = responder([{"id": "p1", "stage": "1 - Early Discovery", "reasoning": "high", "adjustment": "high"}])
        assert run([prospect("p1")], prov)["p1"]["ai_score_adjustment"] == 0.0

class TestModelTier:
    """Judgment runs on its own tier. Measured against flash on real rows."""

    def _model_used(self, provider_config):
        used = {}

        def prov(_p, **kw):
            used["model"] = kw.get("model")
            return json.dumps(
                [{"id": "p1", "stage": "1 - Early Discovery", "reasoning": "r", "adjustment": 0}]
            )

        run([prospect("p1")], prov, provider_config=provider_config)
        return used.get("model")

    def test_prefers_judgment_model_over_the_shared_one(self):
        assert (
            self._model_used({"model": "flash-x", "judgment_model": "pro-y"}) == "pro-y"
        )

    def test_falls_back_to_the_shared_model_when_no_tier_is_set(self):
        # An older gateway or a hand-written provider block must still work.
        assert self._model_used({"model": "flash-x"}) == "flash-x"

    def test_an_empty_judgment_model_does_not_blank_the_model(self):
        # `or` rather than a bare `.get(...)` default: an empty string is a config
        # mistake, and passing it through would send model="" to the API.
        assert self._model_used({"model": "flash-x", "judgment_model": ""}) == "flash-x"

    def test_the_shipped_default_is_a_pinned_pro_id_not_a_floating_alias(self):
        from aeo.config_mapping import DEFAULT_PROVIDER

        tier = DEFAULT_PROVIDER["judgment_model"]
        assert "pro" in tier, "judgment must not silently run on the retrieval tier"
        # A floating alias would change the model under a running pipeline with no
        # deploy, so sales stages would shift with nothing in the diff to explain it.
        assert not tier.endswith("-latest"), "pin the id; never a floating alias"
        assert tier != DEFAULT_PROVIDER["model"], "the second tier must actually differ"

class TestEnginePrefersTheJudgement:
    """The vendored-engine edit (UPSTREAM.md, 2026-08-21).

    The stage AND its scoring weight must move together. Overwriting only the stage
    leaves the axis scored from the ladder we are replacing — a prospect reading
    "Decision Imminent" while carrying the 2-point "Too Late" weight.
    """

    CTX = {
        "organization": {"name": "Seller", "markets": []},
        "scoring": {"region_bonus": {"max": 10}},
        "sources": {},
    }

    def _score(self, prospect):
        import datetime

        import av_lead_scanner as als

        return als.score_prospects(
            [prospect], self.CTX, today=datetime.date(2026, 8, 21)
        )[0]

    def test_a_judged_stage_replaces_the_ladders(self):
        item = self._score(
            {
                "id": "p1",
                "company_name": "A All in One",
                "_ai_judgment": {
                    "pipeline_status": "6 - Likely Awarded",
                    "stage_score": 8,
                    "ai_analysis": "Permit 6 months ago; contractor likely selected.",
                },
            }
        )
        assert item["pipeline_status"] == "6 - Likely Awarded"

    def test_the_scoring_axis_follows_the_STAGE_not_the_ladder(self):
        # The reason this had to be an engine edit rather than a post-process.
        item = self._score(
            {
                "id": "p1",
                "_ai_judgment": {"pipeline_status": "6 - Likely Awarded", "stage_score": 8},
            }
        )
        assert item["score_factors"]["pipeline_timing"] == 8

    def test_falls_back_to_the_engines_own_weights_when_the_vocabulary_omits_one(self):
        # A declared vocabulary need not weight its rungs; the engine's `statuses`
        # table covers the shared ladder's keys.
        item = self._score(
            {"id": "p1", "_ai_judgment": {"pipeline_status": "4 - Active Pursuit"}}
        )
        assert item["pipeline_status"] == "4 - Active Pursuit"
        assert item["score_factors"]["pipeline_timing"] == 30

    def test_an_unweighted_unknown_stage_scores_zero_rather_than_borrowing(self):
        item = self._score(
            {"id": "p1", "_ai_judgment": {"pipeline_status": "Warm Lead"}}
        )
        assert item["pipeline_status"] == "Warm Lead"
        assert item["score_factors"]["pipeline_timing"] == 0

    def test_the_reasoning_reaches_pipeline_detail(self):
        item = self._score(
            {
                "id": "p1",
                "_ai_judgment": {
                    "pipeline_status": "4 - Active Pursuit",
                    "ai_analysis": "Lease signed last month; fit-out ahead.",
                },
            }
        )
        assert item["pipeline_detail"] == "Lease signed last month; fit-out ahead."

    def test_no_judgement_leaves_calculate_pipeline_in_charge(self):
        # The fallback that makes a failed model call degrade to previous behaviour
        # rather than to nothing.
        item = self._score({"id": "p1", "estimated_timeline": "completion 2028"})
        # Asserting the LADDER RAN, not which rung it picked: the rung depends on the
        # sales-cycle subtraction, and pinning it here would make this a test of
        # `calculate_pipeline` arithmetic rather than of the fallback path.
        assert item["pipeline_status"] in {s["key"] for s in PIPELINE["stages"]} | {
            "2 - Relationship Building", "3 - Design Influence", "6 - Likely Awarded"
        }
        assert "Est. completion" in item["pipeline_detail"], (
            "the ladder formats its own detail; model reasoning would not"
        )

    def test_the_adjustment_the_phase_supplies_reaches_the_item(self):
        # `ai_score_adjustment` is the field the engine ALREADY read and nothing ever
        # supplied — which is why ai_analysis was NULL on all 131 rows of the last run.
        item = self._score({"id": "p1", "ai_score_adjustment": -10})
        assert item["ai_score_adjustment"] == -10
