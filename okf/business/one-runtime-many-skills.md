---
type: Business Concept
title: Many skills, one runtime
description: Every skill runs on this same image and registers as a single queue catalog entry; per-skill behaviour is config, never a fork.
resource: configurable-prospect-scanner
tags: [architecture, decision, config-driven, naming]
timestamp: 2026-09-02
---

# Many skills, one runtime

Every skill the platform builds runs **this image**, registered in the queue as **one**
catalog entry. A new vertical is a new config document, not a new container.

## What the alternative costs

Per-skill images mean every engine improvement has to be rebuilt and redeployed N times,
each fork drifts from the others, and each acquires its own small edits that nobody
remembers. The scanner's behaviour then depends on which image a particular skill happens
to be pinned to — which is unanswerable from the config alone.

The price of the chosen approach is that **the config is now load-bearing**, which is why
[lib/config-mapping](/lib/config-mapping.md) refuses a missing section instead of
defaulting, and why [business/context-references](/business/context-references.md) exists.

## One string, three places, deliberately

The repository name, the queue task reference and the platform's runtime slug are all the
same string. That is a decision, not a coincidence: a mismatch between those three had
already caused name-collision defects several times.

⚠️ There is still a naming layer underneath. The vendored engine file and its upstream
repository carry the original single-vertical product's name — see
[business/vendored-engine](/business/vendored-engine.md). "This repo" and "the engine
inside it" are different things with different names, and conflating them is the mistake
the shared string was chosen to prevent.
