"""Tests for the engine→AEO event mapping. Ours, not vendored.

Every field name and constraint asserted here was read off aeo-backend's
`scan-event.dto.ts`. If these drift, a scan produces results and loses them at the
callback — the run reports success and the prospects never arrive.
"""

from __future__ import annotations

from aeo.event_mapping import (
    MAX_ITEMS_PER_EVENT,
    PIPELINE_STATUS_KEY,
    map_event,
    map_prospects_event,
    map_scored_event,
    phase_name_for,
)


def _prospect(**over) -> dict:
    item = {
        "id": "11111111-1111-5111-8111-111111111111",
        "company_name": "First Church",
        "city": "Austin",
        "state": "TX",
        "address": "1 Main St",
        "website": "https://example.org",
        "discovery_data": {"source_count": 2},
    }
    item.update(over)
    return item


def _scored(**over) -> dict:
    item = {
        "prospect_id": "11111111-1111-5111-8111-111111111111",
        "company_name": "First Church",
        "score": 72,
        "rank": 1,
        "contact_name": "Jane Doe",
        "score_factors": {"fit": 20},
        # Vertical-shaped extras the engine really emits.
        "denomination": "Baptist",
        "campaign_goal": "$2M",
        "project_type": "sanctuary renovation",
        "pipeline_status": "in campaign",
    }
    item.update(over)
    return item


class TestProspectsMapping:
    def test_stamps_phase_and_phase_name_on_every_item(self):
        # The engine puts `phase` on the EVENT; AEO requires it on each ITEM. A
        # pass-through fails validation for every prospect in the sweep.
        out = map_prospects_event({"type": "prospects", "phase": "discover", "items": [_prospect()]})
        item = out[0]["data"][0]
        assert item["phase"] == "discover"
        assert item["phase_name"] == "Discovery sweep"

    def test_never_emits_an_empty_phase_name(self):
        # AEO declares phase_name as a required non-empty string, and discovery
        # phases are source keys we do not enumerate.
        out = map_prospects_event(
            {"type": "prospects", "phase": "church_architects", "items": [_prospect()]}
        )
        assert out[0]["data"][0]["phase_name"] == "Church architects"

    def test_passes_through_only_the_declared_columns(self):
        out = map_prospects_event({"type": "prospects", "phase": "discover", "items": [_prospect()]})
        assert set(out[0]["data"][0]) == {
            "id", "company_name", "city", "state", "address", "website",
            "discovery_data", "phase", "phase_name",
        }

    def test_omits_null_fields_rather_than_sending_them(self):
        out = map_prospects_event(
            {"type": "prospects", "phase": "discover", "items": [_prospect(website=None, city=None)]}
        )
        item = out[0]["data"][0]
        assert "website" not in item and "city" not in item

    def test_keeps_discovery_data_an_object(self):
        # Migration 071 reverted discovery_data from array to object. The engine
        # already emits an object; assert it so nobody "helpfully" wraps it.
        out = map_prospects_event({"type": "prospects", "phase": "discover", "items": [_prospect()]})
        assert isinstance(out[0]["data"][0]["discovery_data"], dict)


