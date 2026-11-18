"""Decision-maker search phase — the PRD's fourth scan phase.

Finds up to three named decision-makers per prospect (PRD R-27.a; `contacts.max_people`,
default 3, clamped to 1-3) and fills AEO's five contact columns from the first of them,
plus the open `contacts_data` JSONB and a `contacts` list carrying every person. Without
it, `contact_title` / `contact_email` / `contact_phone` / `contact_linkedin` stay empty
and a salesperson gets a company with no way in — the engine supplies only a
`contact_name` scraped incidentally during discovery, and often not even that.

**Contacts ride on the `scored` event, not their own.** AEO's `SCAN_EVENT_TYPES` has
no `contacts` member; the contact fields are declared on `ScanScoredItemDto`. So this
phase returns a per-prospect patch that the caller merges into scored items before
they are emitted.

Four judgement calls, all erring toward fewer false facts, and all applied to EVERY
person, not only the first:

1. **Never invent an email.** Pattern-guessing (`first.last@domain`) produces
   plausible addresses that bounce, and a bounced first touch is worse than no
   touch. Only an address the model states it found is kept: one that does not parse,
   or whose local part is a template (`first.last`, `firstname.lastname`, ...), is
   recorded in `contacts_data` as evidence (`rejected_email` for the person in the
   columns, `rejected_emails` for everyone in the list) and never promoted.
2. **A missing contact is empty, never a placeholder.** No "Unknown" / "N/A" — those
   read as data downstream and defeat any "needs enrichment" filter. A value that is
   not a string is absent, never stringified. An entry with no real name is not a
   person, so it never enters the list; a role-only entry (a general office email or
   phone) can still fill the columns when nobody is named.
3. **A field over its limit is absent, never cut** (FIELD_LIMITS). One over-long column
   refuses the whole scored event at the gateway; a cut address is an invented one.
4. **An empty or failed search sends no `contacts` at all.** An empty list on the wire
   would read as "this business has nobody", erasing people an earlier run found.
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

#: Local parts that are a naming TEMPLATE, not a mailbox anybody stated. A model that
#: pattern-guesses writes exactly these, and they parse as addresses, so the shape
#: check above cannot catch them. Kept narrow on purpose: an ordinary
#: `jane.doe@` is indistinguishable from a guess by shape, and only the prompt's
#: rule stands between it and invention.
_TEMPLATE_LOCAL_PARTS = frozenset({
    "first.last", "firstname.lastname", "first_last", "firstname_lastname",
    "firstlast", "firstnamelastname", "first", "firstname", "last", "lastname",
    "flast", "f.last", "firstl", "first.l", "name", "fullname", "yourname",
})
_TEMPLATE_CHARS = frozenset("{}[]<>*")

#: People per business (PRD R-27.a). The ceiling is the gateway's: it stores at most
#: three, so asking for more pays for output nobody keeps.
DEFAULT_MAX_PEOPLE = 3
MAX_PEOPLE_CEILING = 3

#: Longest value kept per person field. 🔴 **A field over its limit is DROPPED, never
#: cut** — a cut email or URL is a fabricated one. The limits sit at or under what the
#: gateway accepts (`ScanScoredItemDto`: `@MaxLength(50)` on phone, 255 on name, title
#: and email, 500 on LinkedIn), because ONE over-long column refuses the whole scored
#: event and loses every prospect in it. An over-limit email is refused like any other
#: (kept as evidence); an over-limit name drops the whole person.
FIELD_LIMITS = {
    "contact_name": 200,
    "contact_title": 200,
    "contact_email": 254,
    "contact_phone": 50,
    "contact_linkedin": 500,
    "source_url": 500,
    "confidence": 10,
}

#: Every per-person field the phase reads; each is limited by FIELD_LIMITS.
_PERSON_FIELDS = (*CONTACT_COLUMNS, "source_url", "confidence")

#: Model entries read per business, at most. A runaway list is cut BEFORE cleaning, so
#: a model that returns hundreds of entries costs nothing beyond its own tokens.
_PEOPLE_READ_FACTOR = 4

#: Wire key on the scored item for each person's column-backed field. The other two
#: (`source_url`, `confidence`) keep their own names. snake_case, like every key on
#: `ScanScoredItemDto` (measured 2026-11-18: `prospect_id`, `contact_name`,
#: `contacts_data`, ... and `post_event` sends keys verbatim).
_PERSON_WIRE_KEYS = (
    ("contact_name", "name"),
    ("contact_title", "title"),
    ("contact_email", "email"),
    ("contact_phone", "phone"),
    ("contact_linkedin", "linkedin"),
    ("source_url", "source_url"),
    ("confidence", "confidence"),
)

_PROMPT = """Identify up to {max_people} {makers} to contact at each organization below,
for the seller described. List them best match first: the person whose title best
matches the target job titles comes first.

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

