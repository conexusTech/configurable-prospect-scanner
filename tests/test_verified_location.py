"""A lead is located by the address the geography step VERIFIED, and that is all the wire carries.

**Why this exists.** The geography loop pays a grounded call per candidate to establish where a
firm really is, then used the answer only to keep or reject. For a KEPT lead the verified address
was thrown away: the stored row, the score and the wire all kept whatever discovery happened to
report -- and discovery only yields a state when the source field is literally named `state`,
`location` or `address`. A source that answers `headquarters_location: "Austin, TX"` produced a
lead with NO state, so a gated skill's target-market gate failed it closed, silently.

These tests pin the change: a kept, verified lead carries the normalised verified location on
the record AND on `_internal` (the scorer reads `_internal`, never the record), the wire carries
a location only for leads that were verified, and a gated run whose target-market gate cannot be
evaluated says so.

Leads are built THROUGH the real producer (`_assemble_prospects`) and scored by the real scorer,
for the reason `test_score_explanation.TestAttachesToTheProducerShape` records: a hand-written
dict without `_internal` would pass while the score still used discovery's location.
"""

from __future__ import annotations

import json
from datetime import date

import av_lead_scanner as als
from aeo.event_mapping import map_scored_event
from aeo.phases.geo_filter import build_target_area
from aeo.phases.geo_loop import discover_in_area
from tests.test_gated_score import CFG

TODAY = date(2026, 8, 27)

# The gateway's list: 50 states plus DC. Must match `aeo-backend` exactly -- a code the gateway
# does not know makes it ignore the whole location.
GATEWAY_STATE_CODES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
    "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
).split()

AUSTIN_AREA = build_target_area([{"zip_code": "78701", "city": "Austin", "state": "TX"}], None)
CHARLOTTE_AREA = build_target_area(
    [
        {"zip_code": "78701", "city": "Austin", "state": "TX"},
        {"zip_code": "28202", "city": "Charlotte", "state": "NC"},
    ],
    None,
)


def _parse(text: str) -> list[dict]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [p for p in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(p, dict)]


def _verifier(by_name: dict[str, dict]):
    """Provider that answers location questions from a lookup table; anything else gets none."""

    def call(prompt, **kwargs):
        for name, answer in by_name.items():
            if name in prompt:
                return json.dumps([answer])
        return "[]"

    return call


def _produce(raw: dict, *, scan_run_id: str = "r1") -> dict:
    """One prospect from the real assembler -- carries `_internal`, as every real lead does."""
    groups = {
        als.normalize_name(raw["company_name"]): [
            {"raw": raw, "source": "in_market_triggers", "name_field": "company_name"}
        ]
    }
    return als._assemble_prospects(groups=groups, scan_run_id=scan_run_id, canonical=tuple(raw))[0]


def _run_loop(prospect: dict, *, area, verifier: dict[str, dict], log=lambda _: None):
    """The real geography loop over one discovered prospect, with a stub verifier."""
    return discover_in_area(
        log=log,
        tool_context={"sources": {"s": {}}},
        area=area,
        target_count=1,
        discover=lambda ctx: [prospect],
        provider=_verifier(verifier),
        provider_config={},
        parse_json_array=_parse,
        max_rounds=1,
    )


def _gated_scoring(allowed_states: list[str]) -> dict:
    target = {**CFG["gate"]["target_market"], "allowed_states": allowed_states}
    return {**CFG, "score_cap": 100, "gate": {**CFG["gate"], "target_market": target}}


def _score(prospects: list[dict], scoring: dict | None = None) -> list[dict]:
    """Scored items from the real scorer, under a gated config (NC unless told otherwise)."""
    for p in prospects:
        p["validation_data"] = {
            "switching_signal": [
                {
                    "signal_type": "rfp activity",
                    "signal_class": "rfp_active",
                    "signal_date": "2026-08-01",
                    "signal_description": "Opened a broker review.",
                }
            ]
        }
        p["_ai_judgment"] = {"pipeline_status": "4 - Active Pursuit"}
    ctx = {
        "scoring": scoring or _gated_scoring(["NC"]),
        "pipeline": {
            "stages": [
                {"key": "4 - Active Pursuit", "min_months": 4, "max_months": 8, "kind": "timing"}
            ]
        },
        "skill_type": "customer",
    }
    return als.score_prospects(prospects, ctx, today=TODAY)


