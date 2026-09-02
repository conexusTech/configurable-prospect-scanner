# QA checklists

One checklist per capability, at `qa/<capability-name>.md`, with one check per
requirement.

**A QA check is not a test.** It is the specification of what must be proven; a test is
one way to satisfy one. Checks are written **at plan time, before the code exists** — a
list written afterwards describes what was built rather than what was asked for, and
then everything passes while the wrong thing ships.

```markdown
### Check: howto-uncached-degraded-503
**Requirement:** An uncached article is refused rather than rendered empty
**Surface:** Public article route
**Automated:** test/degraded.spec.ts

**Do**
- Stop the backend, clear the render cache, request an article

**Expect**
- 503 with a Retry-After
- The page is never marked noindex
```

`**Requirement:**` must match a `#### Scenario:` title in the capability **exactly** —
that is the backwards-traceability rule, which catches a check that outlived its
requirement. `**Automated:**` is a test path, or `Manual` when a person runs it; a
checklist with `Manual` on every line is telling you something specific.

**Include the negative case, always.** A check that only proves the happy path is half a
check, and a check with no `**Expect**` block cannot fail at all.
