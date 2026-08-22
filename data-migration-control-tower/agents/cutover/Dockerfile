# Cutover agent service (Deploy & Harden Phase 2b). Build context is the
# REPO ROOT.
#
# Originally believed lean (Cutover writes to BigQuery via
# tools/bigquery_tools.py, no ODBC driver needed for its own direct
# code) -- wrong in practice, found live the first time cutover.approved
# was ever actually processed by this deployed service (2026-08-22,
# Deploy & Harden async-data-plane-to-PASSED close-out):
# trigger_post_cutover_monitoring() (agents/cutover/agent.py) imports
# tools.adapters.build_adapter_for_binding, and tools/adapters/__init__.py
# unconditionally imports every adapter submodule at module load time --
# including dag_artifact_adapter, which imports tools/source_catalog.py,
# which imports pyodbc unconditionally regardless of which adapter is
# actually used. Same transitive-import gotcha agents/planner/Dockerfile
# already had to work around; cutover-agent had simply never exercised
# this import path in any run before this one.
#
#   gcloud run deploy cutover-agent --source . \
#     --dockerfile agents/cutover/Dockerfile --region us-central1 \
#     --service-account sa-cutover@PROJECT_ID.iam.gserviceaccount.com \
#     --no-allow-unauthenticated \
#     --set-env-vars GCP_PROJECT_ID=autonomous-data-migration

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
# Whole agents/ tree — see agents/discovery/Dockerfile's comment
# (Deploy & Harden Phase 5 close-out).
COPY agents/ agents/

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --system --uid 10001 --create-home controltower
USER controltower

CMD ["sh", "-c", "exec uvicorn agents.cutover.service_main:app --host 0.0.0.0 --port ${PORT}"]
