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
    """A prospect in the shape the discovery phase leaves behind.

    ⚠️ This claim was **false for four months** and cost two production runs. The key
    was `_by_source`, which `av_lead_scanner.build_prospects` has never written — it
    writes `discovery_data.by_source`. Every assertion in this file therefore passed
    against a shape that does not exist, while the judge in production read an empty
    dict and placed all 84 prospects at the entry rung.

    A hand-written fixture cannot testify about a producer it never calls, no matter
    what its docstring claims. `TestReadsTheRealProducerShape` below closes that by
    going through `build_prospects` itself.
    """
    return {
        "id": pid,
        "company_name": f"Company {pid}",
        "discovery_data": {"by_source": by_source or {}},
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


class TestReadsTheRealProducerShape:
    """The gap that made every other test in this file vacuous.

    `ai_judgment` read `prospect["_by_source"]`. `av_lead_scanner._assemble_prospects`
    writes `discovery_data.by_source`. Nothing wrote `_by_source` — not the producer,
    not the runner, not the engine — so the judge's event loop iterated an empty dict on
    every prospect of every production run, and the prompt fell through to
    "event: none found". The model then did exactly as instructed with no signal and
    placed all 84 judged prospects of 2026-08-21 at the entry rung.

    Every assertion above passed throughout, because the fixture invented the same wrong
    key. So these tests go through the PRODUCER: if the two shapes ever diverge again,
    the failure lands here rather than in production.
    """

    @staticmethod
    def _produced(raw: dict, source: str = "in_market_triggers"):
        import av_lead_scanner as als

        groups = {
            als.normalize_name(raw["company_name"]): [
                {"raw": raw, "source": source, "name_field": "company_name"}
            ]
        }
        return als._assemble_prospects(
            groups=groups, scan_run_id="r1", canonical=tuple(raw)
        )[0]

    def test_reads_the_shape_the_producer_actually_writes(self):
        p = self._produced(
            {
                "company_name": "Highwoods Properties",
                "trigger_type": "Commercial Building Permit",
                "trigger_date": "2026-08-20",
            }
        )
        prov = responder(
            [{"id": p["id"], "stage": "4 - Active Pursuit", "reasoning": "r", "adjustment": 0}]
        )
        run([p], prov)
        line = next(l for l in prov.seen["prompt"].split("\n") if l.startswith("event:"))
        assert "Commercial Building Permit" in line
        assert "2026-08-20" in line

    def test_the_producer_does_not_write_the_key_this_module_used_to_read(self):
        # Stated as its own assertion so the reason the above works is not a coincidence
        # anybody can silently undo.
        p = self._produced({"company_name": "Acme", "trigger_date": "2026-01-01"})
        assert "_by_source" not in p
        assert "by_source" in p["discovery_data"]

    def test_the_hand_written_fixture_matches_the_producer(self):
        # Keeps `prospect()` honest about the claim in its docstring.
        produced = self._produced({"company_name": "Acme", "trigger_date": "2026-01-01"})
        fixture = prospect("p1", s={"trigger_date": "2026-01-01"})
        assert set(fixture["discovery_data"]) <= set(produced["discovery_data"])
        assert "by_source" in fixture["discovery_data"]


class TestSignalFieldsAreNotKeyedToOneVertical:
    """`signal_fields` used to default to `["trigger_date", "transaction_date"]`.

    Those are the field names of ONE skill (`commercial-flooring`). Measured on the
    production copy: that pair matched 220 of 389 of its own prospects, **0 of 15 for a
    second org that had 8 date-shaped fields**, and 0 of 497 for a third whose sources
    ask for no date at all. The resolution is now by SHAPE, via the engine's own
    `timing_fields_from_authored`, so a vertical authored next year works unedited.
    """

    @staticmethod
    def _resolve(rows: list[dict], declared=None):
        from aeo.runner import _resolve_signal_fields

        prospects = [
            {"id": f"p{i}", "discovery_data": {"by_source": {"s": row}}}
            for i, row in enumerate(rows)
        ]
        return _resolve_signal_fields({"signal_fields": declared or []}, prospects)

    def test_finds_a_date_field_no_skill_declared(self):
        assert "permit_date" in self._resolve([{"permit_date": "2026-02-20"}])

    def test_finds_the_universal_pair_this_change_appends(self):
        assert "event_date" in self._resolve([{"event_date": "2026-02-20"}])

    def test_finds_a_timeline_that_is_not_spelled_date(self):
        assert "estimated_timeline" in self._resolve([{"estimated_timeline": "Q2 2027"}])

    def test_an_operators_declaration_always_survives(self):
        # Even one the shape vocabulary would never match on its own.
        out = self._resolve([{"closing": "2026-05-01"}], declared=["closing"])
        assert "closing" in out

    def test_ignores_a_field_that_is_not_timing_shaped(self):
        assert self._resolve([{"square_footage": "120,000"}]) == []

    def test_the_old_hardcoded_pair_is_gone(self):
        # It must no longer appear from nowhere: a run whose data contains neither name
        # resolves to neither name.
        assert self._resolve([{"square_footage": "1"}]) == []


class TestShapeMatchedFieldsMustLookLikeDates:
    """`signal_fields` is resolved by NAME shape, and this phase does not parse.

    The engine can match on shape safely because `timing_fields_from_authored` feeds a
    parser — a non-date simply fails to parse and costs nothing. Here the value goes
    straight into a prompt as `dated <value>`, so a field matched on its name alone can
    put prose in front of the model as timing evidence. `candidate_name` is the concrete
    case: it normalises to `candidatename`, which contains "date".
    """

    def _lines(self, row, fields):
        from aeo.phases.ai_judgment import _prospect_lines

        return _prospect_lines([prospect("p1", s=row)], fields)

    def test_a_name_matched_field_holding_prose_is_not_offered_as_an_event(self):
        out = self._lines({"candidate_name": "Bob Smith"}, ["candidate_name"])
        assert "dated Bob Smith" not in out
        assert "event: none found" in out

    def test_a_bare_year_still_counts(self):
        assert "dated 2019" in self._lines({"trigger_date": "2019"}, ["trigger_date"])

    def test_a_quarter_still_counts(self):
        assert "dated Q2 2027" in self._lines(
            {"estimated_timeline": "Q2 2027"}, ["estimated_timeline"]
        )

    def test_a_month_and_year_still_counts(self):
        assert "dated March 2026" in self._lines(
            {"event_date": "March 2026"}, ["event_date"]
        )


class TestTheReasoningInstruction:
    """The `ai_analysis` prose contract, asked for by the PO via aeo-frontend.

    Thread: `aeo-triage/prospect-pipeline-stage.md`. The panel's Overview tab no longer
    has a "Why this stage" heading, so this sentence is now the **opening paragraph a
    salesperson reads about the company** — unlabelled. Measured before the change: 84
    rows, 123–209 characters, average 159, i.e. one or two sentences citing a single
    signal.

    Pinned as assertions because a prompt is a string: every test in this file passed
    unchanged when the instruction was added, so nothing here would have noticed it being
    dropped again.
    """

    def _prompt(self) -> str:
        from aeo.phases import ai_judgment

        return ai_judgment._PROMPT

    def test_asks_for_three_or_four_sentences(self):
        assert "Three or four sentences" in self._prompt()

    def test_forbids_opening_on_the_models_own_procedure(self):
        # The PO's actual complaint: "the beginning ... says the agent read this
        # prospect's dated event and what kind of events it was and picked the stage. We
        # can get rid of that." Half of that was frontend copy; this is our half.
        p = self._prompt()
        assert "Open on the evidence" in p
        assert "Never open on your own procedure" in p

    def test_carries_the_UI_length_ceiling_and_says_why(self):
        # A soft ceiling, and the reason matters: past it the panel pushes the company's
        # address and contact below the fold on the tab where a salesperson looks for
        # them. Without the reason a future editor drops it as an arbitrary number.
        p = self._prompt()
        assert "700 characters" in p
        assert "address" in p and "contact" in p

    def test_keeps_the_field_scoped_to_STAGE_reasoning(self):
        # 🔴 Load-bearing. `ai_analysis` SURVIVES an operator move — the gateway rewrites
        # status and source and leaves the prose — so on a dragged prospect it argues for
        # the rung the prospect just left. aeo-frontend renders it only while
        # `pipeline_status_source === 'ai'` for exactly that reason. A longer, more
        # confident paragraph makes that worse if anything ever renders it
        # unconditionally, so the field must not drift into a general company summary.
        p = self._prompt()
        assert "Stay stage reasoning" in p
        assert "not a general summary" in p

    def test_asks_it_to_name_real_fields_and_not_invent_them(self):
        # ⚠️ The judge's payload holds ONLY id, company, industry, address/city/state and
        # dated events with their types (`_prospect_lines`). It does NOT see firm type,
        # related property owner, validation signals or score axes. Asking for "all the
        # data points" without this guard would buy fabrication rather than detail.
        p = self._prompt()
        assert "Do not list a field you were not given" in p

    def test_still_asks_for_the_timing_implication(self):
        assert "means for TIMING" in self._prompt()


# ── billing: this phase must not pay the grounded-search meter ───────────────


def _kwarg_recording_provider(payload):
    """A provider that records the KEYWORDS it was called with, not just the prompt.

    `responder` above swallows them into `**_kw`, which is fine for prompt assertions
    and useless here: the thing under test IS a keyword. A double that discards the
    argument cannot test the argument.
    """
    calls: list[dict] = []

    def provider(promptext, **kw):
        calls.append(kw)
        return json.dumps(payload)

    provider.calls = calls  # type: ignore[attr-defined]
    return provider


def test_judgment_calls_the_provider_ungrounded():
    """🔑 The regression that cost ~$800.

    `gemini_provider` attached the Google Search tool unconditionally, so every
    judgment call was a billed grounded search request on the pro model — one per
    prospect — for a phase that asks the model for no external research. The module
    docstring had asserted these calls were ungrounded for the phase's whole life.

    Asserted on the wire rather than on the docstring, because a comment cannot fail.
    """
    provider = _kwarg_recording_provider(
        [{"id": "p1", "stage": "3 - Evaluating", "reasoning": "r"}]
    )
    run([prospect("p1")], provider)

    assert provider.calls, "the phase never called the provider"
    for kw in provider.calls:
        assert kw.get("grounded") is False, (
            "judgment must pass grounded=False — dropping it silently restores a "
            "billed search request per prospect on the most expensive model"
        )


def test_judgment_tags_its_calls_for_cost_attribution():
    """Per-phase cost reporting is only real if each phase names itself.

    Without this the whole judgment spend lands in the meter's `unknown` bucket, and
    the estimate-vs-actual report FE renders cannot show where money went.
    """
    provider = _kwarg_recording_provider(
        [{"id": "p1", "stage": "3 - Evaluating", "reasoning": "r"}]
    )
    run([prospect("p1")], provider)
    assert {kw.get("phase") for kw in provider.calls} == {"judgment"}
