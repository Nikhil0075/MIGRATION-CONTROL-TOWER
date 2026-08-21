# Planner agent service (Deploy & Harden Phase 2b). Build context is the
# REPO ROOT.
#
# NOT the lean variant, despite the original design intent (see
# agents/lineage/Dockerfile's note) — discovered live (Deploy & Harden
# Phase 5 close-out): agents/planner/agent.py imports
# tools.pack_loader at module level, which eagerly imports
# tools.adapters (tools/adapters/__init__.py imports every adapter,
# including dag_artifact_adapter -> tools.source_catalog -> `import
# pyodbc`), so pyodbc's native libodbc.so.2 is required at import time
# even though Planner itself never opens a live SQL Server connection.
# Same unixODBC requirement as the discovery/validation/orchestrator
# variant — see agents/discovery/Dockerfile's note.
#
#   gcloud run deploy planner-agent --source . \
#     --dockerfile agents/planner/Dockerfile --region us-central1 \
#     --service-account sa-planner@PROJECT_ID.iam.gserviceaccount.com \
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
COPY packs/ packs/
# Whole agents/ tree — see agents/discovery/Dockerfile's comment
# (Deploy & Harden Phase 5 close-out).
COPY agents/ agents/

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --system --uid 10001 --create-home controltower
USER controltower

CMD ["sh", "-c", "exec uvicorn agents.planner.service_main:app --host 0.0.0.0 --port ${PORT}"]
