"""Quota safety and cost metering.

Covers the 2026-08-26 incident, in which the Google Search grounding quota was
exhausted and three customer test scans returned `status=completed`,
`total_prospects=0`, `error=NULL` in ~90 seconds each. Their configuration was fine.
Every discovery query had died on `429 RESOURCE_EXHAUSTED`, and nothing said so.

The behaviours pinned here are the ones that make that impossible to repeat:

* a dead allowance is distinguishable from an empty market  (`ScanAbort.reason`)
* it is detected DURING the sweep, not after paying for all of it
* it is not retried once established
* what we were billed for is recorded, per phase, in units
"""

from __future__ import annotations

import json

import pytest

import av_lead_scanner as als


# ── the quota-error detector ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "msg",
    [
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429}}",
        "resource_exhausted",
        "429 You exceeded your current quota, please check your plan and billing details",
        "Error code: 429 - insufficient_quota",
    ],
)
def test_recognises_an_exhausted_allowance(msg):
    assert als._is_quota_error(msg) is True


@pytest.mark.parametrize(
    "msg",
    [
        # ⚠️ The one that must NOT match. Providers return 429 for per-minute
        # throttling too, and that IS worth waiting out — treating it as a dead
        # allowance would abort runs that only needed to slow down.
        "429 Too Many Requests: rate limit exceeded, retry in 12s",
        "503 Service Unavailable",
        "500 internal error",
        "read operation timed out",
    ],
)
def test_does_not_mistake_throttling_or_noise_for_an_allowance(msg):
    assert als._is_quota_error(msg) is False


# ── the breaker ──────────────────────────────────────────────────────────────


def test_breaker_trips_only_on_consecutive_errors_and_then_stays_tripped():
    b = als._QuotaBreaker(3)
    assert b.note_quota_error() is False
    assert b.note_quota_error() is False
    assert b.tripped is False, "must not trip below the threshold"

    # A success clears the CONSECUTIVE run, so isolated 429s spread across a long
    # healthy run never accumulate into a trip.
    b.note_success()
    assert b.note_quota_error() is False
    assert b.note_quota_error() is False
    assert b.tripped is False

    assert b.note_quota_error() is True, "the third consecutive error trips it"
    assert b.tripped is True

    # 🔑 Sticky. A monthly allowance does not come back mid-run, so un-tripping would
    # only re-enter the retry storm the breaker exists to end.
    b.note_success()
    assert b.tripped is True, "a later success must NOT un-trip the breaker"

    # And it reports the trip rather than only recording it.
    with pytest.raises(als.ProviderQuotaExhausted):
        b.raise_if_tripped("gemini")


def test_an_untripped_breaker_lets_calls_through():
    b = als._QuotaBreaker(3)
    b.raise_if_tripped("gemini")  # must not raise


def test_breaker_counts_every_quota_error_for_the_report():
    b = als._QuotaBreaker(2)
    for _ in range(5):
        b.note_quota_error()
    assert b.total_quota_errors == 5


# ── the discovery abort decision ─────────────────────────────────────────────


def test_a_quota_failure_aborts_on_the_very_first_one():
    """No threshold consulted. The breaker is sticky and process-wide, so it has
    already established that every remaining query will fail — there is nothing left
    to sample, only money to spend."""
    abort = als._discovery_abort(
        exc=als.ProviderQuotaExhausted("gemini quota exhausted"), failed=1, total=60
    )
    assert isinstance(abort, als.ProviderQuotaExhausted)
    assert abort.reason == "provider_quota_exhausted"


def test_one_ordinary_failure_in_a_small_sweep_does_not_abort():
    """⚠️ The floor, and it is load-bearing.

    MYgroup's real shape was 7 queries. At a bare 10% rate the FIRST failure is
    1/7 = 14% and would abort — turning one transient malformed response into a failed
    customer run, and undoing `_run_query`'s deliberate "one bad query mustn't kill the
    source" tolerance. `DISCOVERY_FAILURE_ABORT_MIN` keeps that tolerance for genuine
    one-offs.
    """
    assert als._discovery_abort(exc=RuntimeError("bad json"), failed=1, total=7) is None