class TestScoredMapping:
    def test_passes_the_five_declared_fields_top_level(self):
        out = map_scored_event({"type": "scored", "items": [_scored()]})
        item = out[0]["data"][0]
        for field in ("prospect_id", "contact_name", "score", "rank", "score_factors"):
            assert field in item, field

    def test_does_NOT_bucket_undeclared_fields_into_scoring_payload(self):
        # AEO reads exactly one key out of scoring_payload and discards the rest, so
        # a bucket here would look durable on the wire and persist nothing. The
        # extras are already durable in discovery_data.by_source on the prospects
        # event, which IS a persisted open JSONB column.
        out = map_scored_event({"type": "scored", "items": [_scored()]})
        payload = out[0]["data"][0]["scoring_payload"]
        assert set(payload) == {"pipeline_status"}

    def test_pipeline_status_travels_in_scoring_payload_not_top_level(self):
        # The DESIGNED channel: AEO reads scoring_payload.pipeline_status and assigns
        # it set-once (COALESCE), so a skill seeds the value and an operator edit is
        # never overwritten. Not a collision — an earlier version of this test said
        # it was, and the live run disproved it.
        out = map_scored_event({"type": "scored", "items": [_scored()]})
        item = out[0]["data"][0]
        assert PIPELINE_STATUS_KEY not in item
        assert item["scoring_payload"][PIPELINE_STATUS_KEY] == "in campaign"

    def test_pipeline_source_rides_along_when_the_model_judged_the_stage(self):
        # 🔑 The marker AEO needs to KEEP a stage instead of re-deriving it. Without it,
        # AEO discards a customer skill's stage unconditionally and buckets months
        # itself -- measured on run 222e758b, '4 - Active Pursuit' persisted as
        # '7 - Too Late'. A bare stage cannot express "a model decided this".
        out = map_scored_event(
            {"type": "scored", "items": [_scored(pipeline_source="ai")]}
        )
        payload = out[0]["data"][0]["scoring_payload"]
        assert payload["pipeline_source"] == "ai"
        assert payload[PIPELINE_STATUS_KEY] == "in campaign"

    def test_pipeline_source_is_ABSENT_when_the_date_ladder_chose_the_stage(self):
        # The other half, and the reason this is a marker rather than a flag AEO can
        # assume: the engine still falls back to `calculate_pipeline` for a prospect the
        # judge did not reach, and that value must keep falling through to derivation.
        # Absent == derived, so `calculate_pipeline` needed no new key.
        out = map_scored_event({"type": "scored", "items": [_scored()]})
        assert "pipeline_source" not in out[0]["data"][0]["scoring_payload"]

    def test_drops_an_item_with_no_prospect_id(self):
        # prospect_id is AEO's only required field; without it the item cannot be
        # attached to anything, so dropping beats a 400 that loses the batch. And
        # once every item is dropped there is nothing to post at all — see
        # TestEmptyResults.
        out = map_scored_event({"type": "scored", "items": [_scored(prospect_id=None)]})
        assert out == []


class TestEmptyResults:
    """A zero-result sweep is a real outcome, not a failed run.

    AEO declares `@ArrayMinSize(1)` on every data array, so posting `{"data": []}`
    is a 400 — which would flip a legitimately empty scan to `failed`. Found by
    running against a live gateway; no amount of reading the engine would have
    surfaced it, because the engine is perfectly happy to emit an empty event.
    """

    def test_no_post_at_all_for_an_empty_prospects_event(self):
        assert map_prospects_event({"type": "prospects", "phase": "discover", "items": []}) == []

    def test_no_post_at_all_for_an_empty_scored_event(self):
        assert map_scored_event({"type": "scored", "items": []}) == []

    def test_map_event_yields_nothing_rather_than_an_empty_batch(self):
        assert map_event({"type": "prospects", "phase": "discover", "items": []}) == []


class TestCompletedSummary:
    def test_keeps_only_the_four_declared_counters(self):
        # The engine also puts `provider` in its summary. Relying on the global
        # ValidationPipe to strip it would make the payload depend on a pipe
        # setting rather than on this contract.
        out = map_event(
            {
                "type": "completed",
                "summary": {"total_prospects": 3, "total_scored": 3, "provider": "mock"},
            }
        )
        assert out == [("completed", {"summary": {"total_prospects": 3, "total_scored": 3}})]

    def test_drops_non_numeric_counters(self):
        out = map_event({"type": "completed", "summary": {"total_prospects": "three"}})
        assert out == [("completed", {"summary": {}})]

    def test_forwards_the_cost_meter_snapshot(self):
        """🔑 The assertion that was missing, and the whole reason the meter shipped mute.

        `7ecc9ce` added the meter, its call site in all six phases, the emission in
        `runner.py` and 514 lines of tests — and never touched this file. So the first
        production run of the new build (e9b5c7f5) wrote all four counters correctly and
        left `scan_runs.actual_cost` and `cost_breakdown` NULL. Every one of those 514
        lines asserted the meter's OWN state; none asserted the payload that actually
        leaves the process.
        """
        cost = {
            "calls": 367,
            "grounded_requests": 322,
            "grounded_search_queries": 340,
            "by_phase": [{"phase": "geography", "calls": 96}],
            "search_histogram": {"0": 28, "1": 294},
        }
        out = map_event(
            {"type": "completed", "summary": {"total_prospects": 96, "cost": cost}}
        )
        assert out == [("completed", {"summary": {"total_prospects": 96, "cost": cost}})]

    def test_cost_survives_the_guard_the_counters_need(self):
        """The half of the bug an allowlist fix alone would NOT have caught: `cost` is a
        dict, so adding it to the counter tuple would still have dropped it on
        `isinstance(..., (int, float))`. Two defects, one symptom."""
        out = map_event({"type": "completed", "summary": {"cost": {"calls": 1}}})
        assert out[0][1]["summary"]["cost"] == {"calls": 1}

    def test_omits_cost_entirely_rather_than_sending_an_empty_one(self):
        """An empty dict is NOT the same as absent downstream. The gateway writes
        `s.cost ? JSON.stringify(s.cost) : null`, and `{}` is truthy in JS — so an empty
        cost persists as `{}` and prices to a confident $0.00, which reads as "measured,
        and it was free" instead of "unmeasured". Absent keeps the column NULL."""
        for empty in ({}, None, "not-a-dict", 0, []):
            out = map_event(
                {"type": "completed", "summary": {"total_scored": 1, "cost": empty}}
            )
            assert out == [("completed", {"summary": {"total_scored": 1}})], empty


