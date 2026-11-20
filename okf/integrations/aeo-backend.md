---
type: External Integration
title: The gateway (aeo-backend)
description: Supplies the org runtime context and the skill config, and receives every scan event. It owns all persistence; this scanner owns none.
resource: aeo-backend:/service.md
tags: [gateway, http, events, context]
timestamp: 2026-09-02
---

# The gateway

Two calls on every run, and a third for a skill that reads a feed; they are the whole of
this scanner's durable contact with the platform.

**Read the context.** The org's runtime context, scoped to a skill slug, which carries both
the org's own data and the skill's authored config. Everything the run does is derived
from it.

**Read a feed's records** — only for a skill whose discovery sources name a gateway feed.
`GET /runtime/organizations/:orgId/source-records?skill=<slug>&source=<name>`, once per feed
source, with the same Basic credentials, answering `{feed, terms, since, total, truncated,
items}` with at most 500 rows, one per establishment, each named "<name>, <street>" (else the
city; else the plain name) with the plain name beside it; the scanner refuses more rows, or more
than 4 MB. Only the skill and the source are sent: the gateway reads the feed, its terms and its
window from the stored skill, so the scanner cannot widen the question. The shape both repos pin
is `tests/fixtures/source-records/health_inspections.json`. A refusal (an unknown feed, skill or
organization), a redirect, an unreachable gateway or an unreadable answer stops the run with a
named `source_records_*` error carrying the gateway's own words — see
[capabilities/source-records](/capabilities/source-records.md).

**Post the events.** Every phase result, plus completion and error, to the scan's event
endpoint. Shapes in [lib/event-mapping](/lib/event-mapping.md).

Authentication is HTTP Basic from environment credentials. Tenant scoping travels with
every posted event, because the gateway enforces row-level tenancy on write — the scanner
supplying it is not optional.

🔴 **No gateway call follows a redirect.** urllib's default handler copies a request's headers
onto the redirected request, `Authorization` included and whatever the new host, so all three
calls go through one opener that refuses a redirect instead. The context fetch and the event
POST raise it as an HTTP error (`GatewayRedirectRefused`); the records fetch as
`source_records_redirected`.

**This scanner holds no database, object-store or AWS client of any kind**, and no ORM. If
an event did not reach the gateway, it did not happen. That is the intended design: it
keeps the scanner stateless and disposable, which is what allows the queue to run it as a
throwaway Job.
