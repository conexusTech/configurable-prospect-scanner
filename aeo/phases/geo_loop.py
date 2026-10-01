"""Search → verify → re-search, until enough results are genuinely in area.

**Why a loop.** Prompting the model to respect geography is not a reliable control:
the identical prompt, image and config returned Austin firms twice standalone and
Dallas firms twice in-container. Same everything, opposite outcomes. So geography
cannot be an instruction — it has to be a **loop condition**, checked against
evidence and re-run until satisfied or exhausted.

## Three constraints from the surrounding system, none of them negotiable

1. **A prospect's stored address can never be corrected by the prospects write.** AEO
   writes prospects `ON CONFLICT ("id") DO NOTHING`, and the engine emits them *during*
   discovery — before verification can run. So the first write wins permanently.
   Verification therefore decides **keep or reject**, and records the address it actually
   found in the rejection's `validation_data`, rather than pretending it can fix the column.
   For a KEPT lead the verified address now travels on the *scored* callback instead
   (`aeo-backend` `fca4d0e`): `_apply_verified` sets it on the lead, and the runner carries
   it onto the scored item.
2. **Re-discovery must exclude what we have already seen**, or every round returns
   the same firms. The engine already has the mechanism: `seed_firms` renders as
   *"do NOT return these; find ADDITIONAL firms"*. Each round appends the names seen
   so far.
3. **A repeat find is free.** Prospect ids are `uuid5(namespace, f"{scan_run_id}:{name}")`,
   so the same firm discovered twice is the same id — deduped by construction, and
   `DO NOTHING` means no duplicate row.

## Cost, stated plainly because it is not small

Verification is **one grounded call per candidate**, and rounds multiply it. A 3-result
sweep over 2 rounds is up to 2 discovery sweeps + 6 verification calls, where the
un-verified version was 2 sweeps. That is the price of geography being a fact rather
than a hope.

Not paid down by trusting the model's own location claim — see `needs_verification`,
where doing exactly that left a hole big enough to drive the original bug through. What
is bounded instead: the loop stops the moment the target count is met, a round that
discovers nothing new ends it (an exhausted search is not retried into a bill), and
`trust_reported=True` is available as an explicit, documented trade.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from aeo.phases._concurrent import (
    DEFAULT_CALL_TIMEOUT_S,
    PHASE_RETRY_ATTEMPTS,
    concurrency_from,
    map_bounded,
)
from aeo.phases.geo_filter import (
    IN_AREA,
    OUT_OF_AREA,
    STRICTNESS_METRO,
    TargetArea,
    classify_prospect,
)
from aeo.us_states import normalize_city, normalize_state, normalize_zip

#: Rounds of discovery. 2 by default: one search, one corrective re-search. A third
#: round rarely finds what two did not, and each one costs a full discovery sweep.
DEFAULT_MAX_ROUNDS = 2

_VERIFY_PROMPT = """Where is this organization actually located?

ORGANIZATION
{name}{hint}

Report the city, state and ZIP code of its OWN primary business address — not a
branch, not a project site, not a market it serves. If you cannot establish the
address from public sources, say so rather than guessing.

Return a JSON array with exactly one object:
[{{"city": "", "state": "", "zip_code": "", "confidence": "high|medium|low",
   "source_url": ""}}]

Leave a field empty when you cannot support it. Return only the JSON array."""


def _hint(prospect: dict[str, Any]) -> str:
    bits = [
        str(prospect.get(k))
        for k in ("city", "state", "website")
        if prospect.get(k)
    ]
    return f"\nreported as: {', '.join(bits)}" if bits else ""


def verify_locations(
    prospects: list[dict[str, Any]],
    *,
    provider: Callable[..., str],
    provider_config: dict[str, Any],
    parse_json_array: Callable[[str], list[dict[str, Any]]],
    log: Callable[[str], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Establish each prospect's real address. Returns `{id: {city, state, zip_code, …}}`.

    A failed or unparseable verification yields no entry, and the caller then falls
    back to what discovery reported — an unverifiable prospect must not be treated as
    out of area, for the same reason validation keeps its unjudged rows.
    """
    targets = [p for p in prospects if p.get("id")]

    def _verify(prospect: dict[str, Any]) -> dict[str, Any]:
        raw = provider(
            _VERIFY_PROMPT.format(
                name=prospect.get("company_name") or "(unknown)", hint=_hint(prospect)
            ),
            model=provider_config.get("model"),
            temperature=provider_config.get("temperature", 0.1),
            retry_attempts=PHASE_RETRY_ATTEMPTS,
            timeout_s=DEFAULT_CALL_TIMEOUT_S,
            phase="geography",
        )
        parsed = parse_json_array(raw)
        return parsed[0] if parsed else {}

    results = map_bounded(
        targets,
        _verify,
        max_concurrency=concurrency_from(provider_config),
        timeout_s=DEFAULT_CALL_TIMEOUT_S,
        log=log,
        label='address verification',
    )

    verified: dict[str, dict[str, Any]] = {}
    for prospect, result in zip(targets, results):
        if isinstance(result, dict) and (result.get("city") or result.get("zip_code")):
            verified[prospect["id"]] = result
    return verified


