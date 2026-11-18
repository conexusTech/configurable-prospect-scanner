"""Contact search finds up to three named decision-makers per business. Ours.

Capability `contact-search`, checklist `okf/qa/contact-search.md`. Every provider here
is a stub: nothing in this file reaches a model, a search or the network.

The contract with the gateway (`aeo-backend`, row `prospects-hold-several-contacts`):

- the five `contact_*` fields on the scored item are filled from the FIRST person,
  in the fields a single contact always used;
- the scored item gains `contacts`, at most three `{name, title, email, phone,
  linkedin, source_url, confidence}`, in snake_case like every other key on that wire;
- an empty or failed search sends no `contacts` key at all, so nothing is erased.
"""

from __future__ import annotations

import inspect
import json
import re

from aeo.event_mapping import map_scored_event
from aeo.phases._batching import CONTACT_BATCH
from aeo.phases.contacts import (
    DEFAULT_MAX_PEOPLE,
    MAX_PEOPLE_CEILING,
    find_contacts,
    merge_into_scored,
)

PID = "11111111-1111-1111-1111-11111111111{}"


def _parse(text: str) -> list[dict]:
    """Stand-in for the engine's parse_json_array."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    return [p for p in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(p, dict)]


def _prospects(n: int) -> list[dict]:
    return [
        {"id": PID.format(i), "company_name": f"Co {i}", "city": "Austin", "state": "TX"}
        for i in range(n)
    ]


def _person(org: str, k: int, **over: str) -> dict:
    slug = org.replace(" ", "").lower()
    person = {
        "contact_name": f"Person {k} of {org}",
        "contact_title": f"Title {k}",
        "contact_email": f"p{k}@{slug}.org",
        "contact_phone": f"512-555-010{k}",
        "contact_linkedin": f"https://www.linkedin.com/in/p{k}-{slug}",
        "source_url": f"https://{slug}.org/staff",
        "confidence": "high",
    }
    person.update(over)
    return person


def _names_in(prompt: str) -> list[str]:
    names = []
    for line in prompt.splitlines():
        line = line.strip()
        if line and line[0].isdigit() and "name: " in line:
            names.append(line.split("name: ", 1)[1].split(";")[0].strip())
    return names


def _people_provider(per_org: int):
    """Answers per entity in the new shape, `per_org` people each."""

    def call(prompt, **kwargs):
        call.prompts.append(prompt)
        return json.dumps(
            [
                {
                    "n": i,
                    "company_name": name,
                    "people": [_person(name, k) for k in range(1, per_org + 1)],
                }
                for i, name in enumerate(_names_in(prompt), 1)
            ]
        )

    call.prompts = []
    return call


def _fixed(response: object):
    def call(prompt, **kwargs):
        call.prompts.append(prompt)
        return json.dumps(response)

    call.prompts = []
    return call


def _boom(prompt, **kwargs):
    raise RuntimeError("provider exploded")


def _search(prospects, provider, contacts_config=None):
    return find_contacts(
        prospects,
        contacts_config=contacts_config or {},
        product_description="AV integration",
        provider=provider,
        provider_config={},
        parse_json_array=_parse,
    )


def _wire(prospects, patches, scored_extra=None):
    """What actually leaves for the gateway: merge, then the scored-event mapping."""
    scored = [
        {"prospect_id": p["id"], "score": 50, **(scored_extra or {})} for p in prospects
    ]
    merged = merge_into_scored(scored, patches)
    out = map_scored_event({"type": "scored", "items": merged})
    return {item["prospect_id"]: item for batch in out for item in batch["data"]}


# ── finds-up-to-three ────────────────────────────────────────────────────


class TestFindsUpToThree:
    def test_three_people_ride_the_scored_item_and_the_first_fills_the_columns(self):
        prospects = _prospects(2)
        provider = _people_provider(3)
        wire = _wire(prospects, _search(prospects, provider))

        for i, p in enumerate(prospects):
            item = wire[p["id"]]
            org = f"Co {i}"
            people = item["contacts"]
            assert [c["name"] for c in people] == [
                f"Person 1 of {org}", f"Person 2 of {org}", f"Person 3 of {org}",
            ]
            first = _person(org, 1)
            assert people[0] == {
                "name": first["contact_name"],
                "title": first["contact_title"],
                "email": first["contact_email"],
                "phone": first["contact_phone"],
                "linkedin": first["contact_linkedin"],
                "source_url": first["source_url"],
                "confidence": "high",
            }
            # The columns are the first person.
            assert item["contact_name"] == first["contact_name"]
            assert item["contact_title"] == first["contact_title"]
            assert item["contact_email"] == first["contact_email"]
            assert item["contact_phone"] == first["contact_phone"]
            assert item["contact_linkedin"] == first["contact_linkedin"]

    def test_the_wire_keys_are_snake_case_like_the_rest_of_the_scored_item(self):
        prospects = _prospects(1)
        wire = _wire(prospects, _search(prospects, _people_provider(1)))
        person = wire[prospects[0]["id"]]["contacts"][0]
        assert set(person) == {
            "name", "title", "email", "phone", "linkedin", "source_url", "confidence",
        }
        assert "sourceUrl" not in person

    def test_each_business_keeps_ITS_OWN_people_across_a_batch(self):
        prospects = _prospects(8)
        patches = _search(prospects, _people_provider(3))
        for i, p in enumerate(prospects):
            names = [c["name"] for c in patches[p["id"]]["contacts"]]
            assert all(n.endswith(f"of Co {i}") for n in names), (
                "a business received another business's people — the failure batching "
                "makes possible"
            )

    def test_still_one_grounded_call_per_batch_of_six(self):
        provider = _people_provider(3)
        _search(_prospects(13), provider)
        assert CONTACT_BATCH == 6
        assert len(provider.prompts) == 3  # ceil(13 / 6), unchanged by three people

    def test_the_prompt_asks_for_up_to_three_best_match_first(self):
        provider = _people_provider(3)
        _search(_prospects(2), provider, {"titles": ["Facilities Director"]})
        prompt = provider.prompts[0]
        assert "up to 3 decision-makers" in prompt
        assert '"people"' in prompt
        assert "best match" in prompt.lower()
        assert "Facilities Director" in prompt
        # The phase's two non-negotiables, stated for every person.
        assert "Do not guess or construct an email address" in prompt
        assert "Never invent a person" in prompt

    def test_a_person_without_a_name_is_not_a_person(self):
        prospects = _prospects(1)
        provider = _fixed([{
            "n": 1, "company_name": "Co 0",
            "people": [
                {"contact_title": "Pastor", "contact_email": "office@co0.org"},
                _person("Co 0", 2),
            ],
        }])
        patch = _search(prospects, provider)[prospects[0]["id"]]
        assert [c["name"] for c in patch["contacts"]] == ["Person 2 of Co 0"]
        # Columns and list agree on who comes first.
        assert patch["contact_name"] == "Person 2 of Co 0"
        assert patch["contact_title"] == "Title 2"

    def test_the_same_person_twice_collapses_to_one(self):
        prospects = _prospects(1)
        provider = _fixed([{
            "n": 1, "company_name": "Co 0",
            "people": [
                _person("Co 0", 1, contact_name="Jane Doe"),
                _person("Co 0", 2, contact_name="  jane  DOE "),
                _person("Co 0", 3),
            ],
        }])
        patch = _search(prospects, provider)[prospects[0]["id"]]
        assert [c["name"] for c in patch["contacts"]] == ["Jane Doe", "Person 3 of Co 0"]

    def test_an_absent_field_is_omitted_not_sent_empty(self):
        prospects = _prospects(1)
        provider = _fixed([{
            "n": 1, "company_name": "Co 0",
            "people": [{"contact_name": "Jane Doe", "contact_title": "", "contact_email": None}],
        }])
        patch = _search(prospects, provider)[prospects[0]["id"]]
        assert patch["contacts"] == [{"name": "Jane Doe"}]


# ── never-invents ────────────────────────────────────────────────────────


class TestNeverInvents:
    def _patch(self, people):
        prospects = _prospects(1)
        provider = _fixed([{"n": 1, "company_name": "Co 0", "people": people}])
        return _search(prospects, provider)[prospects[0]["id"]]

    def test_a_guessed_email_is_dropped_on_the_second_and_third_person(self):
        patch = self._patch([
            _person("Co 0", 1),
            _person("Co 0", 2, contact_email="first.last at co0"),
            _person("Co 0", 3, contact_email="first.last@co0.org"),
        ])
        people = patch["contacts"]
        assert len(people) == 3
        assert people[0]["email"] == "p1@co0.org"
        assert "email" not in people[1], "a malformed address was promoted"
        assert "email" not in people[2], "a template-guessed address was promoted"
        # Kept as evidence, never as an address somebody would mail.
        assert patch["contacts_data"]["rejected_emails"] == [
            {"name": "Person 2 of Co 0", "email": "first.last at co0"},
            {"name": "Person 3 of Co 0", "email": "first.last@co0.org"},
        ]
        # The first person's good address still fills the column.
        assert patch["contact_email"] == "p1@co0.org"
        assert "rejected_email" not in patch["contacts_data"]

    def test_a_template_guess_on_the_first_person_never_reaches_the_column(self):
        patch = self._patch([_person("Co 0", 1, contact_email="firstname.lastname@co0.org")])
        assert "contact_email" not in patch
        assert "email" not in patch["contacts"][0]
        assert patch["contacts_data"]["rejected_email"] == "firstname.lastname@co0.org"
        assert patch["contacts_data"]["rejected_emails"] == [
            {"name": "Person 1 of Co 0", "email": "firstname.lastname@co0.org"},
        ]

    def test_a_placeholder_name_is_dropped_per_person(self):
        patch = self._patch([
            _person("Co 0", 1),
            _person("Co 0", 2, contact_name="Unknown"),
            _person("Co 0", 3, contact_name="N/A"),
        ])
        assert [c["name"] for c in patch["contacts"]] == ["Person 1 of Co 0"]

    def test_a_real_address_that_merely_looks_ordinary_is_kept(self):
        patch = self._patch([_person("Co 0", 1, contact_email="jane.doe@co0.org")])
        assert patch["contacts"][0]["email"] == "jane.doe@co0.org"
        assert patch["contact_email"] == "jane.doe@co0.org"


# ── old-shape-still-read ─────────────────────────────────────────────────


class TestOldShapeStillRead:
    def test_a_single_contact_response_gives_one_person(self):
        prospects = _prospects(1)
        provider = _fixed([{
            "n": 1, "company_name": "Co 0",
            "contact_name": "Jane Doe", "contact_title": "Facilities Director",
            "contact_email": "jane@co0.org", "contact_phone": "512-555-0100",
            "contact_linkedin": "https://linkedin.com/in/janedoe",
            "source_url": "https://co0.org/staff", "confidence": "high",
        }])
        patch = _search(prospects, provider)[prospects[0]["id"]]
        assert patch["contacts"] == [{
            "name": "Jane Doe", "title": "Facilities Director", "email": "jane@co0.org",
            "phone": "512-555-0100", "linkedin": "https://linkedin.com/in/janedoe",
            "source_url": "https://co0.org/staff", "confidence": "high",
        }]
        # Columns and `contacts_data` keep the single-contact phase's keys. Not identical
        # to it: template emails are now refused and `rejected_emails` is added.
        assert patch["contact_name"] == "Jane Doe"
        assert patch["contact_email"] == "jane@co0.org"
        assert patch["contacts_data"] == {
            "n": "1", "company_name": "Co 0",
            "source_url": "https://co0.org/staff", "confidence": "high",
        }

    def test_an_old_shape_with_no_name_fills_columns_as_before_and_sends_no_list(self):
        prospects = _prospects(1)
        provider = _fixed([{"contact_title": "Office", "contact_email": "office@co0.org"}])
        patch = _search(prospects, provider)[prospects[0]["id"]]
        assert patch["contact_title"] == "Office"
        assert patch["contact_email"] == "office@co0.org"
        assert "contacts" not in patch


# ── max-people-respected ─────────────────────────────────────────────────


class TestMaxPeopleRespected:
    def test_max_people_one_asks_for_and_sends_one(self):
        prospects = _prospects(1)
        provider = _people_provider(3)  # the model over-delivers
        patch = _search(prospects, provider, {"max_people": 1})[prospects[0]["id"]]
        assert "up to 1 decision-maker" in provider.prompts[0]
        assert [c["name"] for c in patch["contacts"]] == ["Person 1 of Co 0"]
        assert patch["contact_name"] == "Person 1 of Co 0"

    def test_never_more_than_the_default_even_when_the_model_sends_five(self):
        assert DEFAULT_MAX_PEOPLE == 3
        prospects = _prospects(1)
        patch = _search(prospects, _people_provider(5))[prospects[0]["id"]]
        assert len(patch["contacts"]) == 3

    def test_max_people_is_clamped_to_one_through_three(self):
        assert MAX_PEOPLE_CEILING == 3
        for authored, asked in ((7, 3), (0, 1), (-2, 1), ("2", 2), ("many", 3), (True, 3), (None, 3)):
            prospects = _prospects(1)
            provider = _people_provider(5)
            patch = _search(prospects, provider, {"max_people": authored})[prospects[0]["id"]]
            assert f"up to {asked} decision-maker" in provider.prompts[0], authored
            assert len(patch["contacts"]) == asked, authored


# ── nothing-erased-on-empty ──────────────────────────────────────────────


class TestNothingErasedOnEmpty:
    def test_an_empty_search_sends_no_contacts_key(self):
        prospects = _prospects(1)
        provider = _fixed([{"n": 1, "company_name": "Co 0", "people": []}])
        patches = _search(prospects, provider)
        assert "contacts" not in patches[prospects[0]["id"]]
        wire = _wire(prospects, patches)
        assert "contacts" not in wire[prospects[0]["id"]]

    def test_a_failed_search_sends_no_contacts_key(self):
        prospects = _prospects(2)
        patches = _search(prospects, _boom)
        assert set(patches) == {p["id"] for p in prospects}
        wire = _wire(prospects, patches)
        for p in prospects:
            assert "contacts" not in patches[p["id"]]
            assert "contacts" not in wire[p["id"]]

    def test_a_search_finding_only_placeholders_sends_no_contacts_key(self):
        prospects = _prospects(1)
        provider = _fixed([{"n": 1, "company_name": "Co 0",
                            "people": [{"contact_name": "Unknown"}]}])
        patches = _search(prospects, provider)
        assert "contacts" not in _wire(prospects, patches)[prospects[0]["id"]]

    def test_an_empty_list_on_a_scored_item_never_reaches_the_wire(self):
        # Defence in depth: whatever put it there, `[]` is "erase", not "found nobody".
        out = map_scored_event({"type": "scored", "items": [
            {"prospect_id": PID.format(0), "contacts": []},
        ]})
        assert "contacts" not in out[0]["data"][0]


# ── cost: output growth per batch against the provider's output cap ──────


class TestOutputFitsTheProviderCap:
    """A batch of 6 with three people each must fit the provider's output cap.

    Measured, not assumed: the Claude provider sets `max_tokens` explicitly; the Gemini
    provider sets no output cap, so the model's own limit applies there.
    """

    @staticmethod
    def _claude_cap() -> int:
        import av_lead_scanner as als

        found = re.findall(r"max_tokens=(\d+)", inspect.getsource(als.claude_provider))
        assert found, "the Claude provider no longer states its output cap"
        return int(found[0])

    @staticmethod
    def _gemini_sets_no_cap() -> bool:
        import av_lead_scanner as als

        src = inspect.getsource(als.gemini_provider)
        return "max_output_tokens" not in src

    @staticmethod
    def _realistic_long_person(org: str, k: int) -> dict:
        # Long, real-world-sized values — not the column maxima, which no real
        # person reaches all at once.
        slug = f"{org.replace(' ', '').lower()}-community-church-of-greater-austin"
        return {
            "contact_name": f"Rev. Dr. Alexandra Montgomery-Whitfield {k}",
            "contact_title": "Executive Director of Facilities, Operations and Technology",
            "contact_email": f"alexandra.montgomery-whitfield{k}@{slug[:30]}.org",
            "contact_phone": "+1 (512) 555-0100 ext. 2045",
            "contact_linkedin": f"https://www.linkedin.com/in/alexandra-montgomery-whitfield-{k}a8b9c0d1",
            "source_url": f"https://www.{slug}.org/about/our-leadership-team/staff-directory",
            "confidence": "medium",
        }

    def test_a_full_batch_of_six_with_three_people_fits_with_headroom(self):
        cap = self._claude_cap()
        assert cap == 8192
        assert self._gemini_sets_no_cap()

        orgs = [f"Co {i}" for i in range(CONTACT_BATCH)]
        new_shape = [
            {"n": i, "company_name": org,
             "people": [self._realistic_long_person(org, k) for k in (1, 2, 3)]}
            for i, org in enumerate(orgs, 1)
        ]
        old_shape = [
            {"n": i, "company_name": org, **self._realistic_long_person(org, 1)}
            for i, org in enumerate(orgs, 1)
        ]
        # Models pretty-print; indent=2 is the larger of the two layouts.
        new_chars = len(json.dumps(new_shape, indent=2))
        old_chars = len(json.dumps(old_shape, indent=2))

        # ~4 chars per token for English/JSON; 3 is the conservative bound for
        # URL- and punctuation-heavy text.
        conservative_tokens = new_chars / 3
        assert conservative_tokens < cap / 2, (
            f"a batch of {CONTACT_BATCH} x 3 people is ~{conservative_tokens:.0f} tokens "
            f"against a {cap} cap — lower CONTACT_BATCH or max_people"
        )
        # Growth is bounded by the person count: at most a little over 3x.
        assert new_chars < old_chars * 3.2


# ── field limits: a field over its limit is DROPPED, never cut (review B1) ──


def _one(people, contacts_config=None):
    prospects = _prospects(1)
    provider = _fixed([{"n": 1, "company_name": "Co 0", "people": people}])
    return _search(prospects, provider, contacts_config)[prospects[0]["id"]]


class TestFieldLimits:
    """Today's gateway refuses the WHOLE scored event over one over-long column
    (`@MaxLength(50)` on phone, 255 on name/title/email), losing every prospect in it.
    A cut email or URL is a fabricated one, so an over-limit field is absent instead.
    """

    def test_the_limits_are_the_decided_ones(self):
        from aeo.phases.contacts import FIELD_LIMITS

        assert FIELD_LIMITS == {
            "contact_name": 200, "contact_title": 200, "contact_email": 254,
            "contact_phone": 50, "contact_linkedin": 500, "source_url": 500,
            "confidence": 10,
        }

    def test_an_over_limit_name_drops_the_whole_person(self):
        patch = _one([
            _person("Co 0", 1, contact_name="N" * 201),
            _person("Co 0", 2),
        ])
        assert [c["name"] for c in patch["contacts"]] == ["Person 2 of Co 0"]
        assert patch["contact_name"] == "Person 2 of Co 0"
        assert patch["contact_email"] == "p2@co0.org", "the dropped person's email leaked"

    def test_an_over_limit_name_is_not_even_a_role_only_fallback(self):
        patch = _one([_person("Co 0", 1, contact_name="N" * 201)])
        assert patch.get("contact_email") is None
        assert "contacts" not in patch

    def test_a_name_at_the_limit_is_kept(self):
        patch = _one([_person("Co 0", 1, contact_name="N" * 200)])
        assert patch["contact_name"] == "N" * 200

    def test_an_over_limit_title_is_absent_not_cut(self):
        patch = _one([_person("Co 0", 1, contact_title="T" * 201)])
        assert "contact_title" not in patch
        assert "title" not in patch["contacts"][0]
        assert patch["contacts"][0]["name"] == "Person 1 of Co 0"

    def test_an_over_limit_email_is_refused_as_evidence(self):
        long_email = "a" * 247 + "@co0.org"
        assert len(long_email) == 255  # one over the limit, and still a valid shape
        patch = _one([
            _person("Co 0", 1, contact_email=long_email),
            _person("Co 0", 2, contact_email=long_email),
        ])
        assert "contact_email" not in patch
        assert all("email" not in c for c in patch["contacts"])
        assert patch["contacts_data"]["rejected_email"] == long_email
        assert patch["contacts_data"]["rejected_emails"] == [
            {"name": "Person 1 of Co 0", "email": long_email},
            {"name": "Person 2 of Co 0", "email": long_email},
        ]

    def test_an_over_limit_phone_is_absent_and_one_at_the_limit_is_kept(self):
        patch = _one([
            _person("Co 0", 1, contact_phone="5" * 51),
            _person("Co 0", 2, contact_phone="5" * 50),
        ])
        assert "contact_phone" not in patch, "a 51-character phone 400s today's gateway"
        assert "phone" not in patch["contacts"][0]
        assert patch["contacts"][1]["phone"] == "5" * 50

    def test_an_over_limit_linkedin_is_absent(self):
        patch = _one([_person("Co 0", 1, contact_linkedin="https://l.in/" + "x" * 490)])
        assert "contact_linkedin" not in patch
        assert "linkedin" not in patch["contacts"][0]

    def test_an_over_limit_source_url_is_absent_everywhere(self):
        patch = _one([_person("Co 0", 1, source_url="https://s.org/" + "x" * 490)])
        assert "source_url" not in patch["contacts"][0]
        assert "source_url" not in patch.get("contacts_data", {})

    def test_an_over_limit_confidence_is_absent_everywhere(self):
        patch = _one([_person("Co 0", 1, confidence="h" * 11)])
        assert "confidence" not in patch["contacts"][0]
        assert "confidence" not in patch.get("contacts_data", {})

    def test_an_old_shape_answer_is_held_to_the_same_limits(self):
        prospects = _prospects(1)
        provider = _fixed([{
            "contact_name": "Jane Doe", "contact_phone": "5" * 5000,
            "source_url": "https://s.org/" + "x" * 490,
        }])
        patch = _search(prospects, provider)[prospects[0]["id"]]
        assert patch["contact_name"] == "Jane Doe"
        assert "contact_phone" not in patch
        assert "source_url" not in patch.get("contacts_data", {})

    def test_the_wire_never_carries_an_over_limit_column(self):
        prospects = _prospects(1)
        patches = _search(prospects, _fixed([{
            "n": 1, "company_name": "Co 0",
            "people": [_person("Co 0", 1, contact_phone="5" * 5000)],
        }]))
        item = _wire(prospects, patches)[prospects[0]["id"]]
        assert "contact_phone" not in item
        assert all("phone" not in c for c in item["contacts"])


# ── non-string values are absent, never stringified into people (review S1) ──


class TestNonStringFieldsAreAbsent:
    def test_nested_values_never_become_a_person(self):
        patch = _one([
            {"contact_name": {"first": "Jane", "last": "Doe"},
             "contact_title": ["Pastor"], "contact_email": {"a": 1}},
            {"contact_name": ["Jane Doe"], "contact_phone": 5125550100},
            {"contact_name": True, "contact_title": 3.5},
            _person("Co 0", 4, contact_phone=5125550100, confidence={"x": 1}),
        ])
        assert [c["name"] for c in patch["contacts"]] == ["Person 4 of Co 0"]
        assert patch["contact_name"] == "Person 4 of Co 0"
        assert "contact_phone" not in patch
        assert "phone" not in patch["contacts"][0]
        assert "confidence" not in patch["contacts"][0]
        flat = json.dumps(patch)
        assert "{'first'" not in flat and "['Pastor']" not in flat

    def test_an_old_shape_dict_name_fills_nothing(self):
        prospects = _prospects(1)
        provider = _fixed([{"contact_name": {"first": "Jane"}, "contact_title": ["Pastor"]}])
        patch = _search(prospects, provider)[prospects[0]["id"]]
        assert "contact_name" not in patch and "contact_title" not in patch
        assert "contacts" not in patch


# ── role-only contacts are kept, in the columns only (review S3) ─────────


class TestRoleOnlyContact:
    def test_the_prompt_still_allows_a_role_only_entry(self):
        provider = _people_provider(1)
        _search(_prospects(1), provider)
        prompt = provider.prompts[0]
        assert "no named person" in prompt
        assert 'empty "contact_name"' in prompt
        assert "general" in prompt

    def test_a_role_only_entry_fills_the_columns_and_is_never_listed(self):
        patch = _one([{
            "contact_name": "", "contact_title": "Church Office",
            "contact_email": "office@co0.org", "contact_phone": "512-555-0199",
            "source_url": "https://co0.org/contact",
        }])
        assert patch["contact_title"] == "Church Office"
        assert patch["contact_email"] == "office@co0.org"
        assert patch["contact_phone"] == "512-555-0199"
        assert "contact_name" not in patch
        assert "contacts" not in patch
        assert patch["contacts_data"]["source_url"] == "https://co0.org/contact"

    def test_a_named_person_beats_a_role_only_entry_for_the_columns(self):
        patch = _one([
            {"contact_name": "", "contact_email": "office@co0.org"},
            _person("Co 0", 1),
        ])
        assert patch["contact_email"] == "p1@co0.org"
        assert [c["name"] for c in patch["contacts"]] == ["Person 1 of Co 0"]


# ── the model's list is bounded before cleaning (review N2) ──────────────


class TestPeopleListIsBounded:
    def test_only_the_first_max_people_times_four_entries_are_read(self):
        nameless = [{"contact_name": "", "contact_title": f"Office {i}"} for i in range(12)]
        patch = _one(nameless + [_person("Co 0", 1)], {"max_people": 3})
        assert "contacts" not in patch, "an entry past max_people * 4 was read"
        assert patch["contact_title"] == "Office 0"