def _wire_item(item: dict) -> dict:
    payloads = map_scored_event({"items": [item]})
    assert len(payloads) == 1 and len(payloads[0]["data"]) == 1
    return payloads[0]["data"][0]


class TestNormalisation:
    """The gateway applies a location only when state is one of 51 codes, zip is 5 or 9 digits and
    city fits 255 -- anything else it ignores whole. So the scanner must send only values that
    already satisfy that, which means normalising here rather than hoping.
    """

    def test_state_names_and_codes_normalise(self):
        from aeo.us_states import US_STATE_CODES, normalize_state

        assert set(US_STATE_CODES) == set(GATEWAY_STATE_CODES)
        assert len(set(US_STATE_CODES)) == 51 and len(US_STATE_CODES) == 51

        assert normalize_state("north carolina") == "NC"
        assert normalize_state(" NC ") == "NC"
        assert normalize_state("nc") == "NC"
        assert normalize_state("District of Columbia") == "DC"
        assert normalize_state("New York") == "NY"
        # Not a US state: the Canadian province's code collides with nothing, but must not pass.
        assert normalize_state("Ontario") is None
        assert normalize_state("ON") is None
        assert normalize_state("Puerto Rico") is None
        assert normalize_state("") is None
        assert normalize_state("   ") is None
        assert normalize_state(None) is None
        # Every code in the gateway's list normalises to itself.
        for code in GATEWAY_STATE_CODES:
            assert normalize_state(code) == code

    def test_zip_and_city_normalise(self):
        from aeo.us_states import normalize_city, normalize_zip

        assert normalize_zip("28202") == "28202"
        assert normalize_zip("28202-1234") == "28202-1234"
        assert normalize_zip(" 28202 ") == "28202"
        assert normalize_zip("2820") is None
        assert normalize_zip("ABCDE") is None
        assert normalize_zip("282021234") is None
        assert normalize_zip("") is None
        assert normalize_zip(None) is None

        assert normalize_city("  Charlotte ") == "Charlotte"
        assert normalize_city("x" * 255) == "x" * 255
        assert normalize_city("x" * 256) is None
        assert normalize_city("   ") is None
        assert normalize_city("") is None
        assert normalize_city(None) is None

    def test_zip_is_ascii_digits_only(self):
        # `\d` alone matches every Unicode decimal digit, including fullwidth ones, which the
        # gateway's own check refuses -- and a refused location is dropped whole.
        from aeo.us_states import normalize_zip

        assert normalize_zip("２８２０２") is None
        assert normalize_zip("٢٨٢٠٢") is None
        assert normalize_zip("28202-１２３４") is None
        assert normalize_zip("28202") == "28202"

    def test_city_refuses_control_characters(self):
        # A NUL fails the gateway's whole-batch UPDATE, not just this lead -- the rule
        # `validate_explanation` applies to the explanation text for the same reason.
        from aeo.us_states import normalize_city

        assert normalize_city("Char\x00lotte") is None
        assert normalize_city("Char\nlotte") is None
        assert normalize_city("Char\tlotte") is None
        assert normalize_city("Char\x7flotte") is None
        # Trimming happens first, so edge whitespace is not a control character.
        assert normalize_city("Charlotte\n") == "Charlotte"


