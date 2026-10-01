"""The score explanation — an AI pass AFTER scoring, and the check on what it writes.

**Why this is a model call and not a template.** The PO ruled it: *"we do not need to
explain anything, we just need to derive it on the information available… explain why it
is a high scored prospect backed up with the best info available. Do not over explain."*
The existing `ai_analysis` is already model-written; it reads as nonsense because it runs
BEFORE the score exists and is never handed it, and its prompt says "Stay stage reasoning".

🔴 **The validator is the load-bearing part.** A fabricated detail beside a score is worse
than dull prose: it is false in the one way a customer can check, and detecting it once
costs the number its credibility permanently.
"""
import pytest

from aeo.phases.ai_judgment import _ADJUSTMENT_FIELD, _FIT_SECTION
from aeo.event_mapping import map_event
from aeo.phases.score_explanation import (
    MAX_CHARS,
    attach_explanations,
    build_facts,
    explain_scores,
    explanation_key,
    validate_explanation,
)

BREAKDOWN = {
    "total": 93,
    "lane": "qualified",
    "gates": {"target_market": True, "buying_window": True},
    "bands": {"signal_strength": 8, "company_size": 3,
              "confirmed_contact": 4, "signal_recency": 3},
    "selected_signal": {
        "signal_type": "rfp activity",
        "signal_class": "rfp_active",
        "signal_date": "2026-08-06",
        "signal_description": "Announced a benefits evaluation for 250 employees.",
    },
}
LEAD = {
    "id": "p1", "company_name": "Acme", "state": "NC", "employee_count": "250",
    "contact_name": "A", "contact_email": "a@b.c", "contact_phone": "555",
    "score_factors": {"gated": BREAKDOWN},
}


class TestFactsAreTheWholeInput:
    def test_both_gate_verdicts_are_stated_either_way(self):
        facts = "\n".join(build_facts(BREAKDOWN, LEAD))
        assert "target market: yes" in facts
        assert "active buying window: yes" in facts

    def test_a_failed_gate_says_WHICH_one_failed(self):
        # "wrong time" and "wrong place" are different sales actions, and today's single
        # number cannot express either.
        bd = {**BREAKDOWN, "gates": {"target_market": True, "buying_window": False}}
        facts = "\n".join(build_facts(bd, LEAD))
        assert "NOT a qualified lead: no recent buying signal" in facts

    def test_the_scanner_finding_is_quoted_verbatim(self):
        # 🔑 Load-bearing for the PO's approval: the quoted sentence is where the
        # persuasion lives. Removing it reopens the decision.
        facts = "\n".join(build_facts(BREAKDOWN, LEAD))
        assert '"Announced a benefits evaluation for 250 employees."' in facts

    def test_no_record_field_beyond_the_named_set_leaks_in(self):
        # What keeps invention unrepresentable rather than merely discouraged.
        noisy = {**LEAD, "revenue": "$40M", "industry": "Biotech", "website": "x.com"}
        facts = "\n".join(build_facts(BREAKDOWN, noisy))
        for leaked in ("40M", "Biotech", "x.com"):
            assert leaked not in facts


class TestTheValidator:
    def test_accepts_a_paragraph_built_only_from_its_inputs(self):
        text = ("A North Carolina employer with an active RFP dated 2026-08-06 and a "
                "full contact on file. Scores 93.")
        assert validate_explanation(text, BREAKDOWN, LEAD) is None

    def test_rejects_an_invented_number(self):
        # The failure that matters: reads beautifully, false in the one way a customer
        # can check.
        text = "A North Carolina employer with 1,200 staff and an active RFP."
        assert validate_explanation(text, BREAKDOWN, LEAD) == (
            "states a number not in its inputs: 1,200"
        )

    def test_accepts_numbers_that_appear_in_the_quoted_finding(self):
        # "250 employees" is in the scanner's own sentence, so repeating it is grounded.
        text = "Announced a benefits evaluation for 250 employees; scores 93."
        assert validate_explanation(text, BREAKDOWN, LEAD) is None

    @pytest.mark.parametrize("bad", ["", "   ", None])
    def test_rejects_empty(self, bad):
        assert validate_explanation(bad, BREAKDOWN, LEAD) == "empty"

    def test_rejects_markup_and_bullets(self):
        assert validate_explanation("- one\n- two", BREAKDOWN, LEAD) is not None
        assert validate_explanation("**bold** claim", BREAKDOWN, LEAD) is not None

    def test_rejects_something_far_too_long(self):
        assert validate_explanation("word " * 400, BREAKDOWN, LEAD) is not None

    def test_rejects_a_control_character(self):
        # A NUL makes Postgres reject the whole scored batch's UPDATE, not one row.
        text = "Worth a call.\x00 Good fit."
        assert validate_explanation(text, BREAKDOWN, LEAD) == "contains a control character"

    def test_a_single_newline_is_not_a_control_character(self):
        assert validate_explanation("Worth a call.\nGood fit.", BREAKDOWN, LEAD) is None

    @pytest.mark.parametrize(
        "text",
        [
            "See http://example.org/offer for the detail.",
            "Their site acme-leads.net lists the project.",
            "Reach name@example.com about it.",
            "Visit www.acme for the detail.",
        ],
    )
    def test_rejects_a_link_or_contact_detail(self, text):
        assert validate_explanation(text, BREAKDOWN, LEAD) == (
            "contains a link or contact detail"
        )

    def test_an_ordinary_sentence_with_abbreviations_is_not_a_link(self):
        text = "Acme Inc. is a St. Louis employer with an RFP dated 2026-08-06."
        assert validate_explanation(text, BREAKDOWN, LEAD) is None


