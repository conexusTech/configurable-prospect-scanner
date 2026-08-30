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
from aeo.phases.score_explanation import (
    MAX_CHARS,
    build_facts,
    explain_scores,
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
