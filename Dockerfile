# Container image for the queue catalog entry `configurable-prospect-scanner`.
#
# One image serves every builder-created skill — that is decision C-1: all skills
# point their `runtime_slug` at this single taskRef, and the per-skill behaviour
# arrives as config in the runtime context rather than as a separate image. So this
# image must contain no vertical logic and no customer data.
FROM python:3.12-slim

# Fail fast and log straight through: this runs as a queue task with no TTY, and a
# buffered traceback that arrives after the process is reaped is a lost diagnosis.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so a config or adapter change does not re-resolve pip.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY av_lead_scanner.py ./
COPY aeo/ ./aeo/

# Non-root: the task handles third-party web content and model output, so it runs
# with the least privilege the runner will give it.
RUN useradd --create-home --shell /usr/sbin/nologin scanner \
    && chown -R scanner:scanner /app
USER scanner

# The adapter is the entrypoint, never the raw tool. It fetches the org's runtime
# context, maps it (aeo/config_mapping.py), drives discover → score, and forwards
# each event to POST /runtime/scans/{id}/events. Invoking the tool directly would
# skip the mapping and quietly scan on defaults.
ENTRYPOINT ["python", "-m", "aeo.runner"]
