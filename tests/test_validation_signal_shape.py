"""`signals_found` carries DATED signal objects, and both shapes survive.

Stage 4 of the scoring redesign. The gate's buying-window test reads
`validation_data.<signal_source>` and needs `signal_date` on each row; until now this
phase emitted an array of plain strings, so four of the five live verticals had nothing
for it to compute an age from. Measured in the spec across 619 scored prospects:
healthcare 68 of 69 carried structured signals, the other four carried **zero**.

🔑 These tests are deliberately vertical-agnostic. The defect this change exists to fix
was one config's shape being baked into shared code, so a test that asserts on HVAC
permits or benefits renewals would reproduce the bug it is meant to prevent. The signal
labels below are nonsense on purpose — the contract is the SHAPE, not the vocabulary.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aeo.phases import validation as V  # noqa: E402


class TestCoerceSignals:
    def test_objects_pass_through_with_their_keys(self):
        got = V._coerce_signals(
            [{"signal_type": "aaa", "signal_date": "2026-01-15", "signal_description": "x"}]
        )
        assert got == [
            {"signal_type": "aaa", "signal_date": "2026-01-15", "signal_description": "x"}
        ]

    def test_a_bare_string_is_kept_as_an_UNDATED_signal(self):
        """The legacy shape, and what a model returns when it ignores the format.

        Dropping it would discard a real finding; dating it would invent one. Keeping the
        text with no date is the only option that loses nothing and claims nothing.
        """
        got = V._coerce_signals(["Open bid posting found on the county portal"])
        assert got == [{"signal_description": "Open bid posting found on the county portal"}]
        assert "signal_date" not in got[0]

    def test_mixed_shapes_both_survive(self):
        got = V._coerce_signals(
            [{"signal_type": "bbb", "signal_date": "2026-02-01"}, "a plain string"]
        )
        assert len(got) == 2
        assert got[0]["signal_date"] == "2026-02-01"
        assert got[1] == {"signal_description": "a plain string"}

    def test_unknown_keys_are_dropped_so_the_shape_stays_a_contract(self):
        got = V._coerce_signals([{"signal_type": "ccc", "confidence": 0.9, "junk": 1}])
        assert got == [{"signal_type": "ccc"}]

    def test_nulls_and_empties_are_dropped_not_kept_as_holes(self):
        assert V._coerce_signals([{"signal_type": None, "signal_date": None}]) == []
        assert V._coerce_signals(["", "   "]) == []
        assert V._coerce_signals([None, 42, []]) == []

    def test_a_non_list_yields_nothing_rather_than_raising(self):
        for bad in (None, "string", {"signal_type": "x"}, 7):
            assert V._coerce_signals(bad) == []


class TestPromptsAskForTheShape:
    """Both prompts must ask for it — the batched one is dormant but not dead.

    `DEFAULT_VALIDATION_BATCH` is 1, so the batched prompt is unused today. It would ship
    the moment that constant changes, and a shape change that updated only the live prompt
    would regress silently at exactly that moment.
    """

    def test_both_prompts_request_all_three_signal_keys(self):
        for name, prompt in (("single", V._PROMPT), ("batched", V._BATCHED_PROMPT)):
            for key in V.SIGNAL_KEYS:
                assert key in prompt, f"{name} prompt does not ask for {key}"

    def test_both_prompts_forbid_dating_the_signal_today(self):
        for name, prompt in (("single", V._PROMPT), ("batched", V._BATCHED_PROMPT)):
            low = prompt.lower()
            assert "never today" in low, f"{name} prompt does not rule out today's date"
            assert "null" in low, f"{name} prompt does not offer null for an unknown date"

    def test_the_prompts_name_no_vertical(self):
        """The bug this change fixes was one vertical's shape baked into shared code.

        A prompt that illustrates `signal_type` with `permit_filed` teaches every vertical
        to look for permits. The label has to come from the seller's own declared signals,
        which are already interpolated into the prompt.
        """
        VERTICAL_WORDS = (
            "hvac", "permit_filed", "lease_signed", "flooring", "eap",
            "benefits", "mechanical", "healthcare", "franchise", "staffing",
        )
        for name, prompt in (("single", V._PROMPT), ("batched", V._BATCHED_PROMPT)):
            low = prompt.lower()
            found = [w for w in VERTICAL_WORDS if w in low]
            assert not found, f"{name} prompt names verticals: {found}"


class TestGateCompatibility:
    def test_an_undated_signal_can_never_open_the_buying_window(self):
        """The safety property behind accepting bare strings.

        A promoted string reaches the gate as a dict, so it passes the `isinstance(s, dict)`
        filter — but it carries no date, so freshness cannot pass it. A string therefore
        counts as "a signal is present" without ever making a stale lead look current.
        """
        from datetime import date

        from aeo.gated_score import fresh_signals

        promoted = V._coerce_signals(["something true but undated"])
        assert fresh_signals(promoted, 18, date(2026, 9, 1)) == []
