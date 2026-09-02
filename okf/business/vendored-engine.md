---
type: Business Concept
title: The vendored engine
description: The generic discovery and scoring engine copied in from an upstream single-vertical tool, its logged edits, and why it must never be invoked directly.
resource: UPSTREAM.md
tags: [vendoring, upstream, engine]
timestamp: 2026-09-02
---

# The vendored engine

`av_lead_scanner.py` is a **copy** of an upstream tool, pinned to a named commit and
dated in `UPSTREAM.md`. It carries the generic discovery and deterministic scoring; the
`aeo/` package around it is what makes it the platform's scanner — promoted, per the
repo's own account, from that upstream's integration *example* into the product.

## The edit rule, stated properly

`README.md` says not to edit it. The fuller truth: it **is** edited, each edit is marked
in the file, and every one is logged in `UPSTREAM.md`'s edit table — and it imports from
this repo's own `aeo` package. The practice is sound and the log is what makes re-vendoring
possible. The README's flat prohibition is the misleading half, because it omits the table
that qualifies it.

Re-vendoring is a defined procedure: fetch upstream, copy the file, run the tests, update
the pinned commit. **The tests are the load-bearing step** — they are what tells you which
of the logged edits the new copy dropped.

## Never run it directly in production

Its CLI reads a generic context shape, not the AEO one. Invoked directly it therefore
**skips the mapping entirely and scans on defaults**, quietly producing a plausible result
against a strategy nobody configured. The container's entry point is the runner, and that
is the only supported path — see [lib/runner](/lib/runner.md).

Its CLI and metadata still carry the upstream product's single-vertical name. That name
describes the engine's origin, not this repo.