def test_two_failures_in_a_small_sweep_do_abort():
    abort = als._discovery_abort(exc=RuntimeError("bad json"), failed=2, total=7)
    assert isinstance(abort, als.DiscoveryFailureRateExceeded)
    assert abort.reason == "discovery_failure_rate_exceeded"


def test_a_large_sweep_aborts_early_rather_than_at_the_end():
    """The whole point of 10% over 50%: catch a 60-query sweep around query 7, not
    query 30. At 50% the run has already been paid for by the time it is failed."""
    assert als._discovery_abort(exc=RuntimeError("x"), failed=6, total=60) is None
    abort = als._discovery_abort(exc=RuntimeError("x"), failed=7, total=60)
    assert isinstance(abort, als.DiscoveryFailureRateExceeded)


def test_the_reason_code_is_carried_and_stable():
    """`scan_runs.error` is free text, and free text is why this outage was invisible.
    Consumers match the prefix, so these strings are a contract, not prose."""
    assert als.ProviderQuotaExhausted.reason == "provider_quota_exhausted"
    assert als.DiscoveryFailureRateExceeded.reason == "discovery_failure_rate_exceeded"
    assert issubclass(als.ProviderQuotaExhausted, als.ScanAbort)
    assert issubclass(als.DiscoveryFailureRateExceeded, als.ScanAbort)


# ── discover(): the silent-success defect, end to end ────────────────────────

_CTX = {
    "organization": {"name": "TestCo"},
    "product_description": "test product",
    "sources": {
        "src_a": {
            "name_field": "organization_name",
            "fields": ["organization_name", "city", "state"],
            # Ten queries so the 10% rate is expressible without the floor deciding it.
            "queries": [f"query a{i}" for i in range(10)],
        },
    },
}

_PC = {"model": "m", "temperature": 0.1, "entries_per_query": 3, "retry_attempts": 1,
       "max_concurrency": 1}


def _row(name):
    return json.dumps([{"organization_name": name, "city": "Austin", "state": "TX"}])


def test_a_total_outage_raises_instead_of_reporting_an_empty_market():
    """🔑 THE defect. Every query failing used to return `[]`, and the run went on to
    report `completed` with 0 prospects and `error` NULL — indistinguishable from
    "we searched your market and found nobody", from the DB to the customer's screen.
    """
    def dead(prompt, **kw):
        raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")

    with pytest.raises(als.ScanAbort) as caught:
        als.discover(_CTX, scan_run_id="s", provider=dead, emit=lambda e: None,
                     provider_config=_PC)
    assert caught.value.reason in {
        "provider_quota_exhausted", "discovery_failure_rate_exceeded"
    }