class TestGeoLoopAppliesTheVerifiedAddress:
    """Verification already decides keep/reject; its answer must also be what a kept lead IS.

    Using the same evidence for one decision and not the other would make them disagree: a lead
    kept because it is in Charlotte would still be stored, scored and sent as Austin.
    """

    def test_a_kept_verified_lead_carries_the_verified_location(self):
        prospect = _produce({"company_name": "Acme Benefits", "city": "Austin", "state": "TX"})
        assert (prospect["city"], prospect["state"]) == ("Austin", "TX")
        assert (prospect["_internal"]["city"], prospect["_internal"]["state"]) == ("Austin", "TX")

        in_area, rejects = _run_loop(
            prospect,
            area=CHARLOTTE_AREA,
            verifier={
                "Acme Benefits": {"city": "Charlotte", "state": "North Carolina", "zip_code": "28202"}
            },
        )

        assert rejects == [] and len(in_area) == 1
        kept = in_area[0]
        # The record (what is stored) ...
        assert (kept["city"], kept["state"], kept["zip_code"]) == ("Charlotte", "NC", "28202")
        # ... and `_internal` (what the scorer reads): either still saying Austin fails the check.
        internal = kept["_internal"]
        assert (internal["city"], internal["state"], internal["zip_code"]) == (
            "Charlotte",
            "NC",
            "28202",
        )
        assert kept["verified_location"] == {
            "city": "Charlotte",
            "state": "NC",
            "zip_code": "28202",
        }

    def test_an_unverifiable_lead_keeps_discovery_and_no_marker(self):
        # An unanswerable verification is kept -- unverifiable is never rejected -- and with no
        # evidence there is nothing to apply, so discovery's location must stand untouched.
        prospect = _produce({"company_name": "Ghost Co", "city": "Austin", "state": "TX"})

        in_area, rejects = _run_loop(prospect, area=AUSTIN_AREA, verifier={})

        assert rejects == [] and len(in_area) == 1
        kept = in_area[0]
        assert (kept["city"], kept["state"]) == ("Austin", "TX")
        assert (kept["_internal"]["city"], kept["_internal"]["state"]) == ("Austin", "TX")
        assert "verified_location" not in kept

    def test_a_non_us_verified_state_keeps_discovery_and_no_marker(self):
        # The area includes a city named London, and `classify_prospect` matches a CITY before it
        # looks at the state -- so a verifier answering London, "Ontario" is still classified
        # in-area and the lead is KEPT. That is the case this check needs: kept, but the verified
        # state is not one of the 51, so nothing may be applied and nothing may be sent.
        area = build_target_area(
            [
                {"zip_code": "78701", "city": "Austin", "state": "TX"},
                {"zip_code": "78702", "city": "London", "state": "TX"},
            ],
            None,
        )
        prospect = _produce({"company_name": "Maple Co", "city": "Austin", "state": "TX"})

        in_area, rejects = _run_loop(
            prospect,
            area=area,
            verifier={"Maple Co": {"city": "London", "state": "Ontario", "zip_code": "N6A 3K7"}},
        )

        assert rejects == [], "the classifier matches the city first, so this lead must be kept"
        assert len(in_area) == 1
        kept = in_area[0]
        assert (kept["city"], kept["state"]) == ("Austin", "TX")
        assert (kept["_internal"]["city"], kept["_internal"]["state"]) == ("Austin", "TX")
        assert kept.get("zip_code") is None
        assert "verified_location" not in kept

    def test_a_rejected_lead_is_recorded_as_before(self):
        # Unchanged by this work: the stored address cannot be corrected, so the true one lives in
        # the rejection's `validation_data`.
        prospect = _produce({"company_name": "Dallas Co", "city": "Austin", "state": "TX"})

        in_area, rejects = _run_loop(
            prospect,
            area=AUSTIN_AREA,
            verifier={
                "Dallas Co": {
                    "city": "Dallas",
                    "state": "TX",
                    "zip_code": "75201",
                    "confidence": "high",
                    "source_url": "https://example.org/dallas",
                }
            },
        )

        assert in_area == []
        assert len(rejects) == 1
        assert rejects[0]["prospect_id"] == prospect["id"]
        data = rejects[0]["validation_data"]
        assert data["validated"] is False
        assert data["disqualifiers_hit"] == ["outside the target geography"]
        assert data["verified_location"] == {
            "city": "Dallas",
            "state": "TX",
            "zip_code": "75201",
            "confidence": "high",
            "source_url": "https://example.org/dallas",
        }
        # The rejected prospect itself is not relocated and carries no marker.
        assert (prospect["city"], prospect["state"]) == ("Austin", "TX")
        assert "verified_location" not in prospect


