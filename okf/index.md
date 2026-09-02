---
okf_version: "0.1"
conqrse_siblings:
  - name: aeo-backend
    path: ../aeo-backend
    description: The gateway. Supplies the org runtime context and this scanner's per-skill config, and receives every scan event. Owns all persistence.
  - name: conqrse-queue
    path: ../conqrse-queue
    description: Starts this scanner as an isolated k8s Job from one catalog entry, and holds the task payload this container fetches by reference.
  - name: aeo-skill-builder-runtime
    path: ../aeo-skill-builder-runtime
    description: Authors the skill config document this scanner executes.
---

# OKF Bundle — configurable-prospect-scanner

Open Knowledge Format bundle for this repo. Start at [service.md](/service.md).

## Sections

- [service.md](/service.md) — the repo as a concept; agent entry point
- [lib/](/lib/runner.md) — the runner, the two mappings, and the scoring models
- [jobs/](/jobs/phases.md) — the pipeline, phase by phase
- [business/](/business/one-runtime-many-skills.md) — domain concepts this repo owns
- [integrations/](/integrations/aeo-backend.md) — outward calls
- [playbooks/](/playbooks/offline-evaluation.md) — operational runbooks
- [briefs/](/briefs/index.md) — product ⇄ design, before the code
- [capabilities/](/capabilities/index.md) — what the system does, as scenarios. No status
- [qa/](/qa/index.md) — one checklist per capability; every requirement has a check
- [log.md](/log.md) — change history

Reserved: `index.md` and `log.md` are never concept documents.
