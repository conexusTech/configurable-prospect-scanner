"""Decision-maker search phase — the PRD's fourth scan phase.

Finds a named contact per prospect and fills AEO's five contact columns plus the
open `contacts_data` JSONB. Without it, `contact_title` / `contact_email` /
`contact_phone` / `contact_linkedin` stay empty and a salesperson gets a company with
no way in — the engine supplies only a `contact_name` scraped incidentally during
discovery, and often not even that.

**Contacts ride on the `scored` event, not their own.** AEO's `SCAN_EVENT_TYPES` has
no `contacts` member; the contact fields are declared on `ScanScoredItemDto`. So this
phase returns a per-prospect patch that the caller merges into scored items before
they are emitted.

Two judgement calls, both erring toward fewer false facts:

1. **Never invent an email.** Pattern-guessing (`first.last@domain`) produces
   plausible addresses that bounce, and a bounced first touch is worse than no
   touch. Only an address the model states it found is kept, and anything
   pattern-shaped is recorded as a `guess` in `contacts_data` instead of promoted
   to the column.
2. **A missing contact is empty, never a placeholder.** No "Unknown" / "N/A" — those
   read as data downstream and defeat any "needs enrichment" filter.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from aeo.phases._batching import (
    BATCH_OUTPUT_RULES,
    CONTACT_BATCH,
    chunk,
    numbered_input,
    reconcile,
)
from aeo.phases._concurrent import (
    DEFAULT_CALL_TIMEOUT_S,
    PHASE_RETRY_ATTEMPTS,
    concurrency_from,
    map_bounded,
)

# Column-backed fields; everything else the model returns goes to `contacts_data`.
CONTACT_COLUMNS = (
    "contact_name",
    "contact_title",
    "contact_email",
    "contact_phone",
    "contact_linkedin",
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

_PROMPT = """Identify the best decision-maker to contact at each organization below,
for the seller described.

ORGANIZATIONS — {count} of them, numbered. Report on every one.
{prospects}

WHAT THE SELLER OFFERS
{product}

TARGET JOB TITLES (in priority order)
{titles}

TARGET SENIORITY LEVELS
{seniorities}

THE SELLER'S CONTACT PREFERENCES
{preferences}

Use grounded public sources. **Do not guess or construct an email address**: if you
cannot find one stated publicly, leave it empty. Never invent a person.

OUTPUT
Return a JSON array of exactly {count} objects, one per organization:

{{
  "n": <the organization's number from the list above>,
  "company_name": "<its exact official name, copied from the list>",
  "contact_name": "", "contact_title": "", "contact_email": "",
  "contact_phone": "", "contact_linkedin": "", "source_url": "",
  "confidence": "high|medium|low"
}}

{rules}

