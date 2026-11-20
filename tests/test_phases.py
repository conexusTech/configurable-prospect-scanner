"""Tests for the validation and contacts phases. Ours.

These pin judgement calls, not just plumbing — each one is a place where the
convenient behaviour silently loses or invents data.
"""

from __future__ import annotations

import json
from pathlib import Path

import runner_harness as harness
from aeo.phases.contacts import find_contacts, merge_into_scored
from aeo.phases.validation import surviving_ids, validate_prospects


def _parse(text: str) -> list[dict]:
    """Stand-in for the engine's parse_json_array."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [p for p in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(p, dict)]


def _provider(response: str):
    def call(prompt, **kwargs):
        call.prompts.append(prompt)
        return response
    call.prompts = []
    return call


def _boom(prompt, **kwargs):
    raise RuntimeError("provider exploded")


PROSPECTS = [
    {"id": "p1", "company_name": "First Church", "city": "Austin", "state": "TX"},
    {"id": "p2", "company_name": "Second Church", "city": "Dallas", "state": "TX"},
]
VALIDATION_CFG = {
    "in_market_signals": ["capital campaign announced"],
    "disqualifiers": ["under 200 seats"],
}


class TestValidation:
    def test_records_a_verdict_per_prospect(self):
        provider = _provider(
            json.dumps([{"validated": True, "signals_found": ["campaign"], "reasoning": "r"}])
        )
        out = validate_prospects(
            PROSPECTS, validation_config=VALIDATION_CFG, provider=provider,
            provider_config={}, parse_json_array=_parse,
        )
        assert [o["prospect_id"] for o in out] == ["p1", "p2"]
        assert out[0]["validation_data"]["validated"] is True

    def test_an_unparseable_response_is_None_NOT_False(self):
        # Absence of evidence is not disqualification. Recording unjudged as invalid
        # would shrink every result set with nothing looking wrong.
        out = validate_prospects(
            PROSPECTS, validation_config=VALIDATION_CFG, provider=_provider("not json"),
            provider_config={}, parse_json_array=_parse,
        )
        assert out[0]["validation_data"]["validated"] is None

    def test_a_provider_failure_fails_one_prospect_not_the_phase(self):
        out = validate_prospects(
            PROSPECTS, validation_config=VALIDATION_CFG, provider=_boom,
            provider_config={}, parse_json_array=_parse,
        )
        assert len(out) == 2
        assert all(o["validation_data"]["validated"] is None for o in out)

    def test_puts_the_orgs_resolved_signals_in_the_prompt(self):
        # If bindings were not resolved upstream, the prompt would contain a dict
        # literal and the model would judge against nothing.
        provider = _provider(json.dumps([{"validated": True}]))
        validate_prospects(
            PROSPECTS, validation_config=VALIDATION_CFG, provider=provider,
            provider_config={}, parse_json_array=_parse,
        )
        assert "capital campaign announced" in provider.prompts[0]
        assert "under 200 seats" in provider.prompts[0]

    def test_skips_prospects_with_no_id(self):
        out = validate_prospects(
            [{"company_name": "No Id"}], validation_config=VALIDATION_CFG,
            provider=_provider("[]"), provider_config={}, parse_json_array=_parse,
        )
        assert out == []


class TestSurvivingIds:
    def test_drops_only_explicit_failures(self):
        keep = surviving_ids([
            {"prospect_id": "a", "validation_data": {"validated": True}},
            {"prospect_id": "b", "validation_data": {"validated": False}},
        ])
        assert keep == {"a"}

    def test_KEEPS_unjudged_prospects(self):
        # A transient model failure must not quietly delete prospects from the run.
        keep = surviving_ids([{"prospect_id": "c", "validation_data": {"validated": None}}])
        assert keep == {"c"}


class TestContacts:
    def test_fills_the_column_backed_fields(self):
        provider = _provider(json.dumps([{
            "contact_name": "Jane Doe", "contact_title": "Facilities Director",
            "contact_email": "jane@example.org", "contact_phone": "512-555-0100",
            "contact_linkedin": "https://linkedin.com/in/janedoe",
        }]))
        out = find_contacts(
            PROSPECTS[:1], contacts_config={"titles": ["Facilities Director"]},
            product_description="AV integration", provider=provider,
            provider_config={}, parse_json_array=_parse,
        )
        patch = out["p1"]
        assert patch["contact_name"] == "Jane Doe"
        assert patch["contact_email"] == "jane@example.org"

    def test_rejects_a_pattern_shaped_non_email_rather_than_promoting_it(self):
        # A guessed address bounces, and a bounced first touch is worse than none.
        provider = _provider(json.dumps([{"contact_name": "Jane", "contact_email": "first.last at example"}]))
        out = find_contacts(
            PROSPECTS[:1], contacts_config={}, product_description="x",
            provider=provider, provider_config={}, parse_json_array=_parse,
        )
        patch = out["p1"]
        assert "contact_email" not in patch
        assert patch["contacts_data"]["rejected_email"] == "first.last at example"

    def test_treats_placeholder_words_as_absent(self):
        # "Unknown"/"N/A" read as data downstream and defeat a needs-enrichment filter.
        provider = _provider(json.dumps([{"contact_name": "Unknown", "contact_title": "N/A"}]))
        out = find_contacts(
            PROSPECTS[:1], contacts_config={}, product_description="x",
            provider=provider, provider_config={}, parse_json_array=_parse,
        )
        assert out["p1"] == {}

    def test_keeps_extras_as_evidence_in_contacts_data(self):
        provider = _provider(json.dumps([{
            "contact_name": "Jane", "source_url": "https://example.org/staff", "confidence": "high",
        }]))
        out = find_contacts(
            PROSPECTS[:1], contacts_config={}, product_description="x",
            provider=provider, provider_config={}, parse_json_array=_parse,
        )
        assert out["p1"]["contacts_data"] == {
            "source_url": "https://example.org/staff", "confidence": "high",
        }

    def test_emits_an_entry_even_when_nobody_was_found(self):
        # "Searched and found nobody" differs from "never searched"; only the
        # former should stop a retry.
        out = find_contacts(
            PROSPECTS, contacts_config={}, product_description="x",
            provider=_provider("[]"), provider_config={}, parse_json_array=_parse,
        )
        assert set(out) == {"p1", "p2"}

    def test_a_provider_failure_fails_one_prospect_not_the_phase(self):
        out = find_contacts(
            PROSPECTS, contacts_config={}, product_description="x",
            provider=_boom, provider_config={}, parse_json_array=_parse,
        )
        assert set(out) == {"p1", "p2"}


class TestMergeIntoScored:
    def test_overwrites_the_engines_incidental_contact_name(self):
        scored = [{"prospect_id": "p1", "contact_name": "scraped guess"}]
        merged = merge_into_scored(scored, {"p1": {"contact_name": "Jane Doe"}})
        assert merged[0]["contact_name"] == "Jane Doe"

    def test_an_empty_patch_never_erases_a_name_we_already_had(self):
        scored = [{"prospect_id": "p1", "contact_name": "scraped guess"}]
        merged = merge_into_scored(scored, {"p1": {}})
        assert merged[0]["contact_name"] == "scraped guess"

    def test_merges_rather_than_replaces_contacts_data(self):
        scored = [{"prospect_id": "p1", "contacts_data": {"existing": 1}}]
        merged = merge_into_scored(scored, {"p1": {"contacts_data": {"added": 2}}})
        assert merged[0]["contacts_data"] == {"existing": 1, "added": 2}

    def test_ignores_a_patch_for_an_unknown_prospect(self):
        scored = [{"prospect_id": "p1"}]
        assert merge_into_scored(scored, {"ghost": {"contact_name": "X"}}) == scored


class TestContactsAreBatched:
    """§4 of the bundling ruling: contact search batches at 6, one call per batch.

    🔴 Batching's real risk is not cost, it is CROSS-CONTAMINATION — the model stops
    treating entities as distinct records and gives one company another's contact. A
    cheaper phase that attributes the wrong person to a prospect is worse than the
    expensive one, so the per-entity attribution test below matters more than the
    call-count test beside it.
    """

    @staticmethod
    def _many(n: int) -> list[dict]:
        return [
            {"id": f"p{i}", "company_name": f"Co {i}", "city": "Austin", "state": "TX"}
            for i in range(n)
        ]

    @staticmethod
    def _batched_provider():
        """Answers per entity, reading the numbered list back out of the prompt."""

        def call(prompt, **kwargs):
            call.prompts.append(prompt)
            names = []
            for line in prompt.splitlines():
                line = line.strip()
                if line and line[0].isdigit() and "name: " in line:
                    names.append(line.split("name: ", 1)[1].split(";")[0].strip())
            return json.dumps(
                [
                    {
                        "n": i,
                        "company_name": n,
                        "contact_name": f"Contact for {n}",
                        "contact_email": f"{n.replace(' ', '').lower()}@example.org",
                    }
                    for i, n in enumerate(names, 1)
                ]
            )

        call.prompts = []
        return call

    def test_one_call_per_batch_of_six(self):
        provider = self._batched_provider()
        find_contacts(
            self._many(13), contacts_config={}, product_description="x",
            provider=provider, provider_config={}, parse_json_array=_parse,
        )
        # ceil(13 / 6) = 3. Unbatched this was 13.
        assert len(provider.prompts) == 3

    def test_each_prospect_gets_ITS_OWN_contact(self):
        provider = self._batched_provider()
        out = find_contacts(
            self._many(8), contacts_config={}, product_description="x",
            provider=provider, provider_config={}, parse_json_array=_parse,
        )
        assert len(out) == 8
        for i in range(8):
            assert out[f"p{i}"]["contact_name"] == f"Contact for Co {i}", (
                "a prospect received another prospect's contact — the failure batching "
                "makes possible"
            )

    def test_a_short_response_is_reported_not_silently_empty(self):
        """§5.5 — a target with no object searched and got nothing BACK."""

        def call(prompt, **kwargs):
            call.prompts.append(prompt)
            return json.dumps([{"n": 1, "company_name": "Co 0",
                                "contact_name": "Only One"}])

        call.prompts = []
        events: list[dict] = []
        out = find_contacts(
            self._many(5), contacts_config={}, product_description="x",
            provider=call, provider_config={}, parse_json_array=_parse,
            emit=events.append,
        )
        miss = next(e for e in events if e["type"] == "contacts_unmatched")
        assert miss["prospects"] == 4 and miss["of"] == 5
        # The one that came back is still used; the rest are empty patches, not absent.
        assert out["p0"]["contact_name"] == "Only One"
        assert set(out) == {f"p{i}" for i in range(5)}

    def test_the_numbered_list_and_output_rules_reach_the_prompt(self):
        provider = self._batched_provider()
        find_contacts(
            self._many(3), contacts_config={}, product_description="x",
            provider=provider, provider_config={}, parse_json_array=_parse,
        )
        prompt = provider.prompts[0]
        assert "1. name: Co 0" in prompt and "3. name: Co 2" in prompt
        assert "exact official name and NOTHING else" in prompt
        assert "IN THE SAME ORDER" in prompt
        # The phase's own non-negotiable survives batching.
        assert "Do not guess or construct an email address" in prompt


class TestValidationBatchingIsDormant:
    """Batching exists here, defaults OFF, and waits on an accuracy A/B.

    🔴 §4 would permit batch 8 for this single-signal phase — 111 grounded calls on the
    reference run, 20% of it. Not taken, because a false verdict here REMOVES the lead:
    a degraded batch returns fewer PROSPECTS, which is indistinguishable from a thin
    market. §6's bar for a signal whose wrong answer zeroes a lead is an accuracy A/B
    against a known sample, judged on correctness never row count.
    """

    @staticmethod
    def _many(n: int) -> list[dict]:
        return [
            {"id": f"p{i}", "company_name": f"Co {i}", "city": "Austin", "state": "TX"}
            for i in range(n)
        ]

    def test_the_default_is_ONE_and_the_control_prompt_is_untouched(self):
        # An A/B whose control arm was silently rewritten measures nothing. At the
        # default the phase must send the ORIGINAL single-prospect prompt, not the
        # batched one with a count of 1.
        from aeo.phases.validation import DEFAULT_VALIDATION_BATCH

        assert DEFAULT_VALIDATION_BATCH == 1
        provider = _provider(json.dumps([{"validated": True}]))
        validate_prospects(
            self._many(3), validation_config=VALIDATION_CFG, provider=provider,
            provider_config={}, parse_json_array=_parse,
        )
        assert len(provider.prompts) == 3, "default must stay one call per prospect"
        assert "ORGANIZATIONS —" not in provider.prompts[0]
        assert "PROSPECT" in provider.prompts[0]

    def test_at_batch_eight_it_makes_one_call_per_batch(self):
        provider = _provider(json.dumps([
            {"n": i, "company_name": f"Co {i-1}", "validated": True} for i in range(1, 9)
        ]))
        validate_prospects(
            self._many(8), validation_config=VALIDATION_CFG, provider=provider,
            provider_config={}, parse_json_array=_parse, batch_size=8,
        )
        assert len(provider.prompts) == 1

    def test_each_prospect_keeps_ITS_OWN_verdict(self):
        # The failure batching makes possible, and the one that matters most here:
        # a disqualifier belonging to one prospect carrying to its neighbours.
        def call(prompt, **kw):
            call.prompts.append(prompt)
            return json.dumps([
                {"n": 1, "company_name": "Co 0", "validated": True,
                 "signals_found": ["expanding"]},
                {"n": 2, "company_name": "Co 1", "validated": False,
                 "disqualifiers_hit": ["too small"]},
                {"n": 3, "company_name": "Co 2", "validated": True,
                 "signals_found": ["hiring"]},
            ])

        call.prompts = []
        out = validate_prospects(
            self._many(3), validation_config=VALIDATION_CFG, provider=call,
            provider_config={}, parse_json_array=_parse, batch_size=8,
        )
        by_id = {o["prospect_id"]: o["validation_data"] for o in out}
        assert by_id["p0"]["validated"] is True
        assert by_id["p1"]["validated"] is False
        assert by_id["p1"]["disqualifiers_hit"] == ["too small"]
        assert by_id["p2"]["validated"] is True
        # The disqualifier did NOT leak to its neighbours.
        assert by_id["p0"]["disqualifiers_hit"] == []
        assert by_id["p2"]["disqualifiers_hit"] == []

    def test_an_unmatched_prospect_is_NULL_not_FALSE(self):
        """🔴 The safety property that makes batching this phase survivable at all.

        A prospect the model omitted was NOT judged. Recording it as `False` would
        remove a lead nobody assessed, and the run would look like a thin market.
        """
        def call(prompt, **kw):
            call.prompts.append(prompt)
            # Answers for the first only; the other three are dropped.
            return json.dumps([{"n": 1, "company_name": "Co 0", "validated": True}])

        call.prompts = []
        out = validate_prospects(
            self._many(4), validation_config=VALIDATION_CFG, provider=call,
            provider_config={}, parse_json_array=_parse, batch_size=8,
        )
        by_id = {o["prospect_id"]: o["validation_data"] for o in out}
        assert by_id["p0"]["validated"] is True
        for missing in ("p1", "p2", "p3"):
            assert by_id[missing]["validated"] is None, (
                "an unjudged prospect must be null, never false — false would delete a "
                "lead nobody assessed"
            )

    def test_the_independence_instruction_reaches_the_batched_prompt(self):
        provider = _provider(json.dumps([
            {"n": 1, "company_name": "Co 0", "validated": True}
        ]))
        validate_prospects(
            self._many(1), validation_config=VALIDATION_CFG, provider=provider,
            provider_config={}, parse_json_array=_parse, batch_size=8,
        )
        prompt = provider.prompts[0]
        assert "Judge every one INDEPENDENTLY" in prompt
        assert "a disqualifier that applies to one does not carry to the rest" in prompt
        # The per-prospect evidence standard survives batching — the §6 concern.
        assert "do not\nguess a value in order to judge it" in prompt


# ── `sources-read-gateway-records`: whole runs, offline ──────────────────────────
#
# Driven through `aeo.runner.main()` by `runner_harness`, with the gateway, the queue
# and the model all stubbed. These pin what a run SPENDS and SENDS, which no phase-level
# test can see: a feed skill pays for no search, and a searched skill is untouched.


#: Every phase that buys a grounded search to FIND or PLACE a prospect.
_SEARCH_PHASES = ("discovery", "geography", "validation", "enrichment")


class TestAFeedRunPaysForNoSearch:
    """`no-search-spend`: a feed run makes no discovery, geography or validation call."""

    def test_only_contacts_and_judgment_reach_the_model_beyond_the_area(self, monkeypatch):
        run = harness.run_main(monkeypatch, harness.feed_context(), records=harness.feed_answer())
        assert run.code == 0, run.error_message()
        calls = run.provider.calls
        for phase in _SEARCH_PHASES:
            assert run.provider.count(phase) == 0, (phase, calls)
        assert run.provider.count("contacts") >= 1
        assert run.provider.count("judgment") >= 1
        # Zip discovery is the territory's widening (one call per market), not a search
        # for prospects; nothing else is paid for.
        assert set(calls) == {"zip_discovery", "judgment", "contacts"}, calls
        assert run.provider.count("zip_discovery") == 1

    def test_the_gateway_is_read_once_for_the_feed_source(self, monkeypatch):
        run = harness.run_main(monkeypatch, harness.feed_context(), records=harness.feed_answer())
        assert run.record_requests == ["inspections"]

    def test_the_run_reports_what_it_read(self, monkeypatch):
        run = harness.run_main(monkeypatch, harness.feed_context(), records=harness.feed_answer())
        [completed] = run.posted_of("completed")
        summary = completed["summary"]
        assert summary["total_prospects"] == 6
        assert summary["total_validated"] == 0
        assert summary["total_scored"] == 6
        assert not run.posted_of("validations")

    def test_the_finding_rides_on_the_prospect(self, monkeypatch):
        run = harness.run_main(monkeypatch, harness.feed_context(), records=harness.feed_answer())
        bella = next(i for i in run.items_of("prospects") if i["company_name"] == "Bella's Bistro, 100 E Cary St")
        row = next(r for r in harness.feed_answer()["items"]
                   if r["establishment_name"] == "Bella's Bistro, 100 E Cary St")
        assert bella["discovery_data"]["by_source"]["inspections"] == row
        assert (bella["city"], bella["state"], bella["zip_code"]) == ("Richmond", "VA", "23220")
        assert bella["phase"] == "discover"


class TestContactsAreFoundForFeedProspects:
    """`contacts-found-for-feed-prospects`: each gets up to three people, as any prospect."""

    def test_every_prospect_gets_three_people_and_the_first_fills_the_columns(self, monkeypatch):
        run = harness.run_main(monkeypatch, harness.feed_context(), records=harness.feed_answer())
        assert run.code == 0, run.error_message()
        scored = run.items_of("scored")
        prospect_names = {i["id"]: i["company_name"] for i in run.items_of("prospects")}
        assert len(scored) == 6
        for item in scored:
            name = prospect_names[item["prospect_id"]]
            assert len(item["contacts"]) == 3, name
            first = item["contacts"][0]
            assert first["name"] == f"Avery {name}"
            assert item["contact_name"] == first["name"]
            assert item["contact_title"] == first["title"] == "Owner"
            assert item["contact_email"] == first["email"]

    def test_six_prospects_are_one_contact_call(self, monkeypatch):
        run = harness.run_main(monkeypatch, harness.feed_context(), records=harness.feed_answer())
        assert run.provider.count("contacts") == 1

    def test_the_contacts_ceiling_still_binds(self, monkeypatch):
        ctx = harness.feed_context(config_contacts={"max_prospects": 2, "titles": ["Owner"]})
        run = harness.run_main(monkeypatch, ctx, records=harness.feed_answer())
        with_people = [i for i in run.items_of("scored") if i.get("contacts")]
        assert len(with_people) == 2


class TestASearchedSkillIsUnchanged:
    """`searched-skills-unchanged`: a skill with no feed sends what it sent before feeds.

    The expected callbacks were recorded from the scanner at 95d1eea, before this change,
    by running the same fixture through the same harness.
    """

    GOLDEN = Path(__file__).parent / "fixtures" / "searched-run" / "wire-callbacks.json"

    def test_the_same_callbacks_reach_the_gateway(self, monkeypatch):
        golden = json.loads(self.GOLDEN.read_text())
        run = harness.run_main(monkeypatch, harness.SEARCHED_CONTEXT)
        assert run.code == golden["exit_code"] == 0
        assert harness.wire_snapshot(run) == golden["callbacks"]

    def test_the_same_model_calls_are_made(self, monkeypatch):
        golden = json.loads(self.GOLDEN.read_text())
        run = harness.run_main(monkeypatch, harness.SEARCHED_CONTEXT)
        assert dict(sorted(run.provider.calls.items())) == golden["provider_calls"]
        # The same prompts, too: a search sent with its market unbound costs the same
        # number of calls and finds different firms.
        assert harness.prompt_digests(run) == golden["provider_prompts"]

    def test_a_searched_run_never_reads_the_gateway_feed(self, monkeypatch):
        # The harness fails the run if it asks; the empty list says it never did.
        run = harness.run_main(monkeypatch, harness.SEARCHED_CONTEXT)
        assert run.record_requests == []
