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

## 5. SHIPPED — measured against the code, not projected

| phase | before | after | mechanism |
|---|---:|---:|---|
| `enrichment` | 171 | **12** | §3 fuse 3 lanes + §4 batch 5 |
| `contacts` | 50 | **9** | §4 batch 6 (no fusion — see below) |
| **total** | **221** | **21** | **−200 grounded calls** |

**568 → 368 grounded requests on the reference run, −35%.** At its own all-in rate
($153.76 ÷ 568 = $0.271) that is **~$54**; at the ruling's $0.15 basis, $30.

✅ **Verified across 15 shapes that the gateway estimator and the scanner agree** on call
count for 1/2/3 lanes at 7/50/120/150 entities and contacts at 13/50/150. Pinned from both
sides — `tests/test_batching_contract.py` here, `prospect-cost.constants.spec.ts` there.

---

## 6. What is NOT batched or fused, with reasons

| phase | held | reason |
|---|---|---|
| `zip_discovery`, `discovery` | §2 | **Generative.** Merging targets costs 2.7× recall, invisibly — the merged call returns a full-looking list. Cost control here is target selection, never bundling. |
| `geography` | §6, both exceptions | A search→verify→re-search **loop** whose verdict **rejects** a lead outright. Batching a loop whose exit condition is "enough results are in area" changes the loop, not just its cost. |
| `validation` | §6, deliberately | See below. |
| `contacts` | §3 does not apply | It runs AFTER scoring on a top-N cut; `enrichment` runs BEFORE it on validation survivors. Different entity sets at different pipeline points — batched (§4), not fused (§3). |

### 🔴 Why `validation` is NOT batched, though §4 would permit it

It is a single-signal phase, so §4's table says batch 8 — `127 → 16` calls, **111 saved**,
the largest remaining win. It is being left alone anyway.

§6's exception is written about fusion, and validation has nothing to fuse with. But its
**reasoning** is about instruction dilution, and that applies to batching entities just as
it does to mixing signals. Its prompt is precisely the evidence-standard language §6 names:

> *"A requirement you CAN evaluate and the prospect FAILS is a disqualifier … A requirement
> you cannot evaluate from the evidence available is NOT a failure — leave it out and do
> not guess a value in order to judge it."*

And its own module docblock states the failure mode:

> *"An unparseable model response is `validated: null`, never `False`. Absence of evidence
> is not disqualification. Recording an unjudged prospect as invalid would **silently
> shrink every result set, and nothing would look wrong** — the failure mode this whole
> feature keeps producing."*

**A wrong answer here zeroes a lead.** §6's instruction is to A/B classification accuracy
against a known sample first, *"judged on correctness, never on row count"*. Taking 111
calls without that measurement is trading a known saving against an unmeasured risk of
quietly deleting qualified prospects — and the phase is specifically built so that failure
looks like a thin market rather than a bug.

**The A/B is the unlock**, and it is cheap: ~20 prospects with known verdicts, run at batch
1 and batch 8, compared on `validated` / `disqualifiers_hit` agreement. If accuracy holds,
111 calls follow.

---

## 7. Batch size — measured, not assumed (§10)

§4 anchors fused multi-group prompts at **5** because *"that is the only size anyone has
measured"*, and §10 asks for 5 / 8 / 10 on ~20 known entities, judged on:

- data points returned per entity (does the model thin results as the batch grows?)
- exact-name match rate against the input batch (where does echo pollution start?)
- entities silently dropped from the response

**Until that runs, 5 stands.** Shipping 8 on the assumption that bigger is cheaper is the
failure §4 names explicitly. The reconciliation added in this work is what makes the
measurement possible — `enrichment_unmatched` / `contacts_unmatched` count the third
criterion directly.

---

## 8. Self-lint (§9) — after the change

- [x] A generative phase merges multiple targets into one call — **no**
- [x] Two or more enriching phases share an entity set and are not fused — **fixed**; the
      three enrichment lanes are one call. `contacts` and `validation` are separate sets,
      each held with a written reason above
- [x] A fused multi-group prompt uses a batch size above 5 without measurement — **no**, 5
- [x] Any batched prompt missing the numbered list, exact-name rule, one-object-per-entity
      or empty-array-not-omission — **no**; all four come from one shared module so they
      cannot drift between phases
- [x] No post-parse name reconciliation for a batched phase — **fixed**, and a miss is
      reported rather than read as "found nothing"
- [x] The call-count and cost table is absent — **this document**

**Six of six pass.** Two items remain as measurements rather than defects: the validation
A/B (§6) and the batch-size sweep (§10).
