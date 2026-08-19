"""The test-run prospect ceiling.

The load-bearing assertion here is the NEGATIVE one: a real run must come back with
exactly what the skill configured. The cap only ever moves a ceiling downward, and only
for a run the gateway explicitly marked `test: true` -- because the failure mode of
getting it wrong is capping production scans to 15 prospects, which looks like a thin
market rather than a bug.
"""

from __future__ import annotations

from aeo.bootstrap import job_from_payload
from aeo.runner import TEST_RUN_MAX_PROSPECTS, _resolve_prospect_ceiling


def _config(max_prospects: int | None) -> dict[str, object]:
    if max_prospects is None:
        return {"discovery": {}}
    return {"discovery": {"max_prospects": max_prospects}}


# ── the ceiling rule ──────────────────────────────────────────────────────


def test_real_run_is_untouched_even_when_far_above_the_cap() -> None:
    """The one that matters. Resource Floor Care runs 150 for real."""
    limit, reason = _resolve_prospect_ceiling(_config(150), is_test=False)
    assert limit == 150
    assert reason == "skill config"


def test_real_run_stays_unbounded_when_the_skill_declares_no_ceiling() -> None:
    limit, _ = _resolve_prospect_ceiling(_config(None), is_test=False)
    assert limit is None


def test_test_run_is_capped_down_from_a_larger_configured_ceiling() -> None:
    limit, reason = _resolve_prospect_ceiling(_config(150), is_test=True)
    assert limit == TEST_RUN_MAX_PROSPECTS
    assert "capped down from the skill's 150" in reason


def test_test_run_keeps_a_smaller_configured_ceiling_rather_than_raising_it() -> None:
    """`min`, not assignment -- a skill set to 5 must not be promoted to 15."""
    limit, reason = _resolve_prospect_ceiling(_config(5), is_test=True)
    assert limit == 5
    assert "already at or below" in reason


def test_test_run_gets_the_cap_when_the_skill_declares_no_ceiling() -> None:
    """An unbounded TEST run is the case the cap exists to prevent."""
    limit, reason = _resolve_prospect_ceiling(_config(None), is_test=True)
    assert limit == TEST_RUN_MAX_PROSPECTS
    assert "declared no ceiling" in reason


def test_a_configured_ceiling_exactly_at_the_cap_is_unchanged() -> None:
    limit, _ = _resolve_prospect_ceiling(
        _config(TEST_RUN_MAX_PROSPECTS), is_test=True
    )
    assert limit == TEST_RUN_MAX_PROSPECTS


# ── the flag only arrives from an explicit boolean ────────────────────────

_REQUIRED = {
    "organization_id": "org-1",
    "tenant_id": "tenant-1",
    "scan_run_id": "run-1",
    "skill": {"slug": "some-skill"},
}


def test_payload_marks_a_test_run_when_test_is_boolean_true() -> None:
    resolved = job_from_payload({**_REQUIRED, "test": True})
    assert resolved["SCAN_IS_TEST"] == "true"


def test_payload_omits_the_marker_for_a_real_run() -> None:
    """`test: False` and an absent key must be indistinguishable downstream."""
    assert "SCAN_IS_TEST" not in job_from_payload({**_REQUIRED, "test": False})
    assert "SCAN_IS_TEST" not in job_from_payload(dict(_REQUIRED))


def test_payload_ignores_truthy_non_booleans() -> None:
    """A string "true", a 1, or a stray dict must NOT cap a production run. Only the
    gateway's real boolean counts -- anything else is treated as a real run."""
    for value in ("true", "True", 1, "1", {"enabled": True}, ["yes"]):
        resolved = job_from_payload({**_REQUIRED, "test": value})
        assert "SCAN_IS_TEST" not in resolved, f"{value!r} must not mark a test run"


def test_test_flag_does_not_disturb_the_required_payload_mapping() -> None:
    resolved = job_from_payload({**_REQUIRED, "test": True})
    assert resolved["ORGANIZATION_ID"] == "org-1"
    assert resolved["TENANT_ID"] == "tenant-1"
    assert resolved["SCAN_RUN_ID"] == "run-1"
    assert resolved["SKILL_SLUG"] == "some-skill"
