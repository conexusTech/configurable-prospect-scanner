---
type: External Integration
title: The gateway (aeo-backend)
description: Supplies the org runtime context and the skill config, and receives every scan event. It owns all persistence; this scanner owns none.
resource: aeo-backend:/service.md
tags: [gateway, http, events, context]
timestamp: 2026-09-02
---

# The gateway

Two calls, and they are the whole of this scanner's durable contact with the platform.

**Read the context.** The org's runtime context, scoped to a skill slug, which carries both
the org's own data and the skill's authored config. Everything the run does is derived
from it.

**Post the events.** Every phase result, plus completion and error, to the scan's event
endpoint. Shapes in [lib/event-mapping](/lib/event-mapping.md).

Authentication is HTTP Basic from environment credentials. Tenant scoping travels with
every posted event, because the gateway enforces row-level tenancy on write — the scanner
supplying it is not optional.

**This scanner holds no database, object-store or AWS client of any kind**, and no ORM. If
an event did not reach the gateway, it did not happen. That is the intended design: it
keeps the scanner stateless and disposable, which is what allows the queue to run it as a
throwaway Job.
