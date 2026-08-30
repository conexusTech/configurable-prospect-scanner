"""Canonical `signal_class` for a switching signal — the emitter-side fix for D2.

**The problem this replaces.** The 25-point switching-signal factor scored **0 on 17 of 24
leads** while all 24 carried a populated `signal_type`. The field was reachable and the
table worked; the vocabulary was the defect. The model emits free text — **54 distinct
phrasings across 72 signal rows** on run 741b7b3b alone — against six hand-guessed keys.
`workforce stress event` matched and `workforce_stress` did not, losing on an underscore.

**Two layers, and the order matters.** The real fix is that the scanner emits a canonical
class from a closed enum, the same pattern `ai_judgment` already uses for
`pipeline_status`. This module is the *transition*: it normalises what the model actually
writes into that enum, so the closed set exists before anything depends on it.

🔴 **This module classifies. It does not score.** The weights live in
`skills.config.scoring`, per skill, and an unrecognised phrasing is `None` here — the
caller decides what that is worth. Returning a class the config has no weight for is the
`{"max": N}` defect in a new shape.
"""

from __future__ import annotations

import re
from typing import Optional

#: The closed enum. Anything outside it is a bug in this file, not in a config.
SIGNAL_CLASSES: tuple[str, ...] = (
    "rfp_active",
    "broker_carrier_change",
    "dissatisfaction",
    "benefits_change",
    "corporate_event",
    "workforce_change",
    "leadership_change",
)

_PUNCT = re.compile(r"[^a-z0-9]+")


def normalize(raw: object) -> str:
    """Lowercase, underscores to spaces, punctuation stripped, whitespace collapsed.

    🔑 This single step is what closes the D2 gap that cost the most: `workforce stress`
    and `workforce_stress` were two different signals to the old table, and the second
    scored nothing. Same for `broker change` / `broker_relationship_change`.
    """
    return _PUNCT.sub(" ", str(raw or "").lower()).strip()


#: Ordered (class, patterns) rules. **Order is the whole design — first match wins.**
#:
#: Every rule below sits above another that would also match some real phrasing, and the
#: two placements that matter are both recorded decisions rather than accidents:
#:
#: 1. **dissatisfaction ABOVE benefits.** `low benefit satisfaction` contains "benefit",
#:    but it is a complaint about benefits, not a change to them. Ranked the other way it
#:    would score as a benefits change and the whole dissatisfaction signal would vanish
#:    into the wrong band.
#: 2. **benefits ABOVE leadership.** `key personnel change (benefits leadership)` is
#:    deliberately a benefits signal, not a leadership one — the phrase contains
#:    "benefits" *because the model identified the function*, so it marks a leadership
#:    change INSIDE the buying centre, which is a strong forward-looking broker trigger.
#:    Generic churn (`senior leadership appointment`) has no such word and stays at
#:    leadership. ⚠️ Known misroute, pre-registered: a phrasing about the *broker's* own
#:    leadership would also land on benefits. None occurs in the reference run; when one
#:    does, it belongs in the fixture before it belongs in a rule.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("rfp_active", ("rfp", "request for proposal", "bid posting")),
    ("broker_carrier_change", ("broker", "carrier")),
    (
        "dissatisfaction",
        (
            "dissatisfaction",
            "satisfaction",
            "sentiment",
            "complaint",
            "utilization",
        ),
    ),
    ("benefits_change", ("benefit",)),
    (
        "corporate_event",
        (
            "m a activity",
            "m a integration",
            "acquisition",
            "merger",
            "integration",
            "spin off",
            "transaction",
            "funding",
            "ownership",
            "restructuring",
            "financial",
            "consolidation",
            "relocation",
            "partnership",
            "recognition",
            "advisory",
            "strategy shift",
            "strategic",
        ),
    ),
    (
        "workforce_change",
        (
            "workforce",
            "headcount",
            "recruitment",
            "hiring",
            "job creation",
            "expansion",
            "growth",
        ),
    ),
    ("leadership_change", ("leadership", "personnel", "appointment", "executive")),
)


def classify(raw: object) -> Optional[str]:
    """The canonical class for a raw `signal_type`, or ``None`` when unrecognised.

    ``None`` is a real answer and must stay distinguishable from a class: it means "the
    model wrote something we have not seen", which is a prompt for the fixture, not a
    reason to score zero. The caller logs it and applies the band midpoint.
    """
    text = normalize(raw)
    if not text:
        return None
    for cls, needles in _RULES:
        for needle in needles:
            if needle in text:
                return cls
    return None
