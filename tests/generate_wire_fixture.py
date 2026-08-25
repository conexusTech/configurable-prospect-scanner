"""Regenerate `fixtures/eap-parity/scored-wire-payload.json`.

The scanner's ACTUAL wire payload for the parity fixture, shared with aeo-backend, which
validates it against `ScanScoredItemDto`. `forbidNonWhitelisted` is global on that side,
so a field here the DTO does not declare rejects the WHOLE scored callback — every
prospect of the run, not just the field. That is why this crosses the repo boundary as a
file rather than as an assumption.

Run:  python tests/generate_wire_fixture.py > tests/fixtures/eap-parity/scored-wire-payload.json
"""
from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# The engine lives at the repo root, which pytest adds to the path but a plain
# `python tests/…` invocation does not.
sys.path.insert(0, str(Path(__file__).parent.parent))

import av_lead_scanner as als  # noqa: E402
from aeo.event_mapping import map_scored_event  # noqa: E402
from test_eap_parity_fidelity import SCAN_DATE, SCORING, _prospect, _rows  # noqa: E402


def _stable_uuid(name: str) -> str:
    """Deterministic id per employer, and a VALID uuid.

    `prospect_id` is `@IsUUID()` on the gateway DTO, so a name there would fail the round
    trip on an artefact of this fixture rather than on the contract — the opposite of what
    the round trip is for. Deterministic so the file does not churn on regeneration.

    ⚠️ uuid5, not `UUID(sha256(name)[:32])`. That first attempt produced 32 valid hex
    digits in the right shape and the gateway rejected all seven: `@IsUUID()` checks the
    VERSION and VARIANT nibbles, which arbitrary hash bytes do not satisfy. Shaped like a
    uuid is not the same as being one.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"eap-parity/{name}"))


def main() -> int:
    rows = _rows()
    prospects = []
    for row in rows:
        prospect = _prospect(row)
        prospect["id"] = _stable_uuid(str(prospect["company_name"]))
        prospects.append(prospect)
    scored = als.score_prospects(prospects, {"scoring": SCORING}, today=SCAN_DATE)
    items = [
        item
        for payload in map_scored_event({"type": "scored", "items": scored})
        for item in payload["data"]
    ]
    print(
        json.dumps(
            {
                "_comment": (
                    "GENERATED — do not hand-edit. See tests/generate_wire_fixture.py. "
                    "The scanner's real wire payload for the eap-parity fixture; "
                    "aeo-backend validates it against ScanScoredItemDto."
                ),
                "scan_date": SCAN_DATE.isoformat(),
                "items": items,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
