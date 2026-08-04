"""Run loaded custom modules over a prospect set.

Kept separate from the loader so the gate and the execution read as two things: one
decides *whether* generated code runs, the other bounds *what it can do to a scan*.
"""

from __future__ import annotations

from typing import Any

from aeo.modules.interface import CustomModule, sanitize_signals


def apply_modules(
    prospects: list[dict[str, Any]],
    modules: list[CustomModule],
    *,
    context: dict[str, Any],
    on_error: Any = None,
) -> dict[str, dict[str, Any]]:
    """Collect sanitized signals per prospect id. `{}` when there are no modules.

    Every call is individually guarded: generated code that raises on one prospect
    must not fail the phase, and must not prevent other modules from contributing.
    Signals are namespaced by module name, so two modules cannot overwrite each
    other's keys even if they choose the same one.
    """
    if not modules or not prospects:
        return {}

    collected: dict[str, dict[str, Any]] = {}
    for prospect in prospects:
        prospect_id = prospect.get("id")
        if not prospect_id:
            continue
        merged: dict[str, Any] = {}
        for module in modules:
            try:
                raw = module.signals(prospect, context=context)
            except Exception as exc:  # noqa: BLE001 — generated code
                if on_error:
                    on_error(getattr(module, "name", "<unknown>"), prospect_id, exc)
                continue
            merged.update(sanitize_signals(raw, module_name=module.name))
        if merged:
            collected[prospect_id] = merged
    return collected
