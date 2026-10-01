"""Turn an org's free-text markets into the US states a target-market gate admits.

**Why this exists.** A gated skill can bind `scoring.gate.target_market.allowed_states` to the
org's `home_markets`. Those are free text -- "North Carolina", "Concord, NC", "Southeast US",
"Atlanta" -- and `gated_score.in_target_market` compares the WHOLE lead state to the WHOLE entry,
expanding only through the skill's own `state_aliases`. So "Concord, NC" could never match a lead
in NC, and a skill without full aliases could not match a state name at all.

**What resolves, and nothing more.** An entry places a state only where its own text names one: a
state code or name ("NC", "north carolina"), or "City, ST" / "City, State" where the part after the
LAST comma is a state. There is no city table, no region map and no guessing -- a bare "Portland"
is two states and "Southeast US" is a judgement, so each contributes nothing and is REPORTED, so a
market list that places nothing is visible rather than a silent narrowing.

**The original entries are kept**, beside the codes. The existing whole-string and alias matching
therefore behaves exactly as before; resolution only ever ADDS states.
"""

from __future__ import annotations

from typing import Any

from aeo.us_states import normalize_state

#: The only binding resolved here. Any other shape of `allowed_states` is the skill's own.
HOME_MARKETS_BINDING = {"context_ref": "home_markets"}


def _state_of(entry: str) -> str | None:
    """The state a single entry names, or None. Never guesses."""
    code = normalize_state(entry)
    if code:
        return code
    if "," in entry:
        return normalize_state(entry.rsplit(",", 1)[1])
    return None


def resolve_market_states(entries: Any) -> tuple[list[str], list[str]]:
    """(sorted unique state codes, entries that name no state in their original order).

    Non-strings are ignored: they are neither resolved nor reported.
    """
    if not isinstance(entries, (list, tuple)):
        return [], []
    codes: set[str] = set()
    unresolved: list[str] = []
    for entry in entries:
        # A non-string cannot name a state and is not worth reporting either; the engine's own
        # market flattening (`str(v)`) never sees one from the backend's text lists.
        if not isinstance(entry, str):
            continue
        code = _state_of(entry)
        if code:
            codes.add(code)
        else:
            unresolved.append(entry)
    return sorted(codes), unresolved


def _entries(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _includes_secondary(geography: dict[str, Any]) -> bool:
    """Whether the org's scope takes in its secondary markets.

    The SAME test, with the same normalisation, as `phases.zip_discovery.target_markets`, so the
    gate and the search can never disagree about which markets the org asked for. An unrecognised
    or absent scope is home only.
    """
    return str(geography.get("include_scope") or "").strip().upper() == "HOME_SECONDARY"


def apply_market_binding(
    raw_config: Any, resolved_config: Any, geography: Any
) -> list[str]:
    """Widen the RESOLVED config's `allowed_states` by what the org's markets name.

    Acts only when the RAW config's `allowed_states` is exactly the `home_markets` binding AND
    the org's `home_markets` is a list; any other shape (a literal list, absent, another binding,
    a `{state: [cities]}` dict that the engine flattens itself) is left untouched and returns [].
    Sets the resolved list to the original home entries, then their codes, then -- only when the
    org's `include_scope` is HOME_SECONDARY -- the codes of `secondary_markets`, de-duplicated in
    order. Returns the entries that named no state, for the caller to log.
    """
    if not isinstance(raw_config, dict) or not isinstance(resolved_config, dict):
        return []
    if not isinstance(geography, dict):
        return []
    raw_target = _target_market(raw_config)
    resolved_target = _target_market(resolved_config)
    if raw_target is None or resolved_target is None:
        return []
    if raw_target.get("allowed_states") != HOME_MARKETS_BINDING:
        return []

    home = geography.get("home_markets")
    if not isinstance(home, list):
        return []
    home_codes, unresolved = resolve_market_states(home)
    # The originals are kept AS THEY ARE, non-strings included, so the existing matching is
    # unchanged; a non-string simply resolves to nothing and is not reported.
    allowed: list[Any] = list(home) + home_codes

    if _includes_secondary(geography):
        secondary_codes, secondary_unresolved = resolve_market_states(
            _entries(geography.get("secondary_markets"))
        )
        allowed.extend(secondary_codes)
        unresolved.extend(secondary_unresolved)

    # De-duplicate strings in order; a non-string (unhashable, possibly) is kept where it stands.
    seen: set[str] = set()
    deduped: list[Any] = []
    for entry in allowed:
        if isinstance(entry, str):
            if entry in seen:
                continue
            seen.add(entry)
        deduped.append(entry)
    resolved_target["allowed_states"] = deduped
    return unresolved


def _target_market(config: dict[str, Any]) -> dict[str, Any] | None:
    node: Any = config
    for key in ("scoring", "gate", "target_market"):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node if isinstance(node, dict) else None