class TestBatching:
    def test_splits_above_the_aeo_item_cap(self):
        # AEO's @ArrayMaxSize(1000) rejects the WHOLE event, so an oversized sweep
        # would lose every prospect rather than the overflow.
        items = [_prospect(id=f"id-{i}") for i in range(MAX_ITEMS_PER_EVENT + 1)]
        out = map_prospects_event({"type": "prospects", "phase": "discover", "items": items})
        assert len(out) == 2
        assert len(out[0]["data"]) == MAX_ITEMS_PER_EVENT
        assert len(out[1]["data"]) == 1

    def test_one_batch_when_under_the_cap(self):
        out = map_prospects_event({"type": "prospects", "phase": "discover", "items": [_prospect()]})
        assert len(out) == 1


class TestEventRouting:
    def test_routes_each_engine_type_to_its_aeo_type(self):
        assert map_event({"type": "prospects", "phase": "discover", "items": [_prospect()]})[0][0] == "prospects"
        assert map_event({"type": "scored", "items": [_scored()]})[0][0] == "scored"
        assert map_event({"type": "completed", "summary": {"total_prospects": 1}})[0] == (
            "completed", {"summary": {"total_prospects": 1}},
        )
        assert map_event({"type": "error", "message": "boom"})[0] == (
            "error", {"message": "boom"},
        )

    def test_progress_and_unknown_events_have_no_destination(self):
        # Empty list, not an exception: these are legitimate engine events that AEO
        # simply has nowhere to durably put.
        assert map_event({"type": "phase_start", "phase": "x"}) == []
        assert map_event({"type": "something_new"}) == []


class TestPhaseNames:
    def test_known_phases(self):
        assert phase_name_for("discover") == "Discovery sweep"
        assert phase_name_for("score") == "Scoring"

    def test_unknown_phase_never_yields_empty(self):
        assert phase_name_for("") == "Unknown"
        assert phase_name_for("municipal_permits") == "Municipal permits"


class TestScoredContactPassthrough:
    """Regression: a run found 17 contacts and persisted 17 names, zero emails.

    `aeo/phases/contacts.py` writes all five contact fields onto the prospect and
    AEO's `ScanScoredItemDto` declares all five, but `SCORED_PASSTHROUGH` named only
    `contact_name` — so the whitelist silently dropped the rest. Asserting each field
    individually so a future trim of the tuple names the field it broke.
    """

    ITEM = {
        "prospect_id": "11111111-1111-1111-1111-111111111111",
        "contact_name": "Jeff Lewis",
        "contact_title": "Facilities Director",
        "contact_email": "jeff@example.com",
        "contact_phone": "+1-803-555-0100",
        "contact_linkedin": "https://www.linkedin.com/in/jefflewis",
        "contacts_data": {"guess": {"contact_email": "j.lewis@example.com"}},
        "score": 76,
        "rank": 1,
    }

    def _mapped(self):
        out = map_scored_event({"type": "scored", "items": [dict(self.ITEM)]})
        assert len(out) == 1 and len(out[0]["data"]) == 1
        return out[0]["data"][0]

    def test_every_column_backed_contact_field_reaches_the_payload(self):
        got = self._mapped()
        for field in (
            "contact_name",
            "contact_title",
            "contact_email",
            "contact_phone",
            "contact_linkedin",
        ):
            assert got.get(field) == self.ITEM[field], f"{field} was dropped"

    def test_contacts_data_survives_as_an_object(self):
        assert self._mapped().get("contacts_data") == self.ITEM["contacts_data"]

    def test_scoring_fields_still_pass(self):
        got = self._mapped()
        assert got["score"] == 76 and got["rank"] == 1