Use grounded public sources. Never invent a person: every person you list is a real,
named individual a source names at that organization.
**Do not guess or construct an email address**: if you cannot find one stated publicly
for that person, leave it empty. Both rules hold for every person you list, not only
the first.

If you find no named person at an organization, you may instead return ONE entry with an
empty "contact_name" carrying the organization's general office email, phone, or the
title of the role to ask for, as a source states it. Never put a role, a department or a
placeholder in "contact_name".

OUTPUT
Return a JSON array of exactly {count} objects, one per organization:

{{
  "n": <the organization's number from the list above>,
  "company_name": "<its exact official name, copied from the list>",
  "people": [
    {{
      "contact_name": "", "contact_title": "", "contact_email": "",
      "contact_phone": "", "contact_linkedin": "", "source_url": "",
      "confidence": "high|medium|low"
    }}
  ]
}}

"people" holds at most {max_people} entries, each a different person, best match first,
each with the source that names them. It is an empty list when you found nobody and no
general office contact either.

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


def max_people_from(contacts_config: dict[str, Any]) -> int:
    """`contacts.max_people`, default 3, clamped to 1-3. Anything unreadable is the default."""
    raw = contacts_config.get("max_people", DEFAULT_MAX_PEOPLE)
    # `True` is an int in Python; an authored boolean is not a count.
    if raw is None or isinstance(raw, bool):
        return DEFAULT_MAX_PEOPLE
    try:
        wanted = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_PEOPLE
    return max(1, min(MAX_PEOPLE_CEILING, wanted))


def _field(value: Any) -> str:
    """A person field from the model: a string, or absent.

    A dict, list, bool or number is absent rather than stringified — `str({...})`
    would otherwise become a person named `{'first': 'Jane'}`.
    """
    if not isinstance(value, str):
        return ""
    return _clean(value)


def _is_email(address: str) -> bool:
    """An address somebody stated, not one that fails to parse or names a template."""
    if not _EMAIL_RE.match(address):
        return False
    if any(ch in _TEMPLATE_CHARS for ch in address):
        return False
    return address.split("@", 1)[0].lower() not in _TEMPLATE_LOCAL_PARTS


def _name_key(name: str) -> str:
    return "".join(ch for ch in name.casefold() if ch.isalnum())


def _people_in(result: dict[str, Any], max_people: int) -> list[dict[str, Any]]:
    """The raw person objects in one organization's result, in the model's order.

    The new shape carries them under `people`, read no further than
    `max_people * _PEOPLE_READ_FACTOR` entries. A result in the old single-contact
    shape (the five fields at top level) is read as one person, so a model that ignores
    the new shape still yields its one contact rather than nothing.
    """
    people = result.get("people")
    if isinstance(people, list):
        bounded = [p for p in people[: max_people * _PEOPLE_READ_FACTOR] if isinstance(p, dict)]
        if bounded:
            return bounded
    if any(_field(result.get(c)) for c in CONTACT_COLUMNS):
        return [result]
    return []


