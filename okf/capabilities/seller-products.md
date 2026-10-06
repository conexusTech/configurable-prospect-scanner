---
type: Capability
title: The models judging a prospect are told what the seller actually sells
description: Which text a scan uses as the seller's product description, and why the skill builder's seeded placeholder never reaches a model.
---

# The models judging a prospect are told what the seller actually sells

Discovery, stage judgment and contact search each tell a model what the seller sells. That text is
the skill's authored product description when an operator wrote one, and otherwise every product the
organisation registered at onboarding, as plain text.

**Seeded 2026-10-06** by `qualify-only-on-real-buying-signals`, which found every builder-created
skill still carrying the builder's placeholder — so every model had been told each seller sold
prospect-scanning software.

Related: [config mapping](/lib/config-mapping.md) · [buying-signal qualification](/capabilities/buying-signal-qualification.md)

## Scenarios

#### Scenario: The builder's placeholder yields to the org's products

- GIVEN a skill whose product description is the builder's seeded "Prospect-scanning skill for the
  <vertical> vertical."
- AND an org with registered products whose descriptions are rich text
- WHEN the scan context is built
- THEN the product description lists every product by name and description, with no markup

**Checked by:** seller-placeholder-yields

#### Scenario: A description an operator wrote still wins

- GIVEN a skill whose product description was written by an operator, including one that merely
  begins like the placeholder
- WHEN the scan context is built
- THEN that text is used, not the org's products

**Checked by:** seller-authored-wins, seller-lookalike-not-placeholder

#### Scenario: An org with no products is described by its own company description

- GIVEN a skill carrying the builder's placeholder
- AND an org with no usable products but a company description written as rich text
- WHEN the scan context is built
- THEN the product description is that company description as plain text
- AND an org that has products is still described by its products

**Checked by:** seller-org-description-fallback, seller-products-beat-description