def needs_verification(
    prospect: dict[str, Any],
    area: TargetArea,
    *,
    strictness: str = STRICTNESS_METRO,
    trust_reported: bool = False,
) -> bool:
    """Whether this candidate's location needs establishing independently.

    **Defaults to verifying everything, including prospects that claim to be in
    area.** An earlier version skipped those as an optimisation — "it would spend a
    call to learn nothing" — and a test written against it immediately exposed the
    hole: the reported city comes from *the same model whose geography we do not
    trust*. A model inclined to return Dallas firms can label them "Austin" and walk
    straight past enforcement. Verifying only the candidates that admit being out of
    area catches the honest mistakes and misses the confident ones, which is the
    wrong half.

    `trust_reported=True` restores the cheaper behaviour for runs where discovery is
    known to be honest about location and the halved call count matters. It is an
    explicit choice, not a default.
    """
    if not trust_reported:
        return True
    return classify_prospect(prospect, area, strictness=strictness) != IN_AREA


def _merge_verified(prospect: dict[str, Any], verified: dict[str, Any]) -> dict[str, Any]:
    """A copy of the prospect carrying the verified location, for the keep/reject verdict.

    Deliberately does NOT mutate the original. ⚠️ What the verdict sees is the verified
    values layered OVER discovery's: any part the verifier left empty falls back to what
    discovery reported (its ZIP, typically). So a lead can be kept on discovery's in-area
    ZIP although the verified city is elsewhere -- which is why `_apply_verified` does not
    trust this copy and classifies the verified location on its own.
    """
    merged = dict(prospect)
    for key in ("city", "state", "zip_code"):
        if verified.get(key):
            merged[key] = verified[key]
    return merged


def _place(value: Any) -> str | None:
    """A city or state for comparison: trimmed, case-folded, blank as None."""
    return " ".join(str(value or "").split()).lower() or None


def _apply_verified(
    prospect: dict[str, Any],
    evidence: dict[str, Any],
    area: TargetArea,
    *,
    strictness: str = STRICTNESS_METRO,
) -> bool:
    """Make a KEPT lead's location the verified one. True when it was relocated.

    Relocates only when (a) the verified state normalises to one of the 51 US codes the
    gateway accepts and (b) the location this writes -- by itself, with no fallback to
    discovery's values -- classifies IN_AREA under the same area and strictness. Otherwise
    discovery's location stands and nothing is marked, so "verified" keeps meaning
    something on the wire. (b) closes a hole: the keep/reject verdict sees the verified
    values layered over discovery's, so a lead kept on discovery's in-area ZIP could be
    stored as a verified city that is outside the area.

    City and ZIP are normalised too, and an unusable one becomes absent rather than being
    kept from discovery -- a Charlotte state beside an Austin city would be a location
    nobody reported. When the place changes (city or state, case/whitespace-insensitive,
    as the gateway compares them) the street address is cleared: it belonged to the old
    place. Same place keeps its street.

    ⚠️ Written to `_internal` as well as the record: the scorer reads `_internal`, so
    setting the record alone would store one location and score on another. `_internal`
    takes `""` for an absent part (its own empty form -- `_parse_locality` never writes a
    None there and region scoring does `str(lead.get("city"))`), the record takes `None`.
    """
    state = normalize_state(evidence.get("state"))
    if state is None:
        return False
    location = {
        "city": normalize_city(evidence.get("city")),
        "state": state,
        "zip_code": normalize_zip(evidence.get("zip_code")),
    }
    # A bare state is not a location: the gateway would null the city and ZIP, and the street
    # below would be wiped for nothing. Same rule as `verify_locations`, but applied AFTER
    # normalising -- a NUL-laden city or a fullwidth ZIP is truthy raw and empty here.
    if location["city"] is None and location["zip_code"] is None:
        return False
    if classify_prospect(location, area, strictness=strictness) != IN_AREA:
        return False

    moved = _place(prospect.get("city")) != _place(location["city"]) or _place(
        prospect.get("state")
    ) != _place(location["state"])
    internal = prospect.get("_internal")
    for key, value in location.items():
        prospect[key] = value
        if isinstance(internal, dict):
            internal[key] = value or ""
    if moved:
        prospect["address"] = None
        if isinstance(internal, dict):
            internal["location_address"] = ""
    prospect["verified_location"] = dict(location)
    return True


