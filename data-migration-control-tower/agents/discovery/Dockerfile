# Discovery agent service (Deploy & Harden Phase 2b). Build context is
# the REPO ROOT, same convention as agents/orchestrator/hello_agent/Dockerfile:
#
#   gcloud run deploy discovery-agent --source . \
#     --dockerfile agents/discovery/Dockerfile --region us-central1 \
#     --service-account sa-discovery@PROJECT_ID.iam.gserviceaccount.com \
#     --no-allow-unauthenticated \
#     --set-env-vars GCP_PROJECT_ID=autonomous-data-migration
#
# pyodbc+psycopg variant: Discovery's adapters (tools/adapters/sqlserver_adapter.py,
# postgres_adapter.py) open live source connections, so unixODBC is
# required at import time even before a connection is attempted (same
# note as hello_agent/Dockerfile).

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
COPY agents/discovery/ agents/discovery/
COPY agents/__init__.py agents/__init__.py

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --system --uid 10001 --create-home controltower
USER controltower

CMD ["sh", "-c", "exec uvicorn agents.discovery.service_main:app --host 0.0.0.0 --port ${PORT}"]