def test_a_single_bad_query_still_does_not_kill_the_sweep():
    """The tolerance that predates this work is preserved: 1 of 10 is under both the
    rate and the floor, so the other nine results stand."""
    calls = {"n": 0}

    def flaky(prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("malformed response")
        return _row(f"Co {calls['n']}")

    prospects = als.discover(_CTX, scan_run_id="s", provider=flaky,
                             emit=lambda e: None, provider_config=_PC)
    assert len(prospects) == 9


def test_the_sweep_stops_issuing_queries_once_it_has_decided_to_fail():
    """⚠️ The reason the threshold is checked in flight rather than tallied at the end.

    A rate evaluated after the fan-out drains tells you the run was worthless *after*
    you have paid for all of it. The 2026-08-26 incident spent ~16 minutes and a full
    query set to learn nothing.
    """
    import threading
    import time

    lock = threading.Lock()
    calls = {"n": 0}

    def dead(prompt, **kw):
        with lock:
            calls["n"] += 1
        # A real grounded query is a multi-second network call. Modelled here because
        # `Future.cancel()` can only stop a query that has NOT STARTED — with an
        # instantly-failing double the single worker drains the whole queue before the
        # main thread is even scheduled to notice the second failure, which measures
        # thread timing rather than the behaviour under test.
        time.sleep(0.03)
        raise RuntimeError("malformed response")

    with pytest.raises(als.DiscoveryFailureRateExceeded):
        als.discover(_CTX, scan_run_id="s", provider=dead, emit=lambda e: None,
                     provider_config=_PC)
    # An exact count is not assertable — how many are in flight when the decision
    # lands depends on the scheduler. What must hold is that it did not pay for the
    # whole sweep, which is the entire difference between 10% and 50%.
    assert calls["n"] < 10, f"issued all {calls['n']} queries despite deciding to fail"


# ── the cost meter ───────────────────────────────────────────────────────────


def test_meter_counts_units_and_never_money():
    """⚠️ The scanner must not price anything.

    The price table lives gateway-side and is env-overridable there, so a rate change
    — or calibrating the searches-per-request multiplier once there is finally data —
    re-prices history instead of leaving it permanently wrong. A `cost`/`usd` key
    appearing in this snapshot means that split has been broken.
    """
    m = als._CostMeter()
    m.record(phase="discovery", model="flash", grounded=True, search_queries=3,
             input_tokens=1000, output_tokens=100)
    snap = m.snapshot()
    assert not [k for k in snap if "usd" in k.lower() or "cost" in k.lower()]


def test_meter_separates_billed_searches_from_request_count():
    """🔑 Grounding bills per SEARCH QUERY, and one request may fire several.

    Reporting requests as searches understates the bill by exactly the multiplier
    nobody has measured — and discarding this field is why an $800 invoice could not
    be attributed to anything.
    """
    m = als._CostMeter()
    m.record(phase="discovery", model="flash", grounded=True, search_queries=4)
    m.record(phase="discovery", model="flash", grounded=True, search_queries=1)
    snap = m.snapshot()
    assert snap["grounded_requests"] == 2
    assert snap["grounded_search_queries"] == 5


def test_an_ungrounded_call_is_counted_but_bills_no_search():
    """Judgment after the fix: a real call on the expensive model, zero search meter.
    A reader seeing 0 requests must still see its token cost."""
    m = als._CostMeter()
    m.record(phase="judgment", model="pro", grounded=False,
             input_tokens=5000, output_tokens=400, thinking_tokens=171)
    snap = m.snapshot()
    assert snap["calls"] == 1
    assert snap["grounded_requests"] == 0
    assert snap["grounded_search_queries"] == 0
    assert snap["thinking_tokens"] == 171


def test_meter_attributes_by_phase_and_by_model():
    """Both axes, because they answer different questions: by_phase says WHERE the
    money went, by_model says at WHICH RATE it was billed."""
    m = als._CostMeter()
    m.record(phase="discovery", model="flash", grounded=True, search_queries=2,
             input_tokens=10)
    m.record(phase="judgment", model="pro", grounded=False, input_tokens=90)

    snap = m.snapshot()
    by_phase = {e["phase"]: e for e in snap["by_phase"]}
    by_model = {e["model"]: e for e in snap["by_model"]}

    assert by_phase["discovery"]["grounded_search_queries"] == 2
    assert by_phase["judgment"]["grounded_search_queries"] == 0
    assert by_model["pro"]["input_tokens"] == 90
    assert snap["input_tokens"] == 100, "totals must equal the sum of the buckets"


def test_meter_records_a_failed_call_without_inflating_billed_units():
    """A 429 is a call we made and were not billed a search for. Both facts matter:
    the failure count is the degradation signal, the zeroes keep the bill honest."""
    m = als._CostMeter()
    m.record(phase="discovery", model="flash", grounded=True, failed=True)
    snap = m.snapshot()
    assert snap["failed_calls"] == 1
    assert snap["grounded_search_queries"] == 0


def test_usage_extraction_survives_a_response_shape_it_does_not_recognise():
    """These are preview models on a moving SDK. A metering shim must NEVER fail a
    call that already succeeded and was already paid for — a missing field costs one
    row of attribution; a raise would throw away the answer we just bought."""
    m_before = als._cost_meter.snapshot()["calls"]

    class Weird:
        usage_metadata = "not an object"

        @property
        def candidates(self):
            raise RuntimeError("SDK changed under us")

    als._record_gemini_usage(Weird(), phase="discovery", model="flash", grounded=True)
    assert als._cost_meter.snapshot()["calls"] == m_before + 1


def test_meter_records_the_search_distribution_not_just_a_total():
    """🔑 A sum cannot yield a median.

    agent-service measured 335 real grounded requests: mean 3.68, median 3, **max 35**.
    A scalar mean cannot say that the expensive runs are expensive for a structural
    reason, which is the whole question an operator asks after a costly run.
    """
    m = als._CostMeter()
    for n in (2, 2, 3, 35):
        m.record(phase="discovery", model="flash", grounded=True, search_queries=n)
    hist = m.snapshot()["search_histogram"]
    # String keys — this crosses a JSON boundary where int keys become strings anyway.
    assert hist == {"2": 2, "3": 1, "35": 1}


def test_histogram_counts_zero_search_requests():
    """⚠️ 29 of 335 requests (8.7%) fired NO search — a model that chose not to, not
    missing data. Dropping them flatters the mean by ~10%, so they are counted at 0."""
    m = als._CostMeter()
    m.record(phase="discovery", model="flash", grounded=True, search_queries=0)
    m.record(phase="discovery", model="flash", grounded=True, search_queries=4)
    assert m.snapshot()["search_histogram"] == {"0": 1, "4": 1}


def test_histogram_excludes_ungrounded_and_failed_calls():
    """An ungrounded call never touches the search meter, and a failed one was not
    billed a search — either in the histogram would drag the distribution toward zero
    and understate what a real grounded request costs."""
    m = als._CostMeter()
    m.record(phase="judgment", model="pro", grounded=False, input_tokens=10)
    m.record(phase="discovery", model="flash", grounded=True, failed=True)
    assert m.snapshot()["search_histogram"] == {}


# ── the Gemini false positive (agent-service, 2026-08-27) ────────────────────

#: Gemini's REAL throttle body. 🔴 It is `RESOURCE_EXHAUSTED` **and** says `quota`, so
#: neither branch of the original predicate could tell it from an exhausted allowance.
_GEMINI_THROTTLE = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for quota metric 'Generate Content API "
    "requests' and limit 'requests per minute' of service 'generativelanguage.googleapis.com'"
)

