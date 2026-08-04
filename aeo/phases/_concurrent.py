"""Bounded, timeout-enforced fan-out for per-prospect model calls.

**Why this exists, and it is not a nicety.** The Gemini provider takes `timeout_s`
and ignores it — upstream marks the parameter `noqa: ARG001 — enforced by the caller
via signal/thread`. Nothing then enforces it: discovery passes `timeout_s=180` into a
provider that drops it. So a hung grounded-search call hangs forever.

Discovery survives that because it fans out across a thread pool and a stuck worker
only stalls one query. A per-prospect phase written the obvious way — a `for` loop of
provider calls — has neither property: no bound and no parallelism. The first live
four-phase run hit exactly this and did not finish in nine minutes for six calls.

So this helper does two things the engine does not:

1. **Bounds each call in wall-clock time** via `future.result(timeout=…)`, since the
   provider will not bound itself.
2. **Fans out** with the same `max_concurrency` the engine reads from provider config
   (default 6), so a phase costs about one call's latency rather than N.

A timed-out or failed item yields `None` rather than propagating: one prospect must
never fail a phase, which is the same rule discovery applies per query.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, TypeVar

#: Matches the engine's `_QUERY_TIMEOUT_S`. Per *call*, not per phase.
DEFAULT_CALL_TIMEOUT_S = 180.0

#: **Deliberately lower than the engine's 6.** Measured on a live key: one grounded
#: validation call takes ~47s because the model actually researches the company, and
#: six of those at once earns `429 RESOURCE_EXHAUSTED`. The provider's own backoff for
#: a 429 is 15s then 30s, so a rate-limited prospect can spend longer sleeping than
#: calling — which is how a slow phase becomes an unbounded one. Discovery gets away
#: with 6 because it issues a handful of queries once; a per-prospect phase multiplies
#: by the result count.
DEFAULT_MAX_CONCURRENCY = 2

#: Retries for per-prospect phase calls, **1 by design — not the engine's 3.**
#: Across N prospects, 3 attempts with 15s/30s backoff is the difference between a
#: phase that finishes and one that appears to hang. A per-prospect failure degrades
#: to an unjudged prospect (kept, flagged), which is a far better outcome than a scan
#: that never returns.
PHASE_RETRY_ATTEMPTS = 1

T = TypeVar("T")
R = TypeVar("R")


def map_bounded(
    items: list[T],
    fn: Callable[[T], R],
    *,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
    timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
    on_error: Callable[[T, BaseException], None] | None = None,
) -> list[R | None]:
    """Apply `fn` across `items`, order-preserving. `None` where it failed or timed out."""
    if not items:
        return []

    workers = max(1, min(max_concurrency, len(items)))
    results: list[R | None] = [None] * len(items)

    # NOT a `with` block, deliberately. `ThreadPoolExecutor.__exit__` calls
    # `shutdown(wait=True)`, which joins every worker — so a thread still blocked
    # inside the provider makes the pool exit hang and silently defeats the
    # per-call timeout above it. That is exactly what happened on the first live
    # four-phase run: the futures timed out on schedule and the process still
    # didn't return.
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(fn, item): index for index, item in enumerate(items)}
        for future, index in futures.items():
            try:
                results[index] = future.result(timeout=timeout_s)
            except FutureTimeout as exc:
                if on_error:
                    on_error(items[index], exc)
            except Exception as exc:  # noqa: BLE001 — one item must not fail a phase
                if on_error:
                    on_error(items[index], exc)
    finally:
        # Don't wait on stragglers. ⚠️ Python cannot kill a running thread, and
        # since 3.9 the executor's threads are non-daemon and joined at interpreter
        # exit — so a genuinely wedged provider call can still delay process exit.
        # Making calls fail fast (PHASE_RETRY_ATTEMPTS) is the real mitigation;
        # this only stops the phase itself from blocking.
        pool.shutdown(wait=False, cancel_futures=True)

    return results


def concurrency_from(provider_config: dict[str, Any]) -> int:
    """Concurrency for a per-prospect phase.

    ⚠️ **Deliberately does NOT read the engine's `max_concurrency`.** An earlier
    version did, and it silently had no effect: `_provider_config` always populates
    that key (defaulting to 6), so this function could never fall back to its own
    lower default and every phase ran 6-wide regardless. The engine's value is tuned
    for discovery — a handful of queries issued once — while a per-prospect phase
    multiplies by the result count, so borrowing it is wrong even when it works.

    Override with `SCANNER_PHASE_CONCURRENCY` when a key's rate limit allows more.
    """
    raw = os.environ.get("SCANNER_PHASE_CONCURRENCY")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_MAX_CONCURRENCY