class TestThePass:
    def test_a_rejected_explanation_is_ABSENT_not_a_fallback_string(self):
        # 🔴 An absent explanation renders as no explanation, which is honest. A
        # fabricated one is the defect this phase exists to prevent, and a silent
        # fallback would make the two indistinguishable on screen.
        events = []
        out = explain_scores(
            [LEAD],
            provider=lambda *_a, **_k: "Employs 9,999 people.",
            provider_config={},
            emit=events.append,
        )
        assert out == {}
        assert events[0]["type"] == "score_explanation_rejected"
        assert "9,999" in events[0]["reason"]

    def test_one_prospect_failing_does_not_fail_the_phase(self):
        calls = {"n": 0}

        def flaky(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("provider blew up")
            return "A North Carolina employer with an RFP dated 2026-08-06."

        out = explain_scores(
            [LEAD, {**LEAD, "id": "p2"}], provider=flaky, provider_config={}
        )
        assert list(out) == ["p2"]

    def test_a_legacy_prospect_is_skipped_entirely(self):
        # No gated breakdown means nothing to explain, and no call to spend.
        calls = []
        out = explain_scores(
            [{"id": "x", "score_factors": {}}],
            provider=lambda *a, **k: calls.append(1) or "text",
            provider_config={},
        )
        assert out == {} and calls == []

    def test_the_prompt_forbids_facts_outside_the_list(self):
        seen = {}
        explain_scores(
            [LEAD],
            provider=lambda prompt, **_k: seen.setdefault("p", prompt) or "NC lead, 93.",
            provider_config={},
        )
        assert "must not" in seen["p"].lower()
        assert str(MAX_CHARS) in seen["p"]


class TestTheJudgmentPromptStaysLegacySafe:
    """🔴 The plan said to delete `ALSO RATE THE FIT`. Doing that outright is a regression."""

    def test_the_fit_request_still_exists_for_legacy(self):
        # All five live skills are legacy and `ai_score_adjustment` is a real component
        # of their score. Deleting it unconditionally would have removed a scoring input
        # from every customer in production.
        assert "ALSO RATE THE FIT" in _FIT_SECTION
        assert "adjustment" in _ADJUSTMENT_FIELD

    def test_both_blocks_are_separable_so_the_gated_path_can_drop_them(self):
        assert _FIT_SECTION and _ADJUSTMENT_FIELD


class TestItNeverBuysAGroundedSearch:
    """🔴 The phase promises zero grounded requests. Nothing asserted it until 2026-08-31.

    `gemini_provider` defaults `grounded=True`, and this phase used to call
    `provider(prompt, **provider_config)` — so every prospect silently bought a Google
    Search against a quota shared with production, whose exhaustion has already produced
    a run that completed with 0 prospects and no error. The docblock said otherwise, the
    tests were green, and the only reason it was caught is that the splat ALSO broke on a
    missing kwarg before any call went out.
    """

    def _capture(self):
        seen: list[dict] = []

        def provider(prompt, **kwargs):
            seen.append(kwargs)
            return "Scores 92 because the signal is fresh and the contact is confirmed."

        return provider, seen

    def _row(self):
        return {
            "id": "p1",
            "company_name": "Acme",
            "score_factors": {
                "gated": {
                    "total": 92,
                    "lane": "qualified",
                    "gates": {"target_market": True, "buying_window": True},
                    "bonus": 12,
                    "bands": {
                        "signal_strength": 5,
                        "company_size": 2,
                        "confirmed_contact": 4,
                        "signal_recency": 1,
                    },
                    "selected_signal": {
                        "signal_type": "benefits change",
                        "signal_date": "2026-08-01",
                    },
                    "selected_from_fresh": True,
                }
            },
        }

    def test_the_provider_is_called_with_grounded_False(self):
        provider, seen = self._capture()
        explain_scores([self._row()], provider=provider, provider_config={"model": "m"})
        assert seen, "the provider was never called — the test proves nothing"
        assert seen[0]["grounded"] is False

    def test_it_works_from_the_STANDARD_provider_config(self):
        # `_provider_config()` returns no `timeout_s`. Splatting it raised TypeError per
        # prospect, so a config error surfaced as N individual data failures.
        provider, seen = self._capture()
        standard = {
            "model": "gemini-2.5-flash",
            "temperature": 0.1,
            "entries_per_query": 3,
            "retry_attempts": 3,
            "max_concurrency": 6,
        }
        out = explain_scores([self._row()], provider=provider, provider_config=standard)
        assert out, "a standard provider_config must not fail the phase"
        assert seen[0]["grounded"] is False
        assert seen[0]["timeout_s"] > 0

    def test_it_names_the_phase_so_the_invoice_can_be_attributed(self):
        provider, seen = self._capture()
        explain_scores([self._row()], provider=provider, provider_config={"model": "m"})
        assert seen[0]["phase"] == "score_explanation"


class TestItCannotHangThePhase:
    """🔴 `timeout_s` is accepted by the provider and IGNORED — the caller must enforce it.

    `gemini_provider` carries `timeout_s: float,  # noqa: ARG001 — enforced by the caller
    via signal/thread`. A plain sequential loop therefore passes a timeout nothing honours,
    and one hung call blocks the phase forever. Measured 2026-08-31: 20+ minutes of wall
    clock for 0.8s of CPU, no output, indistinguishable from slow work.
    """

    def _row(self, pid):
        return {
            "id": pid,
            "company_name": "Acme",
            "score_factors": {
                "gated": {
                    "total": 92, "lane": "qualified",
                    "gates": {"target_market": True, "buying_window": True},
                    "bonus": 12,
                    "bands": {"signal_strength": 5, "company_size": 2,
                              "confirmed_contact": 4, "signal_recency": 1},
                    "selected_signal": {"signal_type": "benefits change",
                                        "signal_date": "2026-08-01"},
                    "selected_from_fresh": True,
                }
            },
        }

    def test_a_hung_call_times_out_instead_of_blocking_forever(self):
        import time

        def hangs(prompt, **kwargs):
            time.sleep(30)  # far past the timeout below
            return "never returned"

        events = []
        started = time.monotonic()
        out = explain_scores(
            [self._row("p1")],
            provider=hangs,
            provider_config={"model": "m", "timeout_s": 0.5, "max_concurrency": 2},
            emit=events.append,
        )
        elapsed = time.monotonic() - started

        # The point: it RETURNS. Before the fix this test would sit for 30s and the
        # real phase sat for 20+ minutes.
        assert elapsed < 10, f"phase did not honour timeout_s (took {elapsed:.1f}s)"
        assert out == {}, "a timed-out call must not produce an explanation"

    def test_one_hung_prospect_does_not_lose_the_others(self):
        import time

        # ⚠️ Keyed on `state`, not on company_name: `build_facts` does NOT put the
        # company name in the prompt, so a name-based discriminator silently matches
        # nothing and both calls return fast — which is how the first version of this
        # test passed while proving the opposite of what it claimed. `state` reaches the
        # prompt and carries no digits, so it cannot disturb the number validation.
        def slow_for_p1(prompt, **kwargs):
            if "(ZZ)" in prompt:
                time.sleep(30)
            return "Scores 92 because the signal is fresh and the contact is confirmed."

        rows = [self._row("p1"), self._row("p2")]
        rows[0]["state"] = "ZZ"
        out = explain_scores(
            rows,
            provider=slow_for_p1,
            provider_config={"model": "m", "timeout_s": 0.5, "max_concurrency": 2},
        )
        # p2 survives. Order-preserving mapping is what makes this safe to assert.
        assert "p2" in out
        assert "p1" not in out


class TestAttachesToTheProducerShape:
    """🔴 The defect every test above was blind to: the engine emits `prospect_id`, not `id`.

    `explain_scores` filed each paragraph under `str(p.get("id"))`, the scored items the
    engine produces carry `prospect_id` and no `id`, and the runner looks each one up by
    `prospect_id` — so every lookup missed, no explanation was ever attached, and the
    phase was still billed (89 calls, ~$15 of one run). The fixtures above all use
    `"id": "p1"`, which is exactly the shape the producer does not write.

    So these build their leads THROUGH the producer: assemble, then score under a gated
    config. A hand-written dict here would pass against the defect again.
    """

    OBSERVED = {
        "Acme Benefits Group": "Opened a broker review for the Harbor district office.",
        "Birch Logistics": "Posted a request for proposals covering the Ridge depot.",
        "Cedar Dental": "Named a new people lead after the Lakeside merger.",
    }

    @staticmethod
    def _produced(names_to_description):
        """Scored items from the real assembler and the real scorer, gated."""
        from datetime import date

        import av_lead_scanner as als
        from tests.test_gated_score import CFG

        prospects = []
        for name, description in names_to_description.items():
            raw = {
                "company_name": name,
                "state": "NC",
                "employee_count": "250",
                "key_contact": "A Contact",
                "contact_email": "a@b.c",
            }
            groups = {
                als.normalize_name(name): [
                    {"raw": raw, "source": "in_market_triggers", "name_field": "company_name"}
                ]
            }
            p = als._assemble_prospects(
                groups=groups, scan_run_id="r1", canonical=tuple(raw)
            )[0]
            p["validation_data"] = {
                "switching_signal": [
                    {
                        "signal_type": "rfp activity",
                        "signal_class": "rfp_active",
                        "signal_date": "2026-08-01",
                        "signal_description": description,
                    }
                ]
            }
            p["_ai_judgment"] = {"pipeline_status": "4 - Active Pursuit"}
            prospects.append(p)

        ctx = {
            "scoring": {**CFG, "score_cap": 100},
            "pipeline": {"stages": [
                {"key": "4 - Active Pursuit", "min_months": 4, "max_months": 8,
                 "kind": "timing"}]},
            "skill_type": "customer",
        }
        scored = als.score_prospects(prospects, ctx, today=date(2026, 8, 27))

        # Document the shape this class relies on, so it cannot drift unnoticed.
        assert len(scored) == len(names_to_description)
        for item in scored:
            assert item.get("prospect_id"), "the producer must name the lead"
            assert "id" not in item, "the producer writes prospect_id, never id"
            assert item["score_factors"].get("gated"), "needs a gated breakdown"
        return scored

    @staticmethod
    def _by_name(scored):
        return {item["company_name"]: item for item in scored}

    @staticmethod
    def _observed_in(prompt):
        import re

        return re.search(r'What we observed: "(.*)"', prompt).group(1)

    def _provider(self):
        # `build_facts` does not put the company name in the prompt, so the per-lead
        # discriminator is the quoted finding, which does.
        def provider(prompt, **_kw):
            return f"Worth a call. We saw that it {self._observed_in(prompt)}"

        return provider

    def test_every_lead_with_an_accepted_paragraph_carries_it(self):
        scored = self._produced(self.OBSERVED)
        explanations = explain_scores(
            scored, provider=self._provider(), provider_config={}
        )
        attach_explanations(scored, explanations)

        assert len(scored) == len(self.OBSERVED)
        for item in scored:
            assert item.get("score_explanation"), item["company_name"]

    def test_the_scored_callback_carries_the_paragraph(self):
        scored = self._produced(self.OBSERVED)
        by_name = self._by_name(scored)
        unexplained = by_name["Cedar Dental"]
        explained = by_name["Acme Benefits Group"]

        def provider(prompt, **_kw):
            if "Lakeside" in prompt:
                raise RuntimeError("no paragraph for this one")
            return self._provider()(prompt)

        explanations = explain_scores(scored, provider=provider, provider_config={})
        attach_explanations(scored, explanations)

        mapped = map_event({"type": "scored", "items": scored})
        assert [kind for kind, _ in mapped] == ["scored"]
        payload_items = {i["prospect_id"]: i for i in mapped[0][1]["data"]}

        sent = payload_items[explained["prospect_id"]]
        assert sent["score_explanation"] == explained["score_explanation"]
        assert "Harbor district" in sent["score_explanation"]
        assert "score_explanation" not in payload_items[unexplained["prospect_id"]]

    def test_two_leads_never_swap_paragraphs(self):
        scored = self._produced(self.OBSERVED)
        explanations = explain_scores(
            scored, provider=self._provider(), provider_config={}
        )
        attach_explanations(scored, explanations)

        for name, item in self._by_name(scored).items():
            text = item.get("score_explanation") or ""
            assert self.OBSERVED[name] in text, name
            for other, description in self.OBSERVED.items():
                if other != name:
                    assert description not in text, f"{name} carries a paragraph written for {other}"

    def test_a_lead_with_no_identifier_is_not_explained(self):
        scored = self._produced({
            "Acme Benefits Group": self.OBSERVED["Acme Benefits Group"],
            "Birch Logistics": self.OBSERVED["Birch Logistics"],
        })
        identified, other = scored
        keyless = {k: v for k, v in other.items() if k not in ("prospect_id", "id")}
        assert "prospect_id" not in keyless and "id" not in keyless

        calls = []
        base = self._provider()

        def provider(prompt, **kw):
            calls.append(prompt)
            return base(prompt, **kw)

        out = explain_scores(
            [identified, keyless], provider=provider, provider_config={}
        )

        assert len(calls) == 1, "a lead nothing can be attached to must not be paid for"
        assert "None" not in out
        assert list(out) == [identified["prospect_id"]]

    def test_an_empty_string_identifier_is_not_explained(self):
        scored = self._produced({"Acme Benefits Group": self.OBSERVED["Acme Benefits Group"],
                                 "Birch Logistics": self.OBSERVED["Birch Logistics"]})
        identified, other = scored
        blank = {**other, "prospect_id": ""}

        calls = []
        base = self._provider()

        def provider(prompt, **kw):
            calls.append(prompt)
            return base(prompt, **kw)

        out = explain_scores([identified, blank], provider=provider, provider_config={})

        assert len(calls) == 1
        assert list(out) == [identified["prospect_id"]]

    def test_prospect_id_wins_when_both_keys_are_present(self):
        # The engine writes `prospect_id`; `id` is the fixtures' and older callers' name.
        assert explanation_key({"prospect_id": "a", "id": "b"}) == "a"

        item = {**LEAD, "prospect_id": "a", "id": "b"}
        out = explain_scores(
            [item],
            provider=lambda *_a, **_k: "A North Carolina employer with an RFP dated 2026-08-06.",
            provider_config={},
        )
        assert list(out) == ["a"]
        assert attach_explanations([item], out) == 1
        assert item["score_explanation"] == out["a"]

    def test_the_runner_step_attaches_to_producer_output(self):
        from aeo.runner import explain_scored

        scored = self._produced(self.OBSERVED)
        attached = explain_scored(
            scored,
            gated=True,
            provider=self._provider(),
            provider_config={},
            emit=lambda _e: None,
        )

        assert attached == len(self.OBSERVED)
        for name, item in self._by_name(scored).items():
            assert self.OBSERVED[name] in item["score_explanation"], name

    def test_the_runner_step_does_nothing_for_a_legacy_run(self):
        from aeo.runner import explain_scored

        scored = self._produced(self.OBSERVED)
        calls = []

        def provider(prompt, **_kw):
            calls.append(prompt)
            return "unused"

        attached = explain_scored(
            scored, gated=False, provider=provider, provider_config={}, emit=lambda _e: None
        )

        assert attached == 0 and calls == []
        assert all("score_explanation" not in item for item in scored)

    def test_failure_and_rejection_events_name_the_real_lead(self):
        scored = self._produced(self.OBSERVED)
        by_name = self._by_name(scored)

        def provider(prompt, **_kw):
            if "Harbor district" in prompt:
                raise RuntimeError("provider blew up")
            if "Ridge depot" in prompt:
                return "Call 555 today."  # a number that is in none of its inputs
            return self._provider()(prompt)

        events: list[dict] = []
        out = explain_scores(
            scored, provider=provider, provider_config={}, emit=events.append
        )

        failed = [e for e in events if e["type"] == "score_explanation_failed"]
        rejected = [e for e in events if e["type"] == "score_explanation_rejected"]
        assert len(failed) == 1 and len(rejected) == 1
        assert failed[0]["prospect_id"] == by_name["Acme Benefits Group"]["prospect_id"]
        assert rejected[0]["prospect_id"] == by_name["Birch Logistics"]["prospect_id"]
        assert failed[0]["prospect_id"] and rejected[0]["prospect_id"]
        assert list(out) == [by_name["Cedar Dental"]["prospect_id"]]
