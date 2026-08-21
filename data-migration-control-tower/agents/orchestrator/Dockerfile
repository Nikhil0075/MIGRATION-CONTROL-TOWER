# Orchestrator service (Deploy & Harden Phase 2b) — the state-machine
# driver, distinct from agents/orchestrator/hello_agent/Dockerfile (a
# separate Day-1 proof service). Build context is the REPO ROOT.
#
#   gcloud run deploy orchestrator --source . \
#     --dockerfile agents/orchestrator/Dockerfile --region us-central1 \
#     --service-account sa-orchestrator@PROJECT_ID.iam.gserviceaccount.com \
#     --no-allow-unauthenticated \
#     --set-env-vars GCP_PROJECT_ID=autonomous-data-migration
#
# Still needs unixODBC/pyodbc, same as hello_agent's Dockerfile — NOT
# because the orchestrator itself opens a source connection, but because
# tools/registry.py::invoke_capability()'s `local`-runtime dynamic-import
# path (still the default for every AgentCard as of this phase — see
# docs/adr/0002-typed-http-dispatch.md) imports agents/discovery/agent.py
# in-process, which transitively imports tools/adapters/sqlserver_adapter.py.
# Once every real AgentCard is flipped to `runtime.type=cloud_run` (a live
# deployment decision, not made by this Dockerfile), the orchestrator
# stops needing this and this comment/apt-get block should come out —
# tracked, not silently left stale.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      unixodbc unixodbc-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/ tools/
COPY contracts/ contracts/
COPY policies/ policies/
COPY packs/ packs/
COPY agents/ agents/

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --system --uid 10001 --create-home controltower
USER controltower

CMD ["sh", "-c", "exec uvicorn agents.orchestrator.service_main:app --host 0.0.0.0 --port ${PORT}"]