Leave any field empty when you cannot support it. Return only the JSON array."""


def _as_lines(value: Any) -> str:
    if isinstance(value, list):
        items = [str(v).strip() for v in value if str(v).strip()]
        return "\n".join(f"- {i}" for i in items) if items else "(any)"
    text = str(value).strip() if value is not None else ""
    return f"- {text}" if text else "(any)"


def _clean(value: Any) -> str:
    """Normalise a model string, treating placeholder words as absent."""
    text = str(value or "").strip()
    if text.lower() in {"", "n/a", "na", "none", "unknown", "not found", "null"}:
        return ""
    return text


def _prospect_summary(prospect: dict[str, Any]) -> str:
    parts = [f"name: {prospect.get('company_name') or '(unknown)'}"]
    if prospect.get("city") or prospect.get("state"):
        parts.append(f"location: {prospect.get('city') or '?'}, {prospect.get('state') or '?'}")
    if prospect.get("website"):
        parts.append(f"website: {prospect['website']}")
    return "\n".join(parts)


def find_contacts(
    prospects: list[dict[str, Any]],
    *,
    contacts_config: dict[str, Any],
    product_description: str,
    provider: Callable[..., str],
    provider_config: dict[str, Any],
    parse_json_array: Callable[[str], list[dict[str, Any]]],
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Search per prospect. Returns `{prospect_id: patch}` for merging into `scored`.

    `contacts_config` is the config's `contacts` section **already resolved** — its
    `titles` and `seniorities` are R12-bound (to `decision_titles` and
    `decision_seniorities`), so they arrive as bindings and must be resolved first.
    """
    titles = _as_lines(contacts_config.get("titles"))
    seniorities = _as_lines(contacts_config.get("seniorities"))
    # ⚠️ Authored by CSB-built skills and read by NOTHING until 2026-08-12 — the third
    # authored-but-unread key found in one audit (`scoring.factors`, `validation.rules`,
    # this). An unread key is indistinguishable from a respected one at every surface
    # the operator sees.
    preferences = _as_lines(contacts_config.get("contact_preferences"))

    if emit:
        emit({"type": "phase_start", "phase": "contacts"})

    targets = [p for p in prospects if p.get("id")]

    # 🔴 **6 organizations per call, not one.** The bundling ruling §4
    # prices contact search separately from other single-signal phases — one group but
    # heavier per entity — so it batches at 6 rather than 8. On the reference run this
    # phase spent 50 grounded calls on 50 targets; batched it spends 9.
    #
    # ⚠️ It does NOT fuse with `enrichment`, and that is deliberate: contacts runs AFTER
    # scoring on a top-N cut while enrichment runs BEFORE it on validation survivors.
    # Different entity sets at different points, so §3 fusion does not apply — only §4
    # batching does.
    batches = chunk(targets, CONTACT_BATCH)

    def _search(batch: list[dict[str, Any]]) -> list[dict[str, Any] | None]:
        prompt = _PROMPT.format(
            count=len(batch),
            prospects=numbered_input(batch, _prospect_summary),
            product=product_description or "(not specified)",
            titles=titles,
            seniorities=seniorities,
            preferences=preferences,
            rules=BATCH_OUTPUT_RULES.format(name_key="company_name"),
        )
        raw = provider(
            prompt,
            model=provider_config.get("model"),
            temperature=provider_config.get("temperature", 0.1),
            # 1 attempt, not the config value — see PHASE_RETRY_ATTEMPTS.
            retry_attempts=PHASE_RETRY_ATTEMPTS,
            timeout_s=DEFAULT_CALL_TIMEOUT_S,
            phase="contacts",
        )
        return reconcile(batch, parse_json_array(raw) or [])

    # Bounded + timeout-enforced — the provider ignores `timeout_s`, so a sequential
    # loop has no upper bound. See aeo/phases/_concurrent.py.
    batch_results = map_bounded(
        batches,
        _search,
        max_concurrency=concurrency_from(provider_config),
        timeout_s=DEFAULT_CALL_TIMEOUT_S,
    )

    # Flatten back to one result per target, positionally. A failed batch yields None
    # from `map_bounded`; every target in it gets an empty patch rather than becoming a
    # missing key that reads as "never searched".
    found_results: list[dict[str, Any] | None] = []
    unmatched = 0
    for batch, matched in zip(batches, batch_results):
        rows = matched or [None] * len(batch)
        for row in rows:
            if not isinstance(row, dict):
                unmatched += 1
                found_results.append(None)
            else:
                found_results.append(row)

    patches: dict[str, dict[str, Any]] = {}
    found = 0
    for prospect, result in zip(targets, found_results):
        prospect_id = prospect["id"]
        result = result or {}
        patch: dict[str, Any] = {}
        for column in CONTACT_COLUMNS:
            value = _clean(result.get(column))
            if value:
                patch[column] = value

        # An address that does not look like an address is not one. Keep it as
        # evidence rather than promoting it to the column a sequencer will mail.
        email = patch.get("contact_email")
        rejected_email = None
        if email and not _EMAIL_RE.match(email):
            rejected_email = patch.pop("contact_email")

        extras = {
            k: _clean(v)
            for k, v in result.items()
            if k not in CONTACT_COLUMNS and _clean(v)
        }
        if rejected_email:
            extras["rejected_email"] = rejected_email
        if extras:
            patch["contacts_data"] = extras

        if patch.get("contact_name"):
            found += 1
        # Emitted even when empty: "searched and found nobody" is a different fact
        # from "never searched", and only the former should stop a retry.
        patches[prospect_id] = patch

    # §5 rule 5: "do not let a batch of 8 silently return 5." A target with no object
    # in the response searched and got nothing BACK, which is not the same fact as
    # searching and finding nobody — and only the latter should stop a retry.
    if unmatched and emit:
        emit(
            {
                "type": "contacts_unmatched",
                "prospects": unmatched,
                "of": len(targets),
                "batch_size": CONTACT_BATCH,
            }
        )

    if emit:
        emit({"type": "phase_complete", "phase": "contacts", "count": found})
    return patches


def merge_into_scored(
    scored: list[dict[str, Any]], patches: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Apply contact patches to scored items, keyed by `prospect_id`.

    The engine already sets `contact_name` from whatever discovery happened to
    scrape. A real search result overwrites it; an empty one leaves it alone, so a
    failed lookup never erases a name we already had.
    """
    by_id = {s.get("prospect_id"): s for s in scored}
    for prospect_id, patch in patches.items():
        target = by_id.get(prospect_id)
        if target is None or not patch:
            continue
        for key, value in patch.items():
            if key == "contacts_data":
                merged = {**(target.get("contacts_data") or {}), **value}
                target["contacts_data"] = merged
            elif value:
                target[key] = value
    return scored