class TestWhitelistParity:
    """🔴 The bug that has shipped from `SCORED_PASSTHROUGH` three times.

    Once as `contact_name` alone -- a run found 17 contacts and persisted 17 names and
    zero emails. Once as `industry`/`website`, collected on every prospect and NULL in
    both columns. Once as `ai_analysis`/`ai_score_adjustment`, which would have made the
    model reasoning computed, paid for, and dropped one line before the wire.

    The pattern is identical every time: a field the engine produces, a column AEO
    declares, and one tuple in the middle that does not name it. Nothing errors.

    So this asserts PARITY rather than a list. A new field is either whitelisted or
    named here as deliberately excluded -- both are fine, silence is not.
    """

    #: Fields the engine puts on a scored item that AEO deliberately does NOT persist.
    #: Each needs a reason, because "we forgot" and "we chose not to" look identical in
    #: a diff a year later.
    DELIBERATELY_EXCLUDED = {
        # AEO derives its own ordering from `score`; a second one would drift.
        "company_name": "AEO already holds it from the prospects event",
        "city": "same",
        "state": "same",
        "contact_name": "carried, but as a top-level item field not via scoring",
        "pipeline_detail": "AEO has no column; the reasoning goes to ai_analysis",
        "estimated_completion": "no column",
        "estimated_decision": "no column",
        "months_to_decision": "no column",
        "fields": "AEO holds the authored fields in discovery_data",
        "sources_found_in": "AEO holds it on the prospect row",
        "multi_source": "derivable from sources_found_in",
        # 🔴 REMOVED 2026-08-24. This entry read `"disqualified": "carried on the
        # validations event, not scoring"` and that justification was FALSE:
        # `disqualified` is produced by `score_prospects`, on the scoring path, from
        # `scoring.disqualify_below`. So the guard built to catch exactly this omission
        # was disarmed by a wrong comment — and the omission then shipped, with all three
        # production skills authoring `disqualify_below: 40` and the flag dropped one line
        # before the wire on every real run.
        #
        # Worse, the entry was never even exercised: the fixture below authors no
        # `disqualify_below`, so `disqualified` was absent from `produced` and the
        # exclusion did nothing. An untested exclusion is a comment, not a guard.
        # `disqualified`, `disqualifier_reason` and `priority_band` are now whitelisted,
        # and `test_disqualification_and_band_reach_the_wire` exercises them.
        # NOT an omission: `pipeline_status` rides inside `scoring_payload`, which is
        # the DESIGNED channel (corrected 2026-08-04 after a live run -- an earlier
        # version of event_mapping.py called it a name collision and warned against
        # mapping it, which was wrong). The gateway reads
        # `scoring_payload.pipeline_status`, not a top-level field.
        "pipeline_status": "designed channel is scoring_payload, not top-level",
        # Same channel as `pipeline_status`, and for the same reason -- it exists only
        # to qualify that value. AEO has no column for it; it decides whether AEO KEEPS
        # the stage (a model judgement) or re-derives it (the date-ladder fallback),
        # then stores its own `prospects.pipeline_status_source`. Added 2026-08-22
        # after AEO was measured discarding every verdict for a customer skill.
        "pipeline_source": "designed channel is scoring_payload, not top-level",
        "_ai_judgment": "internal handoff between the phase and the engine",
        "stage_score": "internal: feeds the engine's axis, not a column",
    }

    def test_every_scored_field_is_whitelisted_or_deliberately_excluded(self):
        import datetime

        import av_lead_scanner as als
        from aeo.event_mapping import SCORED_PASSTHROUGH

        scored = als.score_prospects(
            [
                {
                    "id": "p1",
                    "company_name": "Acme Floors",
                    "_ai_judgment": {
                        "pipeline_status": "4 - Active Pursuit",
                        "stage_score": 30,
                        "ai_analysis": "permit six months ago",
                        "ai_score_adjustment": 5,
                    },
                }
            ],
            {
                "organization": {"name": "Seller", "markets": []},
                "scoring": {"region_bonus": {"max": 10}},
                "sources": {},
            },
            today=datetime.date(2026, 8, 21),
        )
        assert scored, "no scored item to inspect"

        produced = set(scored[0])
        unaccounted = produced - set(SCORED_PASSTHROUGH) - set(self.DELIBERATELY_EXCLUDED)
        assert not unaccounted, (
            "scored fields that reach neither AEO nor this exclusion list: "
            f"{sorted(unaccounted)}. Add them to SCORED_PASSTHROUGH if AEO has a "
            "column, or to DELIBERATELY_EXCLUDED with a reason. This is the fourth "
            "occurrence of the same omission if you skip it."
        )

    def test_the_ai_fields_specifically_reach_the_wire(self):
        # Named separately from the parity check: parity would also pass if someone
        # "fixed" it by adding them to the exclusion list instead.
        from aeo.event_mapping import SCORED_PASSTHROUGH

        assert "ai_analysis" in SCORED_PASSTHROUGH
        assert "ai_score_adjustment" in SCORED_PASSTHROUGH
