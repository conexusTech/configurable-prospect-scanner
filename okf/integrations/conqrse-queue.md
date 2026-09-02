---
type: External Integration
title: conqrse-queue
description: Starts this scanner as an isolated k8s Job and holds the task payload, delivered by reference rather than through the environment.
resource: conqrse-queue:/service.md
tags: [queue, k8s, payload, task]
timestamp: 2026-09-02
---

# conqrse-queue

The queue starts this container as its own k8s Job, from a single catalog entry — see
[business/one-runtime-many-skills](/business/one-runtime-many-skills.md).

## The payload arrives by reference

**The only thing injected into the container is a task record id.** The container then
fetches the payload — organization, tenant, scan run, skill slug, optional phase subset —
from the queue's task API.

That indirection is deliberate. A payload passed through the environment is size-limited,
appears in process listings and pod descriptions, and is fixed at launch. A reference is
small, keeps the payload out of the container's environment, and means the queue remains
the one place the payload is stored.

The presence of that id is also how the container knows it is running under the queue at
all, rather than being run by hand — the direct-run path takes its parameters from the
environment instead.
