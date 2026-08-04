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
