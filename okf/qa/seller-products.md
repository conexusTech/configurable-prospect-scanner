---
type: QA Checklist
title: Checks for the seller's product description
description: One check per requirement in the seller-products capability, with the command that runs it and the condition that makes it fail.
---

# Checks for the seller's product description

Proves [seller-products](/capabilities/seller-products.md).

**Preconditions.** Python 3.12 with `requirements.txt` and `pytest`; no network, no model.

---

### Check: seller-placeholder-yields

**Requirement:** The builder's placeholder yields to the org's products
**Surface:** Scan context
**Automated:** `tests/test_config_mapping.py::TestMapping::test_the_builders_placeholder_yields_to_org_products`

**Do**

Build the context for a skill carrying the placeholder for three verticals, with two org products whose descriptions contain HTML and an entity.

**Expect**

`- Frames: Metal frames for signage` and `- Lightboxes`, one per line, for every vertical.

---

### Check: seller-authored-wins

**Requirement:** A description an operator wrote still wins
**Surface:** Scan context
**Automated:** `tests/test_config_mapping.py::TestMapping::test_prefers_authored_description_over_org_products`

**Do**

Build the context for a skill with an authored description and org products.

**Expect**

The authored description.

---

### Check: seller-lookalike-not-placeholder

**Requirement:** A description an operator wrote still wins
**Surface:** Scan context
**Automated:** `tests/test_config_mapping.py::TestMapping::test_text_resembling_the_placeholder_is_not_mistaken_for_it`

**Do**

Build the context for a skill whose description starts like the placeholder but continues.

**Expect**

That description, unchanged.

---

### Check: seller-org-description-fallback

**Requirement:** An org with no products is described by its own company description
**Surface:** Scan context
**Automated:** `tests/test_config_mapping.py::TestMapping::test_with_no_products_the_orgs_own_description_is_used`

**Do**

Build the context for a placeholder skill whose org has no products (absent, empty, or one blank product) and a description with tags, an entity and a non-breaking space.

**Expect**

`Lee Company is a family-owned mechanical contractor.` in every case.

---

### Check: seller-products-beat-description

**Requirement:** An org with no products is described by its own company description
**Surface:** Scan context
**Automated:** `tests/test_config_mapping.py::TestMapping::test_products_still_win_over_the_orgs_description`

**Do**

Build the context for a placeholder skill whose org has one product and a description.

**Expect**

`- Frames`.
