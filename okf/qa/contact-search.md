---
type: QA Checklist
title: Checks for contact search
description: One check per requirement in the contact-search capability, with the test that runs it and the condition that makes it fail.
---

# Checks for contact search

Proves [contact-search](/capabilities/contact-search.md).

**Preconditions.** Python 3.12 with `requirements.txt` and `pytest`. No network and no model:
every provider is a stub. Run with `bash scripts/gate.sh`, or one check with its `Automated:` path.

⚠️ The checks read what leaves for the gateway — the patch merged onto a scored item and passed
through the scored-event mapping — not only the phase's return value. An empty list that the
phase never produces but the mapping would still send is the failure the last check exists for.

---

### Check: finds-up-to-three

**Requirement:** Up to three people per business ride the scored prospect, and the first is its contact
**Surface:** Contact search → scored event
**Automated:** `tests/test_contact_search.py::TestFindsUpToThree`, `tests/test_contact_search.py::TestRoleOnlyContact`, `tests/test_contact_search.py::TestPeopleListIsBounded`

**Do**

Search two businesses with a stub that names three people at each, merge the result onto scored
items and map the scored event. Separately, search eight businesses for attribution and thirteen
for the call count. Then answer with one role-only entry (empty name, office email and phone); with
a role-only entry before a named person; and with twelve nameless entries followed by a named
person under `max_people: 3`.

**Expect**

Each mapped item carries `contacts` with three entries, keyed `name`, `title`, `email`, `phone`,
`linkedin`, `source_url`, `confidence` (snake_case; no `sourceUrl`), and its five `contact_*`
fields equal the first entry. Every name in a business's list belongs to that business. Thirteen
businesses cost three provider calls. A nameless entry is not listed, the same name twice is
listed once, and an absent field is omitted rather than sent empty. The prompt still allows one
role-only entry when no named person is found. A role-only entry alone fills the title, email and
phone columns and sends no `contacts`. Beside a named person, the named person fills the columns.
An entry beyond `max_people * 4` is never read. Any of these differing fails.

---

### Check: never-invents

**Requirement:** No person and no email is invented, for any of the three
**Surface:** Contact search
**Automated:** `tests/test_contact_search.py::TestNeverInvents`, `tests/test_contact_search.py::TestFieldLimits`, `tests/test_contact_search.py::TestNonStringFieldsAreAbsent`

**Do**

Search one business with a stub naming a real person, a second whose email is
`first.last at co0`, and a third whose email is `first.last@co0.org`; then one naming "Unknown"
and "N/A" beside a real person; then one whose only person has `firstname.lastname@co0.org`; then
one with `jane.doe@co0.org`. Then, one field at a time, give a person a value one character over
its limit (name 201, title 201, email 255, phone 51, LinkedIn 501, source 501, confidence 11), and
a phone of exactly 50. Then give entries a name that is an object, a list, a number or true, and a
named person whose phone is a number and whose confidence is an object.

**Expect**

The second and third entries carry no `email`, and `contacts_data.rejected_emails` names both with
their refused addresses. A first person's template address never reaches `contact_email` and is
kept as `contacts_data.rejected_email`. "Unknown" and "N/A" are not listed. `jane.doe@co0.org` is
kept. Each over-limit field is absent from both the columns and the list, never shortened; the
50-character phone is kept. The over-long email is listed in `rejected_emails`. The over-long name
drops that person, so the next person fills the columns, and alone it fills nothing. No non-text
value becomes a person or a field, and none appears stringified anywhere in the patch. A refused
address appearing as anybody's `email`, or any field over its limit on the wire, fails the check.

---

### Check: old-shape-still-read

**Requirement:** A single-contact answer is still read, as one person
**Surface:** Contact search
**Automated:** `tests/test_contact_search.py::TestOldShapeStillRead`, `tests/test_contact_search.py::TestFieldLimits::test_an_old_shape_answer_is_held_to_the_same_limits`, `tests/test_contact_search.py::TestNonStringFieldsAreAbsent::test_an_old_shape_dict_name_fills_nothing`

**Do**

Search one business with a stub answering in the single-contact shape (the five fields, source
and confidence at top level); then with a single-contact answer that names nobody.

**Expect**

The first gives `contacts` with exactly that one person, the five columns filled from it, and
`contacts_data` holding `n`, `company_name`, `source_url` and `confidence`. The second fills the
title and email columns and sends no `contacts`. An old-shape answer is held to the new shape's
rules, so it is not identical to what this phase sent before: an over-limit phone or source is
absent, an object name fills nothing, a template email is refused, and a refused email also appears
in `rejected_emails`.

---

### Check: max-people-respected

**Requirement:** A skill asking for one person gets one
**Surface:** Contact search
**Automated:** `tests/test_contact_search.py::TestMaxPeopleRespected`

**Do**

Search with `max_people: 1` against a stub that names three; with no setting against a stub that
names five; and with `max_people` of 7, 0, -2, "2", "many", true and null against a stub naming five.

**Expect**

`max_people: 1` puts "up to 1 decision-maker" in the prompt and sends only the first person. No
setting sends three. 7 asks for and sends 3; 0 and -2 send 1; "2" sends 2; "many", true and null
send 3. More people sent than asked for fails the check.

---

### Check: nothing-erased-on-empty

**Requirement:** A search that finds nobody erases nobody
**Surface:** Contact search → scored event
**Automated:** `tests/test_contact_search.py::TestNothingErasedOnEmpty`

**Do**

Search with a stub answering `"people": []`; with a provider that raises; and with one naming only
"Unknown". Separately, map a scored item that already carries `contacts: []`.

**Expect**

No patch and no mapped item carries a `contacts` key — not an empty list. A failed search still
returns an entry for every business. Any `contacts: []` on the wire fails the check.