class TestOnlyAnInAreaVerifiedLocationRelocates:
    """The keep/reject verdict sees the verified values layered OVER discovery's, so a lead can be
    kept on discovery's in-area ZIP although the verified city is elsewhere. Relocating such a lead
    to the verified city would store a place outside the scan's area, so the location that would be
    written is classified on its own and applied only when it is in area. Which leads are KEPT is
    unchanged.
    """

    def test_a_lead_kept_on_discovery_zip_is_not_relocated_out_of_area(self):
        prospect = _produce(
            {
                "company_name": "Acme Benefits",
                "city": "Austin",
                "state": "TX",
                "zip_code": "78701",
            }
        )

        in_area, rejects = _run_loop(
            prospect,
            area=AUSTIN_AREA,
            verifier={"Acme Benefits": {"city": "Dallas", "state": "TX"}},
        )

        # Kept: discovery's ZIP is in the area and the verdict falls back to it ...
        assert rejects == [] and len(in_area) == 1
        kept = in_area[0]
        # ... but not relocated to Dallas, which is outside it.
        assert (kept["city"], kept["state"], kept["zip_code"]) == ("Austin", "TX", "78701")
        internal = kept["_internal"]
        assert (internal["city"], internal["state"], internal["zip_code"]) == (
            "Austin",
            "TX",
            "78701",
        )
        assert "verified_location" not in kept

    def test_a_lead_kept_on_its_address_zip_is_not_relocated(self):
        # Discovery gave a street address with an in-area ZIP and no parsable city or state;
        # the classifier reads the ZIP out of the address. Houston is out of the area.
        prospect = _produce({"company_name": "Hill Co", "address": "100 Congress Ave 78701"})
        assert prospect["city"] is None and prospect["state"] is None, "precondition"

        in_area, rejects = _run_loop(
            prospect,
            area=AUSTIN_AREA,
            verifier={"Hill Co": {"city": "Houston", "state": "Texas"}},
        )

        assert rejects == [] and len(in_area) == 1
        kept = in_area[0]
        assert kept["city"] is None and kept["state"] is None
        assert kept["address"] == "100 Congress Ave 78701"
        assert not kept["_internal"].get("state")
        assert "verified_location" not in kept

    def test_a_location_normalised_to_a_bare_state_does_not_relocate(self):
        # The verifier's raw answer is truthy (so it counts as evidence and the lead is judged),
        # but the city carries a NUL and the ZIP is fullwidth: both normalise to nothing, leaving
        # a bare state. Relocating on that would wipe a good street and make the gateway null
        # the city and ZIP -- so the lead keeps discovery's location, street and no marker.
        prospect = _produce(
            {"company_name": "Acme Benefits", "address": "100 Congress Ave, Austin, TX 78701"}
        )
        street = prospect["address"]

        in_area, rejects = _run_loop(
            prospect,
            area=CHARLOTTE_AREA,
            verifier={
                "Acme Benefits": {
                    "city": "Char\x00lotte",
                    "state": "North Carolina",
                    "zip_code": "２８２０２",
                }
            },
        )

        # Kept (the city's letters still match the area), but untouched.
        assert rejects == [] and len(in_area) == 1
        kept = in_area[0]
        assert (kept["city"], kept["state"], kept["zip_code"]) == ("Austin", "TX", "78701")
        assert kept["address"] == street
        assert kept["_internal"]["location_address"] == street
        assert kept["_internal"]["state"] == "TX"
        assert "verified_location" not in kept

    def test_relocating_to_another_place_clears_the_street(self):
        # The street belonged to the old place. The gateway clears it on the same rule, so the
        # lead scored here and the row stored there must agree.
        prospect = _produce(
            {"company_name": "Acme Benefits", "address": "100 Congress Ave, Austin, TX 78701"}
        )
        assert prospect["address"] and prospect["_internal"]["location_address"], "precondition"

        in_area, _ = _run_loop(
            prospect,
            area=CHARLOTTE_AREA,
            verifier={
                "Acme Benefits": {"city": "Charlotte", "state": "North Carolina", "zip_code": "28202"}
            },
        )

        kept = in_area[0]
        assert kept["city"] == "Charlotte"
        assert kept["address"] is None
        assert kept["_internal"]["location_address"] == ""

    def test_relocating_within_the_same_place_keeps_the_street(self):
        # Same city and state, spelled differently: case and whitespace do not make a new place.
        prospect = _produce(
            {"company_name": "Acme Benefits", "address": "100 Congress Ave, Austin, TX 78701"}
        )
        street = prospect["address"]

        in_area, _ = _run_loop(
            prospect,
            area=AUSTIN_AREA,
            verifier={"Acme Benefits": {"city": "  AUSTIN ", "state": "texas", "zip_code": "78701"}},
        )

        kept = in_area[0]
        assert kept["verified_location"]["state"] == "TX", "it was relocated, to the same place"
        assert kept["address"] == street
        assert kept["_internal"]["location_address"] == street

    def test_the_round_log_counts_relocations_and_state_changes(self):
        lines: list[str] = []
        moved = _produce({"company_name": "Acme Benefits", "city": "Austin", "state": "TX"})
        _run_loop(
            moved,
            area=CHARLOTTE_AREA,
            verifier={"Acme Benefits": {"city": "Charlotte", "state": "NC", "zip_code": "28202"}},
            log=lines.append,
        )
        assert any("1 relocated (1 to a different state)" in line for line in lines), lines

        lines.clear()
        same_state = _produce({"company_name": "Birch Logistics", "city": "Dallas", "state": "TX"})
        _run_loop(
            same_state,
            area=AUSTIN_AREA,
            verifier={"Birch Logistics": {"city": "Austin", "state": "TX", "zip_code": "78701"}},
            log=lines.append,
        )
        assert any("1 relocated (0 to a different state)" in line for line in lines), lines


