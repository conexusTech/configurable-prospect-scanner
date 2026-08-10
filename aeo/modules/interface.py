"""The custom-module plug-in interface (PRD R11).

**R11 is OFF at launch by decision D5**, and the split is deliberate:
aeo-agent-service *generates* these modules from a conversation, this runtime
*defines the contract and executes reviewed ones*. The interface must ship even
with generation disabled — a generator with no target contract produces code
against an imagined shape, which is how every other defect in this feature started.

So this file is the contract, and `loader.py` is the gate. Nothing here executes.

## The contract is deliberately narrow

A custom module contributes **per-prospect signals** — a flat dict merged into a
prospect's evidence. It does not get to mutate the prospect list, drop prospects,
rewrite scores, or reach the network on its own.

That is a restriction, and it is the point. This is **LLM-generated code running in
the same process as a scan that writes to a customer's database**. A module that
could remove prospects could silently empty a run; one that could rewrite scores
could reorder a salesperson's day with no audit trail. Contributing signals is
enough to be useful while keeping the blast radius to "this prospect gained a
wrong field".

⚠️ **A signal does NOT currently feed scoring, and this docstring used to say it
did** ("a signal can feed scoring through the normal config-driven path" — removed
2026-08-10). Scoring is the vendored engine's `score_prospects`, which is verbatim
upstream code that knows nothing about these keys, and it runs *before* modules
are applied. Signals are persisted to `prospects.custom_fields` and rendered; they
do not move `score`. Correcting it rather than leaving it aspirational, because a
comment asserting a path that does not exist is the failure this feature has hit
repeatedly — the next person would build against it instead of building it.

If a future module genuinely needs to filter or score, that should be a **second,
separately-reviewed interface** with its own gate, not a widening of this one.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: Bumped when this contract changes shape. A reviewed module records the version it
#: was written against, so a contract change invalidates prior reviews rather than
#: silently running old code against new expectations.
MODULE_API_VERSION = "1.0"


@runtime_checkable
class CustomModule(Protocol):
    """What a generated module must expose.

    `runtime_checkable` covers attribute presence only — Python cannot verify the
    signature or the return shape from a Protocol, so `loader.validate_module`
    checks callability and `sanitize_signals` polices what comes back. Treat
    `isinstance` here as a cheap first filter, never as proof the module is safe.
    """

    #: Stable identifier, used to namespace this module's signals so two modules
    #: cannot overwrite each other's output.
    name: str

    #: The `MODULE_API_VERSION` the module was authored and reviewed against.
    api_version: str

    def signals(self, prospect: dict[str, Any], *, context: dict[str, Any]) -> dict[str, Any]:
        """Return a flat dict of signals for one prospect.

        Contract this runtime enforces on the way out (see `sanitize_signals`):

        - Keys must be strings; values must be JSON-primitive (str/int/float/bool/None).
        - No nested structures — they end up in a JSONB column that FE renders, and
          an arbitrarily deep blob from generated code is not renderable.
        - Returning anything else, or raising, yields no signals for that prospect
          and never fails the scan.

        The module is called **once per prospect** and must be pure with respect to
        the scan: no writes, no network, no shared state between calls.
        """
        ...


#: Values a signal may carry. Anything else is dropped by `sanitize_signals`.
_PRIMITIVES = (str, int, float, bool, type(None))

#: Cap per module per prospect. A generated module returning hundreds of keys would
#: bloat every prospect row; the cap makes that a truncation rather than a database
#: problem.
MAX_SIGNALS_PER_PROSPECT = 25

#: Cap on a single signal's string length, for the same reason.
MAX_SIGNAL_LENGTH = 500


def sanitize_signals(raw: Any, *, module_name: str) -> dict[str, Any]:
    """Coerce a module's return value into something safe to persist.

    Applied to **every** module return, because the module is generated code and the
    Protocol cannot constrain what it actually returns. Keys are namespaced by module
    so two modules cannot collide, and everything non-conforming is dropped silently
    rather than raising — a bad module degrades to contributing nothing.
    """
    if not isinstance(raw, dict):
        return {}

    out: dict[str, Any] = {}
    for key, value in raw.items():
        if len(out) >= MAX_SIGNALS_PER_PROSPECT:
            break
        if not isinstance(key, str) or not key.strip():
            continue
        if not isinstance(value, _PRIMITIVES):
            # Nested structures are dropped, not flattened: flattening invents key
            # names that no reviewer approved.
            continue
        if isinstance(value, str):
            value = value[:MAX_SIGNAL_LENGTH]
        out[f"{module_name}.{key.strip()}"] = value
    return out
