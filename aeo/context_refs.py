"""Resolve `{"context_ref": "<key>"}` bindings against an org's runtime context.

**This is R12 personalization, and it is the scanner's job.** The ownership split
is explicit: the builder authors an *org-agnostic* config that binds org-specific
positions to well-known context keys, and the scanner "resolves well-known context
fields per org at scan time". Without this module a binding reaches the engine as a
literal `{"context_ref": "home_markets"}` dict where a list of markets belongs —
so the engine searches for nothing useful and reports success. It is the mechanism
that makes one skill serve many orgs, which is the whole premise of the catalog
being a library rather than a pile of single-tenant configs.

The vocabulary is the 13 keys published in aeo-backend's
`src/backend/skills/config/context-field-keys.json`. The mapping below was read off
a live `GET /runtime/organizations/{id}/context` response, not inferred from the
contract's `brief_path` values — those are paths into the *design brief*, which is a
different document with camelCase keys.
"""

from __future__ import annotations

from typing import Any

#: Published key → dotted path in the runtime context.
#:
#: ⚠️ **`decision_seniorities` is the ONE key that is not name-derivable**: it lives
#: at `decision_makers.seniorities`, NOT `decision_makers.decision_seniorities`. The
#: other twelve match their published names exactly, which makes this the single
#: place a reasonable person would guess wrong — and guessing wrong yields an empty
#: seniority list, so contact search silently widens to every seniority.
#:
#: This mirrors the same hazard on the authoring side, where `contacts.titles` must
#: bind to `decision_titles` and `contacts.seniorities` to `decision_seniorities`.
#: Two layers, one irregular field.
CONTEXT_PATHS: dict[str, str] = {
    "home_markets": "geography.home_markets",
    "secondary_markets": "geography.secondary_markets",
    "excluded_markets": "geography.excluded_markets",
    "include_scope": "geography.include_scope",
    "decision_titles": "decision_makers.decision_titles",
    "decision_seniorities": "decision_makers.seniorities",  # ⚠️ not name-derivable
    "icp_attributes": "icp.icp_attributes",
    "in_market_signals": "icp.in_market_signals",
    "disqualifiers": "icp.disqualifiers",
    "lookalike_sources": "icp.lookalike_sources",
    "lead_type": "lead_type",
    "industry": "organization.industry",
    "competitors": "organization.competitors",
}


class UnresolvedRefError(ValueError):
    """A binding names a key outside the published vocabulary.

    Raised rather than skipped. An unknown ref resolves to nothing at scan time,
    which silently narrows or widens targeting instead of failing — aeo-backend's
    R12 lint rejects these at finalize for exactly that reason, so a config
    reaching us with one means the lint was bypassed and we should not paper over it.
    """


def _dig(context: dict[str, Any], dotted: str) -> Any:
    node: Any = context
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _is_binding(value: Any) -> bool:
    return isinstance(value, dict) and "context_ref" in value


def _empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, dict, str)):
        return len(value) == 0
    return False


def resolve_ref(binding: dict[str, Any], context: dict[str, Any]) -> Any:
    """Resolve one binding. Falls back to its `default` when the org has no value.

    A `default` is the only position a literal is permitted in an org-specific
    field, per the config contract — it exists precisely so a skill still runs for
    an org that never filled in that part of onboarding.
    """
    key = binding.get("context_ref")
    if not isinstance(key, str) or key not in CONTEXT_PATHS:
        raise UnresolvedRefError(
            f"Unknown context field {key!r}. Must be one of the 13 published keys: "
            f"{', '.join(sorted(CONTEXT_PATHS))}."
        )
    value = _dig(context, CONTEXT_PATHS[key])
    if _empty(value):
        # `default` may itself legitimately be absent — an unset optional field.
        return binding.get("default")
    return value


def resolve(node: Any, context: dict[str, Any]) -> Any:
    """Deep-copy `node`, replacing every binding with its resolved value.

    Walks the whole document rather than a list of known positions: the builder may
    bind fields nobody enumerated, and a binding left unresolved anywhere is the
    same silent defect wherever it appears.
    """
    if _is_binding(node):
        return resolve_ref(node, context)
    if isinstance(node, dict):
        return {k: resolve(v, context) for k, v in node.items()}
    if isinstance(node, list):
        return [resolve(v, context) for v in node]
    return node