class TestScoredOnTheVerifiedAddress:
    """The gate reads `_internal['state']`, so the verified state must be what the SCORE saw."""

    @staticmethod
    def _wheelhouse_lead() -> dict:
        # Discovery answers one combined field that no alias recognises as a location, so the
        # producer yields a lead with NO state -- the real shape this change exists for.
        return _produce(
            {"company_name": "Wheelhouse Benefits", "headquarters_location": "Austin, TX"}
        )

    @staticmethod
    def _target_market(item: dict) -> bool:
        return item["score_factors"]["gated"]["gates"]["target_market"]

    def test_the_target_market_gate_reads_the_verified_state(self):
        lead = self._wheelhouse_lead()
        assert not lead["_internal"].get("state"), "precondition: discovery yielded no state"

        in_area, rejects = _run_loop(
            lead,
            area=CHARLOTTE_AREA,
            verifier={
                "Wheelhouse Benefits": {
                    "city": "Charlotte",
                    "state": "North Carolina",
                    "zip_code": "28202",
                }
            },
        )
        assert rejects == [] and len(in_area) == 1

        scored = _score(in_area)
        assert len(scored) == 1
        assert self._target_market(scored[0]) is True

        # The control: the same lead WITHOUT verification has no state, so the gate fails it.
        control = _score([self._wheelhouse_lead()])
        assert len(control) == 1
        assert self._target_market(control[0]) is False


class TestLegacyScoringFollowsTheVerifiedAddress:
    """Intended, and pinned so it is a decision rather than a side effect: a legacy (non-gated)
    skill's `region_bonus` reads city/state from `_internal` too, so the verified location moves
    its score exactly as it moves a gated skill's gate.
    """

    def test_region_bonus_reads_the_verified_location(self):
        legacy = {"region_bonus": {"max": 10, "regions": {"nc": ["charlotte"]}}}

        def region_bonus(prospect: dict) -> int:
            return _score([prospect], legacy)[0]["score_factors"]["region_bonus"]

        raw = {"company_name": "Acme Benefits", "city": "Austin", "state": "TX"}
        # The control: left on discovery's Austin, TX, outside the NC region.
        assert region_bonus(_produce(raw)) == 0

        verified = _produce(raw)
        in_area, _ = _run_loop(
            verified,
            area=CHARLOTTE_AREA,
            verifier={
                "Acme Benefits": {"city": "Charlotte", "state": "North Carolina", "zip_code": "28202"}
            },
        )
        assert len(in_area) == 1
        assert region_bonus(in_area[0]) == 10


class TestAttachVerifiedLocations:
    """The scored item is a different object from the prospect, keyed `prospect_id` where the
    prospect is keyed `id` -- the same mismatch that once dropped every score explanation.
    """

    def test_copies_the_marker_onto_scored_items_by_prospect_id(self):
        from aeo.runner import attach_verified_locations

        verified = _produce({"company_name": "Acme Benefits", "city": "Austin", "state": "TX"})
        plain = _produce({"company_name": "Birch Logistics", "city": "Austin", "state": "TX"})
        marker = {"city": "Charlotte", "state": "NC", "zip_code": "28202"}
        verified["verified_location"] = dict(marker)

        scored = _score([verified, plain])
        assert all(item.get("prospect_id") and "id" not in item for item in scored)

        attached = attach_verified_locations(scored, [verified, plain])

        assert attached == 1
        by_id = {item["prospect_id"]: item for item in scored}
        assert by_id[verified["id"]]["verified_location"] == marker
        assert "verified_location" not in by_id[plain["id"]]