def _with_exclusions(
    tool_context: dict[str, Any], seen_names: list[str]
) -> dict[str, Any]:
    """A context whose every source excludes the names already seen.

    Uses the engine's own `seed_firms` channel, which renders as "do NOT return these;
    find ADDITIONAL firms" — so re-discovery explores instead of repeating.
    """
    out = copy.deepcopy(tool_context)
    for source in (out.get("sources") or {}).values():
        if isinstance(source, dict):
            existing = list(source.get("seed_firms") or [])
            source["seed_firms"] = existing + [n for n in seen_names if n not in existing]
    return out


def discover_in_area(
    *,
    tool_context: dict[str, Any],
    area: TargetArea,
    target_count: int,
    discover: Callable[[dict[str, Any]], list[dict[str, Any]]],
    provider: Callable[..., str],
    provider_config: dict[str, Any],
    parse_json_array: Callable[[str], list[dict[str, Any]]],
    strictness: str = STRICTNESS_METRO,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
    trust_reported: bool = False,
    log: Callable[[str], None] = lambda _: None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run rounds of discovery until `target_count` in-area prospects exist.

    Returns `(in_area, rejections)` where rejections are validation-shaped entries
    carrying the address that was actually found.

    `discover` is injected rather than imported so this is testable without the
    engine, and so the caller keeps ownership of emitting the engine's own events.
    """
    in_area: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_names: list[str] = []

    for round_no in range(1, max(max_rounds, 1) + 1):
        context = tool_context if round_no == 1 else _with_exclusions(tool_context, seen_names)
        found = discover(context)

        fresh = [p for p in found if p.get("id") and p["id"] not in seen_ids]
        if not fresh:
            # Nothing new — the search is exhausted for this geography. Retrying
            # would spend a sweep to learn the same thing.
            log(f"round {round_no}: no new candidates — stopping")
            break
        for p in fresh:
            seen_ids.add(p["id"])
            if p.get("company_name"):
                seen_names.append(str(p["company_name"]))

        # Only unconfirmed candidates cost a verification call.
        unconfirmed = [
            p
            for p in fresh
            if needs_verification(
                p, area, strictness=strictness, trust_reported=trust_reported
            )
        ]
        confirmed_immediately = [p for p in fresh if p not in unconfirmed]
        in_area.extend(confirmed_immediately)

        verified = (
            verify_locations(
                unconfirmed,
                provider=provider,
                provider_config=provider_config,
                parse_json_array=parse_json_array,
                # The round log only prints once this returns, so without a
                # heartbeat here a long verification pass is indistinguishable from
                # a hang — see `map_bounded`.
                log=log,
            )
            if unconfirmed
            else {}
        )

        relocated = 0
        moved_state = 0
        for prospect in unconfirmed:
            evidence = verified.get(prospect["id"])
            if evidence is None:
                # Unverifiable — keep, same rule as validation's unjudged rows.
                in_area.append(prospect)
                continue
            verdict = classify_prospect(
                _merge_verified(prospect, evidence), area, strictness=strictness
            )
            if verdict == OUT_OF_AREA:
                where = ", ".join(
                    str(evidence.get(k)) for k in ("city", "state") if evidence.get(k)
                ) or "an address that could not be placed"
                rejections.append(
                    {
                        "prospect_id": prospect["id"],
                        "validation_data": {
                            "validated": False,
                            "signals_found": [],
                            "disqualifiers_hit": ["outside the target geography"],
                            "reasoning": (
                                f"Address verified as {where}, outside the scan's "
                                f"target area ({area.describe()})."
                            ),
                            # The column keeps discovery's value — AEO will not
                            # overwrite it — so the true address lives here.
                            "verified_location": {
                                k: evidence.get(k)
                                for k in ("city", "state", "zip_code", "confidence", "source_url")
                                if evidence.get(k)
                            },
                        },
                    }
                )
            else:
                # Normalised, so "Tex." / "texas" vs "TX" is not a move; a discovery state
                # that is not a US state at all counts as none.
                had_state = normalize_state(prospect.get("state"))
                if _apply_verified(prospect, evidence, area, strictness=strictness):
                    relocated += 1
                    # A different STATE than discovery had is the case worth reading in
                    # the log: it is the one that changes which gates a lead passes.
                    if had_state and had_state != prospect["state"]:
                        moved_state += 1
                in_area.append(prospect)

        log(
            f"round {round_no}: {len(fresh)} new, {len(unconfirmed)} verified, "
            f"{len(in_area)}/{target_count} in area, "
            f"{relocated} relocated ({moved_state} to a different state)"
        )
        if len(in_area) >= target_count:
            break

    return in_area, rejections
