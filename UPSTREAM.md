# Upstream provenance

This repo is a **derivation**, not an original. Its discovery-and-scoring engine
was copied from a live customer skill.

| | |
|---|---|
| **Upstream repo** | `skill-av-lead-scanner` (`git@github.com:conexusTech/skill-av-lead-scanner.git`) |
| **Upstream commit** | `3791a718b385f80b3565e38a7246fa8165bc519d` |
| **Copied on** | 2026-08-04 |
| **Files taken verbatim** | `av_lead_scanner.py`, `requirements.txt`, `tests/` |
| **Files kept for reference only** | everything under `reference/` |

## Why a copy and not a dependency

`skill-av-lead-scanner` is a **live customer skill** (church/AV integration). The
platform's generic scanner and one customer's scanner must not be the same
artifact — a change made for the platform would reach a paying customer's
production runs, and vice versa. Upstream is also understood to be feature-frozen
going forward, which is what makes a copy affordable rather than a slow leak.

## The rule that keeps this affordable

**Do not edit `av_lead_scanner.py`.**

It is vendored verbatim so that pulling an upstream fix stays a diff rather than an
archaeology exercise. Everything this platform needs lives in separate modules
(`aeo/`), exactly the boundary upstream themselves drew — their
`examples/aeo_integration.py` header states that the core tool "stays generic — it
knows nothing about AEO" and that all AEO-specific behaviour belongs in a wrapper.
That file is preserved at `reference/upstream-aeo_integration.py`; our `aeo/`
package is that example promoted to production.

If the tool genuinely must change, prefer contributing upstream and re-vendoring.
If that is impossible, record the edit in the table below so the next person
pulling upstream knows where the conflicts will be.

| Date | File | What was changed and why |
|---|---|---|
| — | — | no local edits to vendored files yet |

## How to re-vendor

```bash
git -C ../skill-av-lead-scanner fetch && git -C ../skill-av-lead-scanner log --oneline HEAD..origin/main
cp ../skill-av-lead-scanner/av_lead_scanner.py .   # then run the tests
```

Update the commit hash above in the same change.
