# Grounded-search bundling — §8 call-count report

The report `Skill-Authoring-Bundling-Rules.md` §8 requires before any phase config is drafted.
Every figure below is **measured from a real run**, not estimated from the rules' defaults.

**Reference run:** `711b6652-5810-4d77-a39a-570e97e58d14` — `consulting-prospect-scanner`,
150 discovered / 127 validated / 57 scored, **3 validation lanes**, `actual_cost` **$153.76**,
625 model calls, **568 grounded requests**, 8,798 grounded search queries.

---

## 1. Phase classification (§1)

The test is *"is the entity list an INPUT to this phase or an OUTPUT of it?"*

| phase | grounded | role | entity set | evidence |
|---|---:|---|---|---|
| `zip_discovery` | 10 | **generative** | — outputs zips | row count depends on what it finds |
| `discovery` | 60 | **generative** | — outputs companies | same |
| `geography` | 150 | **enriching** | A: discovered (150) | `geo_loop.py:100` takes `prospects` as input |
| `validation` | 127 | **enriching** | B: geo survivors (127) | 1 call per entity |
| `enrichment` | 171 | **enriching** | C1: validation survivors (57) | **57 × 3 lanes = 171 exactly** |
| `contacts` | 50 | **enriching** | C2: top-N AFTER scoring (50) | separate set — see §3 |
| `judgment` | 0 | non-grounded | C1 | reasons over collected data only |

**88% of grounded requests are enriching** (498 of 568). Generative is 12% — the part §2 says
never to bundle. The rules therefore land on the expensive part of our pipeline.

---

## 2. The defect, in one line

`aeo/phases/enrichment.py:259`:

```python
work = [(p, lane) for p in targets for lane in runnable]
```

One grounded call per **(prospect × lane)** — the Cartesian product. Neither batched (§4) nor
fused (§3). This is the exact pattern §3 measured at *3 calls / 240s / 14 data points* against
*1 fused call / 47s / 26 data points*, and `171 = 57 × 3` is that pattern visible in our own
telemetry.

⚠️ **This is an ENGINE defect, not an authoring one.** The rules are written for skill authors,
but this loop runs one call per lane *regardless of how a skill is authored*. No authoring
discipline can fix it. The engine is ours (`configurable-prospect-scanner`).

---

## 3. §8 table — the fusion target

🔴 **Corrected before implementation.** A first draft of this table put `contacts` in the same
entity set as `enrichment`. It is not. `runner.py:1083`:

> *"After scoring, deliberately: contact search is the most expensive call per prospect, so it
> runs on the set that survived validation and only for the top `SCANNER_TOP_N` by rank.
> Enriching a prospect nobody will look at is spend with no reader."*

`enrichment` runs **before** scoring on validation survivors; `contacts` runs **after** scoring on
a top-N cut. Different sets, different pipeline points. Fusing them would require either running
contacts on everyone (more expensive — it would undo a deliberate optimisation) or deferring
enrichment until after scoring (impossible — scoring consumes enrichment's output).

**They are two separate changes.**

| Phase | Role | Entity set | Fused with | Batch | Calls / 100 entities |
|---|---|---|---|---|---:|
| `enrichment` lanes 1-3 | enriching | C1: validation survivors | each other (§3) | **5** | **20** |
| `contacts` | enriching | C2: top-N after scoring | nothing — batched alone (§4) | **6** | **17** |

`enrichment`: 3 groups on one set → §3 fuse, §4 batch 5 → `ceil(100/5) × 1` = **20**.
`contacts`: single-signal phase, no set to fuse with → §4's *"contact search alone: 6"* →
`ceil(100/6)` = **17**.

### On this run (57 enrichment entities, 50 contact targets)

| phase | now | after | saved |
|---|---:|---:|---:|
| `enrichment` (3 lanes × 57) | **171** | `ceil(57/5)` = **12** | 159 |
| `contacts` (1 × 50) | **50** | `ceil(50/6)` = **9** | 41 |
| **total** | **221** | **21** | **200** |

**568 → 368 grounded requests, a 35% reduction**, without touching a generative phase.

---

## 4. Money and time

| basis | saving on run `711b6652` |
|---|---:|
| rules' stated $0.15 / grounded call | **$30.00** |
| this run's all-in rate ($153.76 ÷ 568 = **$0.271**) | **$54.20** — 35% of the run |

⚠️ **The all-in rate is the honest one** and it is higher than the rules' basis because it
includes tokens. This run burned **7.70M thinking tokens**, of which `enrichment` alone was
**3.59M (47%)** across its 171 calls. Fusing 171 → 12 removes most of that, but *not* 14× —
each fused call does more work. **Token saving is real but not proportional, and is the largest
uncertainty in this report.**

**Time:** §3 measured 240s → 47s (5.1×) for 3 groups over 5 companies. `enrichment` is our
heaviest phase by thinking tokens, so the wall-clock gain should be the most visible one.

---

## 5. What is NOT being fused, with §6 reasons

| phase | why held back |
|---|---|
| `geography` | **Both §6 exceptions at once.** It is a search→verify→re-search *loop* whose verdict **rejects** a lead outright, under a strict evidence standard. §6: *"if a wrong answer zeroes the lead rather than scoring it lower, that signal earns its own call."* |
| `validation` | **§6 candidate** — it produces `disqualifiers_hit`, a hard disqualifier. §6 requires an A/B on classification accuracy against a known sample **before** fusing, judged on correctness not row count. Not fused in phase 1. |
| `zip_discovery`, `discovery` | §2 — generative. Bundling targets costs 2.7× recall, invisibly. Cost control here is target selection, never fusion. |

If `validation` later passes its A/B it does NOT fold into C1 either — it runs on set B (geo
survivors, 127) before enrichment's set C1 exists. It would batch alone at 5:
`127 → ceil(127/5) = 26`, a further **101 calls saved**.

---

## 6. Batch size — measured, not assumed (§10)

§4 anchors fused multi-group prompts at **5** because *"that is the only size anyone has
measured"*, and §10 asks for 5 / 8 / 10 on ~20 known entities, judged on:

- data points returned per entity (does the model thin results as the batch grows?)
- exact-name match rate against the input batch (where does echo pollution start?)
- entities silently dropped from the response

**Until that runs, 5 stands.** Shipping 8 on the assumption that bigger is cheaper is the
failure §4 names explicitly: an oversized batch degrades *silently*, and both failure modes look
like "the data wasn't out there".

---

## 7. Self-lint (§9) — current state

- [x] A generative phase merges multiple targets into one call — **no**, correct today
- [ ] Two or more enriching phases share an entity set and are not fused — **YES, set C1: 3 lanes**
- [x] Fused batch above 5 without measurement — n/a, nothing is fused yet
- [ ] Batched prompt missing numbered list / exact-name rule / one-object-per-entity /
      empty-array-not-omission — **all four missing; nothing is batched**
- [ ] No post-parse name reconciliation for a batched phase — **none exists**
- [x] §8 table absent — **this document**

Four of six fail. All four are the same change.
