"""Shared primitives for batched grounded calls — the PO's bundling ruling §4 and §5.

**One home for the guardrails, deliberately.** §5's five rules apply to *every* batched
prompt, and the failure mode they prevent is the same everywhere: the model stops treating
entities as distinct records. Implementing them per phase is how three of four call sites
end up deciding the same question by not asking it — the shape of a defect this codebase
hit the same week, in a different repo, over a different predicate.

A phase supplies what only it knows — how to summarise its entity, what its output object
looks like — and takes the counting, numbering and reconciliation from here.
"""

from __future__ import annotations

from typing import Any, Callable

#: Entities per call, by how many signal groups share the prompt (§4).
#:
#: 🔴 **5 is the only size measured for a multi-group fused prompt.** Raising it on the
#: assumption that bigger is cheaper is the failure §4 names explicitly: an oversized
#: batch degrades SILENTLY, thinning results per entity and dropping entities from the
#: response, and both look like "the data wasn't out there". §10 gives the procedure for
#: moving it — 5 / 8 / 10 over ~20 known entities, judged on data points per entity,
#: exact-name match rate, and entities silently dropped.
_BATCH_BY_GROUPS = {1: 8, 2: 6}
_BATCH_MULTI_GROUP = 5

#: §4 names contact/decision-maker search separately: it is a single-signal phase but a
#: heavier one per entity, so it sits at 6 rather than the single-group 8.
CONTACT_BATCH = 6


def batch_size_for(group_count: int) -> int:
    """Entities per call for a phase fusing `group_count` signal groups."""
    return _BATCH_BY_GROUPS.get(max(1, int(group_count)), _BATCH_MULTI_GROUP)


def chunk(items: list[Any], size: int) -> list[list[Any]]:
    """Split into batches of at most `size`, preserving order."""
    step = max(1, int(size))
    return [items[i : i + step] for i in range(0, len(items), step)]


def numbered_input(
    batch: list[dict[str, Any]],
    summarise: Callable[[dict[str, Any]], str],
) -> str:
    """The numbered input list required by §5 rule 1.

    ⚠️ Disambiguating context — location, website, how the entity was found — goes on the
    INPUT side only. §5 rule 2 forbids it echoing back into the name field, and a
    production skill shipped `"Hall County (County; Hall County, GA)"` in a name field
    without that rule, breaking every downstream name match until it was cleaned by hand.
    """
    sep = "; "
    return chr(10).join(
        f"{i}. {summarise(entity).replace(chr(10), sep)}"
        for i, entity in enumerate(batch, 1)
    )


def _normalise(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def reconcile(
    batch: list[dict[str, Any]],
    parsed: list[Any],
    *,
    name_key: str = "company_name",
) -> list[dict[str, Any] | None]:
    """Map each input entity to its returned object. §5 rule 5.

    🔴 **Order is the declared contract, but it is not trusted blindly.** §5 rule 3 says
    one object per entity in input order, so position is the primary key — but only when
    the model returned the right number of objects. Anything else falls back to matching
    on name, then on the model's own `n` index, and a miss is reported by the caller
    rather than silently becoming "found nothing": *"do not let a batch of 8 silently
    return 5."*

    Returns a list positionally aligned to `batch`, `None` where nothing matched.
    """
    objects = [o for o in parsed if isinstance(o, dict)]
    if len(objects) == len(batch):
        # The contract held; position wins.
        return list(objects)

    out: list[dict[str, Any] | None] = [None] * len(batch)
    by_name: dict[str, dict[str, Any]] = {}
    for obj in objects:
        key = _normalise(obj.get(name_key))
        if key and key not in by_name:
            by_name[key] = obj

    for i, entity in enumerate(batch):
        hit = by_name.get(_normalise(entity.get("company_name")))
        if hit is not None:
            out[i] = hit
            continue
        # `n` is the model's own index into the numbered list — usable when the name came
        # back annotated despite rule 2.
        for obj in objects:
            try:
                if int(obj.get("n")) == i + 1:
                    out[i] = obj
                    break
            except (TypeError, ValueError):
                continue
    return out


#: The output contract every batched prompt states verbatim. §5 rules 2, 3 and 4.
#:
#: Kept as one string so the three rules cannot drift apart between phases — the whole
#: reason this module exists.
BATCH_OUTPUT_RULES = """Rules, all mandatory:
- "{name_key}" contains the exact official name and NOTHING else. No parenthetical
  annotation, no entity-type suffix, no echo of the input line's location or context.
- Return an object for every entry in the numbered list, IN THE SAME ORDER.
- Return an object even for entries you found nothing for.
- A field or list with no findings is EMPTY, never a missing key and never null.
- Do not merge findings across entries. A fact about entry 3 belongs only to entry 3."""