#: Gemini's REAL allowance body — same status, same `quota` wording, different window.
_GEMINI_ALLOWANCE = (
    "429 RESOURCE_EXHAUSTED. Quota exceeded for quota metric 'Grounding requests' "
    "and limit 'Grounding requests per day'"
)


def test_gemini_per_minute_throttle_is_not_a_dead_allowance():
    """🔴 The false positive agent-service found after adopting this predicate.

    At SCANNER_PHASE_CONCURRENCY=8 three consecutive per-minute 429s are ordinary. The
    original version returned True for them, so the breaker would have tripped and
    terminated a **healthy** run as `provider_quota_exhausted`.

    The old negative tests missed it because they pinned OpenAI's throttle shape — and
    Gemini is the vendor holding the scarce meter.
    """
    assert als._is_short_window_throttle(_GEMINI_THROTTLE) is True
    assert als._is_quota_error(_GEMINI_THROTTLE) is False


def test_gemini_daily_allowance_is_still_terminal():
    """The fix must not swing the other way: a real allowance error still aborts."""
    assert als._is_short_window_throttle(_GEMINI_ALLOWANCE) is False
    assert als._is_quota_error(_GEMINI_ALLOWANCE) is True


def test_a_throttle_wins_the_tie_even_when_the_body_also_says_quota():
    """Window scope FIRST, allowance second. Both words appear in Gemini's throttle, so
    ordering is the whole mechanism — not a tiebreak that rarely matters."""
    both = "429 RESOURCE_EXHAUSTED quota exceeded, limit requests per minute, billing ok"
    assert als._is_quota_error(both) is False


def test_an_ambiguous_body_is_treated_as_transient():
    """Biased toward transient on purpose: a false positive aborts a run that would have
    succeeded; a false negative is only the behaviour that shipped before this existed."""
    for msg in ("", "429", "something went wrong", "503 Service Unavailable"):
        assert als._is_quota_error(msg) is False


