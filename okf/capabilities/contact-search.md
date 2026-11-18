---
type: Capability
title: Contact search finds up to three named decision-makers per business
description: How many people the contact search asks for and sends per business, what keeps it from inventing a person or an email, how an older single-contact answer is read, and why a search that finds nobody erases nothing.
---

# Contact search finds up to three named decision-makers per business

After scoring, the contact search looks for the people a salesperson should call at each
top-ranked business. It finds up to three named decision-makers, best match to the skill's
target titles first, and sends all of them with the scored prospect. The first of them is
also the prospect's one contact, in the fields a single contact always used. When nobody can be
named, a general office email, phone or role can still fill those fields, but it is never listed as
a person.

**Seeded 2026-11-18** by `scanner-finds-up-to-three-contacts` (Phase CRM-2, PRD R-27.a). How the
gateway stores and shows the people it receives is `aeo-backend:/capabilities/prospect-contacts.md`,
seeded by `prospects-hold-several-contacts`.

Related: [phases](/jobs/phases.md) · [event mapping](/lib/event-mapping.md)

## Scenarios

#### Scenario: Up to three people per business ride the scored prospect, and the first is its contact

- GIVEN a contact search whose provider names three people at each business, best match first
- WHEN the scored prospects are sent to the gateway
- THEN each carries all three, each with whichever of name, title, email, phone, LinkedIn,
  source and confidence was found
- AND the prospect's own contact name, title, email, phone and LinkedIn are the first person's
- AND every business keeps its own people, never a neighbour's from the same search
- AND the search still costs one grounded call per six businesses
- AND an entry with no name, such as a general office email or phone, fills the prospect's contact
  fields only when nobody is named, and is never listed as a person

**Checked by:** finds-up-to-three

#### Scenario: No person and no email is invented, for any of the three

- GIVEN a search that returns, beside a real person, a second person whose email does not parse,
  a third whose email is a naming template such as `first.last@…`, and a person named "Unknown"
- WHEN the prospect is sent
- THEN neither refused address is sent as anybody's email, and both are kept only as evidence
- AND the placeholder-named person is not sent at all
- AND a real address of ordinary shape is still kept
- AND a field longer than its limit (name and title 200, email 254, phone 50, LinkedIn and source
  500, confidence 10) is left out, never shortened: an over-long email is refused as evidence, and
  an over-long name drops the whole person
- AND a value that is not text (an object, a list, a number, true or false) is treated as absent,
  so it never becomes a person

**Checked by:** never-invents

#### Scenario: A single-contact answer is still read, as one person

- GIVEN a provider that ignores the new shape and answers with one contact per business
- WHEN the prospect is sent
- THEN it carries that one person, and its contact fields are filled from it
- AND it is held to the same rules as the new shape, so two things differ from before this change:
  a template-guessed email such as `first.last@…` is now refused, and every refused email is also
  listed by name in the evidence

**Checked by:** old-shape-still-read

#### Scenario: A skill asking for one person gets one

- GIVEN a skill whose contact settings ask for one person per business
- WHEN the search runs and the provider names three anyway
- THEN the search asked for one, and only the first person is sent
- AND a setting above three is held to three, below one to one, and an unreadable one is three

**Checked by:** max-people-respected

#### Scenario: A search that finds nobody erases nobody

- GIVEN a business whose search found nobody, failed outright, or found only placeholders
- WHEN the scored prospect is sent
- THEN it carries no list of people at all, never an empty one that would erase people an earlier
  run found

**Checked by:** nothing-erased-on-empty
