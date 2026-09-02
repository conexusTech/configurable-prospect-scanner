# Briefs

What product and design agreed, **before engineering starts** — one file per change, at
`briefs/<slug>.md`.

A brief carries a `## Narrative` (what happens today · why that is a problem, and for
whom · what will be different · what somebody can do afterwards · what is deliberately
not included), the screens, every state a screen can be in, the scenarios, the data each
screen needs in ordinary words, the invented fields that need checking, and open
dependencies with owners.

**The minimum state set, every time:** empty · loading and partly loaded · error · the
thing exists but its parts do not · permission denied · over-limit.

**Never in a brief:** file paths · component names · API shapes or field names · hook
names · "Option A / Option B" · status of any kind. Each of those can be wrong in a way
reading the code would reveal, and a brief is written by someone who has not opened it.

Its scenarios **move** to the capability when the change lands; two permanent copies of
one scenario drift, and the capability is the one with evidence behind it.
