"""Every authored config key must reach the model, or be rejected — never ignored.

One audit (2026-08-12) found **three** keys that CSB-built skills authored and nothing
read: `scoring.factors`, `validation.rules` and `contacts.contact_preferences`. On the
first real production run the operator's `min_square_footage: 10000` never reached the
judgement, and the model's reasoning cited a 5,000 threshold that existed in neither the
config nor the code.

An unread key is indistinguishable from a respected one at every surface the operator
sees: it validates, it renders in the draft panel, the skill finalizes, the scan
completes. So the wiring is asserted here rather than trusted.
"""
from __future__ import annotations

from typing import Any

from aeo.phases.contacts import find_contacts
from aeo.phases.validation import validate_prospects

PROSPECT = {
    "id": "p1",
    "company_name": "Metropolitan Property Management, LLC",
    "city": "Nashville",
    "state": "TN",
}


class _CapturingProvider:
    """Stands in for the grounded-search provider and keeps every prompt."""

    def __init__(self, reply: str = "[]"):
        self.prompts: list[str] = []
        self._reply = reply

    def __call__(self, prompt: str, **_kwargs: Any) -> str:
        self.prompts.append(prompt)
        return self._reply


def _parse_json_array(_raw: str) -> list[dict[str, Any]]:
    return []


class TestValidationRulesReachTheModel:
    def test_rules_are_rendered_into_the_prompt(self):
        provider = _CapturingProvider()
        validate_prospects(
            [PROSPECT],
            validation_config={
                "in_market_signals": ["recent permit"],
                "disqualifiers": ["has in-house mechanical staff"],
                # The operator's real threshold from run c214e3d5.
                "rules": {"min_square_footage": 10000},
            },
            provider=provider,
            provider_config={"model": "m"},
            parse_json_array=_parse_json_array,
        )
        assert provider.prompts, "the provider must have been called"
        prompt = provider.prompts[0]
        assert "min_square_footage" in prompt, "the authored rule never reached the model"
        assert "10000" in prompt

    def test_unmeasurable_rules_are_not_treated_as_failures(self):
        """Mirrors the module's own doctrine: absence of evidence is not
        disqualification. Without this the thresholds would shrink every result set."""
        provider = _CapturingProvider()
        validate_prospects(
            [PROSPECT],
            validation_config={"rules": {"min_square_footage": 10000}},
            provider=provider,
            provider_config={"model": "m"},
            parse_json_array=_parse_json_array,
        )
        # Collapse whitespace: the prompt is hard-wrapped, so a phrase can straddle a
        # newline. Assert on the instruction, not on its line breaks.
        prompt = " ".join(provider.prompts[0].lower().split())
        assert "cannot evaluate" in prompt and "not a failure" in prompt

    def test_absent_rules_do_not_break_the_prompt(self):
        provider = _CapturingProvider()
        validate_prospects(
            [PROSPECT],
            validation_config={"in_market_signals": ["x"]},
            provider=provider,
            provider_config={"model": "m"},
            parse_json_array=_parse_json_array,
        )
        assert "(none specified)" in provider.prompts[0]


class TestCompletedSummaryReportsAllFourCounters:
    """`total_zips` and `total_validated` were never sent.

    The engine's completed summary carried only `total_prospects` and `total_scored`, and
    the event mapper filters to the keys AEO declares — so the gateway wrote NULL for two
    counters while the underlying rows persisted perfectly (60 zip rows on a run whose
    `total_zips` was null). That read as a gateway defect for a day; it was one dictionary
    in the runner.
    """

    def test_the_mapper_forwards_all_four_and_drops_the_rest(self):
        from aeo.event_mapping import map_event

        posts = map_event(
            {
                "type": "completed",
                "summary": {
                    "total_zips": 60,
                    "total_prospects": 36,
                    "total_validated": 34,
                    "total_scored": 12,
                    "provider": "gemini",
                },
            }
        )
        summary = posts[0][1]["summary"]
        assert summary == {
            "total_zips": 60,
            "total_prospects": 36,
            "total_validated": 34,
            "total_scored": 12,
        }, "all four declared counters, and nothing undeclared"

    def test_a_zero_counter_is_still_reported(self):
        """Zero is a real answer — omitting it would leave the column NULL and make
        'no zips discovered' indistinguishable from 'never told you'."""
        from aeo.event_mapping import map_event

        posts = map_event({"type": "completed", "summary": {"total_zips": 0, "total_prospects": 0}})
        assert posts[0][1]["summary"]["total_zips"] == 0


class TestContactPreferencesReachTheModel:
    def test_preferences_are_rendered_into_the_prompt(self):
        provider = _CapturingProvider()
        find_contacts(
            [PROSPECT],
            contacts_config={
                "titles": ["Facilities Director"],
                "seniorities": ["VP"],
                "contact_preferences": ["prefer direct email over gatekeepers"],
            },
            product_description="commercial HVAC service",
            provider=provider,
            provider_config={"model": "m"},
            parse_json_array=_parse_json_array,
        )
        assert provider.prompts, "the provider must have been called"
        assert "gatekeepers" in provider.prompts[0], "contact_preferences was ignored"
