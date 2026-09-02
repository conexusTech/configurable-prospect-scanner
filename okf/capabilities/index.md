# Capabilities

What the system does, as scenarios — one file per capability, at
`capabilities/<name>.md`. **No status:** the roadmap is the only status surface.

Every scenario is falsifiable and names the check that proves it:

```markdown
#### Scenario: An uncached article is refused rather than rendered empty
- GIVEN the backend is unreachable and the article is not in the render cache
- WHEN a crawler requests it
- THEN the response is a 503 with a Retry-After

**Checked by:** howto-uncached-degraded-503
```

A scenario that cannot fail is worse than none, because it reports the same green.

**Capabilities are seeded lazily.** A file appears when the first change touches that
capability, and contains only the requirements that change verified. Existing concepts
are never back-written into capabilities — that produces a large, plausible, unverified
spec, which is the failure this process exists to prevent. **An empty directory here is
correct**, and the absence of a file is information.

Every capability needs `okf/qa/<same-name>.md`; `scripts/okf-check.mjs` enforces it in
both directions.
