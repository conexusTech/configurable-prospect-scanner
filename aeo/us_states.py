"""US state, ZIP and city normalisation for a verified location.

**Why this exists.** The geography verifier answers with whatever the model wrote -- a code
("NC"), a name ("North Carolina"), either in any case. The gateway accepts a location only when
the state is one of 51 codes (50 states plus DC), the ZIP is 5 or 9 digits and the city fits 255
characters, and it ignores the whole location otherwise. So the scanner normalises here and sends
only values the gateway will take, rather than sending raw text and hoping.

⚠️ `US_STATE_CODES` must stay identical to `aeo-backend`'s `US_STATE_CODES` (and the matching
`VERIFIED_LOCATION_VALID_SQL`): a code on one side only means leads that are located here are
silently unlocated there.
"""

from __future__ import annotations

import re
from typing import Any

_STATE_NAMES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}

#: The 51 codes the gateway accepts: 50 states plus DC.
US_STATE_CODES: tuple[str, ...] = tuple(sorted(_STATE_NAMES.values()))

_CODES = frozenset(US_STATE_CODES)
#: ASCII digits only. `\d` alone matches every Unicode decimal digit ("２８２０２"), which
#: the gateway's own digit check would refuse -- and a refused location is dropped whole.
_ZIP_RE = re.compile(r"[0-9]{5}(-[0-9]{4})?")

#: The gateway's cap on a city.
MAX_CITY_CHARS = 255


def normalize_state(raw: Any) -> str | None:
    """A US state code from a code or a full name, any case; None for anything else."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    if not text:
        return None
    if text.upper() in _CODES:
        return text.upper()
    return _STATE_NAMES.get(text.lower())


def normalize_zip(raw: Any) -> str | None:
    """A 5- or 9-digit ASCII ZIP (trimmed); None for anything else."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text if _ZIP_RE.fullmatch(text) else None


def normalize_city(raw: Any) -> str | None:
    """A trimmed, non-empty city of at most 255 characters; None for anything else.

    Also None for any control character (below 32, or 127): a NUL fails the gateway's
    whole-batch UPDATE, not just this lead -- the rule `validate_explanation` applies to
    the explanation text for the same reason.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > MAX_CITY_CHARS:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None
    return text