class TestTheWireCarriesOnlyVerifiedLocations:
    """The engine's scored item ALWAYS has `city`/`state` keys -- discovery's, raw, unnormalised.
    Sending those would make "verified" mean nothing, and would push values like "North Carolina"
    at a gateway that ignores anything that is not a code.
    """

    @staticmethod
    def _scored_item(raw: dict) -> dict:
        return _score([_produce(raw)])[0]

    def test_a_verified_lead_sends_its_location(self):
        item = self._scored_item({"company_name": "Acme Benefits", "city": "Austin", "state": "TX"})
        item["verified_location"] = {"city": "Charlotte", "state": "NC", "zip_code": "28202"}

        wire = _wire_item(item)

        assert (wire["city"], wire["state"], wire["zip_code"]) == ("Charlotte", "NC", "28202")
        # The marker is internal: the gateway whitelists, and an unknown key 400s the callback.
        assert "verified_location" not in wire

        # A part the verifier could not supply is omitted, never sent as null or "".
        item["verified_location"] = {"city": "Charlotte", "state": "NC", "zip_code": None}
        wire = _wire_item(item)
        assert (wire["city"], wire["state"]) == ("Charlotte", "NC")
        assert "zip_code" not in wire

    def test_a_relocated_lead_reaches_the_wire_through_the_runner_step(self):
        # The whole chain, each link the real one: producer -> geography loop -> scorer ->
        # the runner's attach step -> the event mapper. Each link is tested alone elsewhere;
        # this is the one that fails if they stop fitting together (e.g. `id` vs `prospect_id`).
        from aeo.event_mapping import map_event
        from aeo.runner import attach_verified_locations

        moved = _produce({"company_name": "Acme Benefits", "city": "Austin", "state": "TX"})
        unverified = _produce({"company_name": "Ghost Co", "city": "Austin", "state": "TX"})

        in_area, rejects = discover_in_area(
            tool_context={"sources": {"s": {}}},
            area=CHARLOTTE_AREA,
            target_count=2,
            discover=lambda ctx: [moved, unverified],
            provider=_verifier(
                {"Acme Benefits": {"city": "Charlotte", "state": "North Carolina", "zip_code": "28202"}}
            ),
            provider_config={},
            parse_json_array=_parse,
            max_rounds=1,
        )
        assert rejects == [] and len(in_area) == 2

        scored = _score(in_area)
        assert attach_verified_locations(scored, in_area) == 1

        events = map_event({"type": "scored", "items": scored})
        assert len(events) == 1 and events[0][0] == "scored"
        by_id = {item["prospect_id"]: item for item in events[0][1]["data"]}

        relocated = by_id[moved["id"]]
        assert (relocated["city"], relocated["state"], relocated["zip_code"]) == (
            "Charlotte",
            "NC",
            "28202",
        )
        plain = by_id[unverified["id"]]
        for key in ("city", "state", "zip_code", "verified_location"):
            assert key not in plain, key

    def test_an_unverified_lead_sends_no_location(self):
        item = self._scored_item({"company_name": "Acme Benefits", "city": "Austin", "state": "TX"})
        # Precondition: the engine item really does carry discovery's location.
        assert item["city"] == "Austin" and item["state"] == "TX"
        assert "verified_location" not in item

        wire = _wire_item(item)

        for key in ("city", "state", "zip_code", "verified_location"):
            assert key not in wire, key


