---
type: Service Module
title: Event mapping
description: Translates engine output into the gateway's scan-event shapes — the only way anything from a run becomes durable.
resource: aeo/event_mapping.py
tags: [events, mapping, gateway]
timestamp: 2026-09-02
---

# Event mapping

Every durable thing a run produces leaves through here, as an event POSTed to the
gateway. There is no other write path.

| Event | Carries |
|---|---|
| Zip codes | the validated ZIP rows per market, with how each was discovered |
| Prospects | the discovered companies, each stamped with the phase that produced it |
| Validations | the per-prospect verdict, plus any enrichment lanes, in an open field |
| Scored | the ranked result: score, rank, factors, contact details, AI analysis and adjustment, signal and its date, disqualification, explanation, and priority band |
| Completed | the run summary — counts and cost |
| Error | a message |

## Two shape decisions worth respecting

**The validation payload is deliberately open.** Enrichment lanes are authored per skill
and differently shaped by design, so a closed schema there would mean a schema change per
skill. See [jobs/phases](/jobs/phases.md).

**The pipeline-status side-channel on the scored event is a designed field, not a spare
pocket.** It carries the resolved sales-pipeline stage and where that stage came from.
Putting unrelated extras there because it happens to be an open object is how a
side-channel becomes an undocumented second schema.

**A scored lead carries a location only if it was verified.** The engine's scored item
always has a city and state — discovery's raw values — and those are deliberately not on the
fixed list. A lead the geography step verified AND relocated (its verified location was itself in the scan area) sends its normalised city, state and ZIP
(state one of the 51 US codes the gateway accepts); any other lead sends none, and the
internal marker itself is never sent — it is not part of the scored contract.

⚠️ **Deploy order: the gateway (`aeo-backend` `fca4d0e`) before this scanner.** Not because the
scanner would be rejected — corrected 2026-10-01: `POST /runtime/scans/:id/events` binds a raw
object and calls `validate(instance)` with no options (`runtime-scan-events.controller.ts`), so an
undeclared field is **ignored**, not a 400. The risk is quieter: an older gateway drops the
location while this scanner has already **scored** the lead on it, so that run stores a score and
a location that disagree, and nothing ever re-sends it. Both shipped 2026-10-01 in that order.

Prospect and scored events pass a fixed field list through rather than forwarding whatever
the engine produced, so an engine change cannot silently widen what this scanner sends.
