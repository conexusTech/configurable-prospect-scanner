---
type: Service Module
title: Context to engine mapping
description: The point of this repo — mapping the AEO runtime context onto the engine's flat shape, and refusing rather than defaulting when a required section is missing.
resource: aeo/config_mapping.py
tags: [config, mapping, refusal]
timestamp: 2026-09-02
---

# Context to engine mapping

The AEO platform's runtime context and the vendored engine's context are different shapes.
This module is the translation, and it is the reason this repo exists as more than a
Dockerfile.

## It refuses; it does not default

When a required section is missing or unusable — the discovery sources with their fields
and queries, the scoring configuration, the pipeline vocabulary — the mapping **raises**.
The module's own comment puts it best: a default here is a wrong answer wearing a
confident face.

That is the correct behaviour for this component specifically. A scanner that silently
defaults produces a full, plausible, ranked prospect list against a strategy nobody chose,
and nothing downstream can tell the difference between that and a real result. A refusal
costs one failed run and names the missing section.

Validation happens **twice, independently**: this mapping checks the AEO-shaped input, and
the engine checks the mapped output. The second is not redundant — it is the engine
defending its own contract against any mapper, including a future one.

## What may be configured and what may not

Provider settings — which model, which judgment tier, temperature, retry attempts — are
**deployment concerns and not config-authorable**. A skill author choosing the model would
make cost and behaviour a per-skill surprise. The one exception is the per-query yield,
which a config may override because it is genuinely a strategy choice.

References in the config are resolved before anything reads it — see
[business/context-references](/business/context-references.md).