class TestTheBlindGateWarning:
    """A gated skill whose target-market gate cannot be evaluated fails every lead CLOSED and
    reports normally -- the run looks healthy and yields nothing. It has to say so.
    """

    @staticmethod
    def _scored(states: list[str]) -> list[dict]:
        prospects = []
        for i, state in enumerate(states):
            raw = {"company_name": f"Firm {i}", "city": "Somewhere", "state": state}
            prospects.append(_produce(raw))
        return _score(prospects)

    def test_warns_when_most_leads_have_no_state(self):
        from aeo.runner import blind_gate_warning

        scoring = _gated_scoring(["NC"])
        # Two of three have no state, so the gate could not be evaluated for most of the run.
        scored = self._scored(["", "", "NC"])
        warning = blind_gate_warning(scored, scoring, "run-123")
        assert warning is not None
        assert "run-123" in warning
        assert "2 of 3 scored lead(s) have no state" in warning
        assert "no lead passed the gate" not in warning, "one lead did pass"

        # The other way to be blind: states exist but not one lead passes the gate.
        scored = self._scored(["TX", "TX", "TX"])
        assert all(not i["score_factors"]["gated"]["gates"]["target_market"] for i in scored)
        warning = blind_gate_warning(scored, scoring, "run-456")
        assert warning is not None
        assert "run-456" in warning
        assert "no lead passed the gate (3 scored)" in warning
        assert "have no state" not in warning, "every lead had a state"

    def test_is_quiet_when_the_gate_can_be_evaluated(self):
        from aeo.runner import blind_gate_warning

        scoring = _gated_scoring(["NC"])
        # Every lead has a state and all pass.
        assert blind_gate_warning(self._scored(["NC", "NC", "NC"]), scoring, "run-1") is None
        # A minority without a state, and at least one lead passes: healthy enough to say nothing.
        assert blind_gate_warning(self._scored(["", "NC", "NC"]), scoring, "run-1") is None

        # Legacy (non-gated) config: there is no gate to be blind, whatever the leads look like.
        legacy = {"score_cap": 100}
        assert blind_gate_warning(self._scored(["", "", ""]), legacy, "run-1") is None
        assert blind_gate_warning(self._scored(["", "", ""]), {"model": "legacy"}, "run-1") is None
        assert blind_gate_warning(self._scored(["", "", ""]), None, "run-1") is None

        # Nothing scored: nothing to say.
        assert blind_gate_warning([], scoring, "run-1") is None

    def test_exactly_half_without_a_state_is_not_blind(self):
        from aeo.runner import blind_gate_warning

        scoring = _gated_scoring(["NC"])
        # "More than half" is strict: 2 of 4 empty, with leads passing, says nothing ...
        assert blind_gate_warning(self._scored(["", "", "NC", "NC"]), scoring, "run-1") is None
        # ... and 3 of 4 tips it, even though one lead still passes.
        warning = blind_gate_warning(self._scored(["", "", "", "NC"]), scoring, "run-1")
        assert warning is not None and "3 of 4 scored lead(s) have no state" in warning

    def test_warns_when_a_gated_config_has_no_usable_target_market_gate(self):
        # Every lead fails closed there (`in_target_market` refuses an empty allow-list), which
        # is exactly the silent-yield-nothing run the warning exists for -- so it is NOT quiet.
        from aeo.runner import blind_gate_warning

        scoring = _gated_scoring(["NC"])
        scored = self._scored(["NC", "NC", "NC"])
        no_block = {**scoring, "gate": {"buying_window": scoring["gate"]["buying_window"]}}
        empty_states = _gated_scoring([])

        for cfg in (no_block, empty_states):
            warning = blind_gate_warning(scored, cfg, "run-9")
            assert warning is not None
            assert "run-9" in warning
            assert "no target-market gate configured" in warning

    def test_never_raises_on_malformed_scored_items(self):
        # It runs on the way to the wire; a malformed item must not cost a run its results.
        from aeo.runner import blind_gate_warning

        scoring = _gated_scoring(["NC"])
        malformed = [
            {"prospect_id": "a"},
            {"prospect_id": "b", "state": "NC", "score_factors": None},
            {"prospect_id": "c", "state": "NC", "score_factors": {"gated": None}},
            {"prospect_id": "d", "state": "NC", "score_factors": {"gated": "x"}},
            {"prospect_id": "e", "state": "NC", "score_factors": {"gated": {"gates": None}}},
            {"prospect_id": "f", "state": "NC", "score_factors": {"gated": {"gates": []}}},
            {"prospect_id": "g", "state": None, "fields": "not a dict"},
            "not even a dict",
        ]
        warning = blind_gate_warning(malformed, scoring, "run-7")
        assert warning is not None and "run-7" in warning
        assert "no lead passed the gate" in warning

        # Malformed config shapes degrade the same way.
        for cfg in ({"model": "gated"}, {"model": "gated", "gate": "x"}, {"model": "gated", "gate": {"target_market": []}}):
            assert "no target-market gate configured" in blind_gate_warning(malformed, cfg, "run-7")
