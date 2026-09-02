---
type: Business Concept
title: Context references — one config, many orgs
description: A config authored once for a vertical binds org-specific values by reference to a closed vocabulary of context keys, resolved before anything reads the config.
resource: aeo/context_refs.py
tags: [config, multi-tenant, org-coupling]
timestamp: 2026-09-02
---

# Context references

A skill config is authored once for a **vertical** and run for **every org** in it. So
anywhere a value belongs to the org rather than to the vertical, the config holds a
**reference** to a context key, not a literal — and the vocabulary of keys is closed.

References are resolved against the org's runtime context **before anything reads the
config**, so no phase has to know whether a value was authored or supplied.

## The failure this prevents is silent

A config with one org's town, competitors or job titles written in as literals is still
valid, still runs, and still produces a full ranked list. It simply produces that org's
answer for every org. Nothing downstream can detect it, because a wrong result and a right
result have the same shape.

That is why the same constraint is enforced at authoring time too, by the org-coupling lint
in [aeo-skill-builder-runtime](aeo-skill-builder-runtime:/lib/validator.md) — the
scanner resolving references correctly is no help if the config never used them.

Some positions are the reverse case: **the runtime populates them at scan time and a config
must never author them.** The contract names those explicitly, because a config filling
them in would overwrite work the scanner does for itself.
