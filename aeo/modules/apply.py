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


def merge_signals_into_scored(
    scored: list[dict[str, Any]], signals: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Attach collected signals to their scored item as `custom_signals`.

    Mirrors `contacts.merge_into_scored`: signals are computed from the prospect
    set but travel to AEO on the `scored` event, because that is the run's second
    per-prospect write. The engine emits `prospects` mid-discovery, before a
    module could contribute, so there is no earlier event to ride.

    Signals for a prospect that did not survive to scoring are dropped rather
    than emitted unattached — AEO keys the write on `prospect_id`, so an orphan
    would match no row and silently do nothing anyway. Better to not send it.
    """
    if not signals:
        return scored

    by_id = {s.get("prospect_id"): s for s in scored}
    for prospect_id, module_signals in signals.items():
        target = by_id.get(prospect_id)
        if target is None or not module_signals:
            continue
        # Merge rather than assign: keys are already namespaced per module, and
        # AEO merges again on its side (`custom_fields || custom_signals`), so a
        # replay is idempotent at both ends.
        target["custom_signals"] = {
            **(target.get("custom_signals") or {}),
            **module_signals,
        }
    return scored