def _person_fields(raw: dict[str, Any]) -> tuple[dict[str, str], str | None] | None:
    """One entry's fields, cleaned and limited; and the email refused, if any.

    `None` when the entry is not usable at all: a name over its limit drops the whole
    person, since a cut name is a different person.
    """
    fields: dict[str, str] = {}
    rejected = None
    for key in _PERSON_FIELDS:
        value = _field(raw.get(key))
        if not value:
            continue
        if len(value) > FIELD_LIMITS[key]:
            if key == "contact_name":
                return None
            if key == "contact_email":
                rejected = value
            continue  # absent, never cut
        fields[key] = value
    email = fields.get("contact_email")
    if email and not _is_email(email):
        rejected = fields.pop("contact_email")
    return fields, rejected


def _wire_person(fields: dict[str, str]) -> dict[str, str]:
    """One `contacts` entry for the scored item. Absent fields are omitted, never `""`."""
    return {wire: fields[src] for src, wire in _PERSON_WIRE_KEYS if fields.get(src)}


def _patch_for(result: dict[str, Any], max_people: int) -> dict[str, Any]:
    """The scored-item patch for one organization's result.

    🔑 **The five columns are the first person in `contacts`**, so the two never
    disagree about who comes first. When nobody in the result has a real name, the
    columns take the first entry as it stands — a role-only contact such as a general
    office email, which the prompt allows — and no `contacts` list is sent at all.
    """
    cleaned = [
        (raw, *person)
        for raw in _people_in(result, max_people)
        if (person := _person_fields(raw)) is not None
    ]

    listed: list[tuple[dict[str, Any], dict[str, str], str | None]] = []
    seen: set[str] = set()
    for raw, fields, rejected in cleaned:
        name = fields.get("contact_name")
        if not name:
            continue  # a role-only entry is not a person: columns only, never listed
        key = _name_key(name)
        if not key or key in seen:
            # No letters or digits is not a name; the same person twice collapses to
            # the first mention.
            continue
        seen.add(key)
        listed.append((raw, fields, rejected))
        if len(listed) == max_people:
            break

    column_person = listed[0] if listed else (cleaned[0] if cleaned else None)

    patch: dict[str, Any] = {}
    # The organization-level evidence the model returned (`n`, `company_name`, ...),
    # as the single-contact phase always kept it.
    extras: dict[str, Any] = {
        k: _clean(v)
        for k, v in result.items()
        if k not in _PERSON_FIELDS and k != "people" and _clean(v)
    }
    if column_person is not None:
        raw, fields, rejected = column_person
        patch.update({k: v for k, v in fields.items() if k in CONTACT_COLUMNS})
        # Source and confidence, as limited above — never the raw value.
        extras.update({k: v for k, v in fields.items() if k not in CONTACT_COLUMNS})
        if raw is not result:
            extras.update(
                {k: _field(v) for k, v in raw.items()
                 if k not in _PERSON_FIELDS and _field(v)}
            )
        if rejected:
            # An address that does not look like an address is not one. Keep it as
            # evidence rather than promoting it to the column a sequencer will mail.
            extras["rejected_email"] = rejected

    # Every listed person's refused address, by name — `rejected_email` above stays
    # what it always was (the column person's).
    refused = [
        {"name": fields["contact_name"], "email": rejected}
        for _raw, fields, rejected in listed
        if rejected
    ]
    if refused:
        extras["rejected_emails"] = refused
    if extras:
        patch["contacts_data"] = extras

    # 🔴 Sent only when somebody was found. An empty list would read downstream as
    # "this business has nobody" and erase people an earlier run found.
    contacts = [_wire_person(fields) for _raw, fields, _rej in listed]
    if contacts:
        patch["contacts"] = contacts
    return patch


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
    max_people = max_people_from(contacts_config)

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
            max_people=max_people,
            makers="decision-makers" if max_people > 1 else "decision-maker",
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
        patch = _patch_for(result or {}, max_people)
        if patch.get("contact_name"):
            found += 1
        # Emitted even when empty: "searched and found nobody" is a different fact
        # from "never searched", and only the former should stop a retry.
        patches[prospect["id"]] = patch

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
    failed lookup never erases a name we already had. The same holds for `contacts`:
    a patch carries it only when somebody was found, and an empty value is never set.
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
