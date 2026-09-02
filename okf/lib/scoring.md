---
type: Service Module
title: The two scoring models
description: The legacy additive model, and the opt-in gated model where a floor must be met before bonuses count.
resource: aeo/gated_score.py
tags: [scoring, ranking, bands]
timestamp: 2026-09-02
---

# The two scoring models

**Additive** is the engine's original: axes contribute, the total ranks. It remains the
default.

**Gated** is opt-in per config. A prospect must clear a **floor** before bonuses are
counted at all, with partial credit defined explicitly. The difference matters when a
prospect accumulates a high total from many weak signals while failing the one condition
that actually qualifies them — additive ranks them highly, gated does not.

Selecting gated is a single field in the config, so a skill can move between models
without a code change.

When gated is in use, a **score explanation** pass produces a short account of why a
prospect scored what it did, and that text is validated against fact injection — an
explanation that introduces a claim the scoring did not use is worse than no explanation,
because it reads as evidence.

Free-text signal descriptions are classified into a **closed enum** before they can affect
scoring, so an unbounded provider string cannot become an unbounded scoring dimension.

A prospect that was never AI-judged still needs a pipeline stage to score against. A
**ported copy of the gateway's stage resolver** supplies it, so an unjudged prospect scores
against the same rung the platform would display rather than a scanner-local guess. That
port is a second implementation of someone else's logic and will drift; it is pinned by
tests.