def test_the_incident_body_is_still_recognised():
    """The exact message from the 2026-08-26 outage must remain terminal — the whole
    reason any of this exists."""
    incident = (
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
        "current quota, please check your plan and billing details'}}"
    )
    assert als._is_quota_error(incident) is True


# ── cost honesty: an unpriceable call must not read as a free one ────────────


def test_meter_records_cached_input_separately():
    """Cached input bills lower than fresh input, so the gateway needs it separately to
    flag a run whose cost it may be OVERSTATING — the direction that matters when the
    figure reaches a customer."""
    m = als._CostMeter()
    m.record(phase="discovery", model="flash", grounded=True,
             input_tokens=20_000, cached_input_tokens=8_000)
    assert m.snapshot()["cached_input_tokens"] == 8_000


def test_an_unpriceable_call_is_counted_as_such():
    """🔑 The distinction that makes the run's cost honest rather than merely low.

    A call that reached the provider and could not be priced was still billed. Counting
    it as nothing produces a confident under-count; counting it as `unpriced` lets the
    gateway publish the figure as a FLOOR.
    """
    m = als._CostMeter()
    m.record(phase="discovery", model="flash", grounded=True, unpriced=True, failed=True)
    snap = m.snapshot()
    assert snap["unpriced_calls"] == 1
    assert snap["calls"] == 1


def test_usage_that_cannot_be_read_marks_the_call_unpriced():
    """⚠️ The regression that mattered most: `_record_gemini_usage` swallows every read,
    so before this a renamed SDK field produced a call recorded at ZERO cost and
    indistinguishable from a cheap one."""
    before = als._cost_meter.snapshot()["unpriced_calls"]

    class NoUsage:
        candidates: list = []

    als._record_gemini_usage(NoUsage(), phase="discovery", model="flash", grounded=True)
    assert als._cost_meter.snapshot()["unpriced_calls"] == before + 1


def test_readable_usage_is_NOT_marked_unpriced():
    """The flag must discriminate, not simply always fire — otherwise every run reports
    incomplete and the signal is worthless."""
    before = als._cost_meter.snapshot()["unpriced_calls"]

    class Usage:
        prompt_token_count = 1000
        candidates_token_count = 100
        thoughts_token_count = 0
        cached_content_token_count = 0

    class Resp:
        usage_metadata = Usage()
        candidates: list = []

    als._record_gemini_usage(Resp(), phase="discovery", model="flash", grounded=False)
    assert als._cost_meter.snapshot()["unpriced_calls"] == before


def test_claude_usage_is_metered_at_all():
    """🔴 This path recorded NOTHING until 2026-08-27 — a run on `--provider claude`
    reported zero cost while we were billed in full. Latent (gemini is the default) but
    a whole provider silently costing zero is the failure metering exists to prevent."""
    m_before = als._cost_meter.snapshot()["calls"]

    class Usage:
        input_tokens = 5000
        output_tokens = 500
        cache_read_input_tokens = 0

    class Resp:
        usage = Usage()

    als._record_claude_usage(Resp(), phase="validation", model="opus", grounded=False)
    snap = als._cost_meter.snapshot()
    assert snap["calls"] == m_before + 1
    by_model = {e["model"]: e for e in snap["by_model"]}
    assert by_model["opus"]["input_tokens"] == 5000


def test_a_grounded_claude_call_is_unpriced_because_searches_are_uncountable():
    """Anthropic's web_search bills per search and the count is not on `usage` the way
    gemini's is. Pricing the token half as the whole would understate it silently."""
    before = als._cost_meter.snapshot()["unpriced_calls"]

    class Usage:
        input_tokens = 5000
        output_tokens = 500
        cache_read_input_tokens = 0

    class Resp:
        usage = Usage()

    als._record_claude_usage(Resp(), phase="discovery", model="opus", grounded=True)
    assert als._cost_meter.snapshot()["unpriced_calls"] == before + 1
