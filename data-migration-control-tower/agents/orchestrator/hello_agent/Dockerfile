# Build context is the REPO ROOT, not this directory, so that tools/ and
# agents/ are both available inside the image (they're siblings, not
# nested under hello_agent/). Deploy with:
#
#   gcloud run deploy hello-agent \
#     --source . \
#     --dockerfile agents/orchestrator/hello_agent/Dockerfile \
#     --region us-central1 --allow-unauthenticated
#
# (run from the repo root: data-migration-control-tower/)

FROM python:3.11-slim

# unixODBC is required for pyodbc to import even when no SQL Server
# connection is attempted from this container (see main.py notes).
RUN apt-get update && apt-get install -y --no-install-recommends \
      unixodbc unixodbc-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/ tools/
COPY agents/orchestrator/hello_agent/ agents/orchestrator/hello_agent/
COPY agents/orchestrator/__init__.py agents/orchestrator/__init__.py
COPY agents/__init__.py agents/__init__.py

ENV PYTHONPATH=/app
ENV PORT=8080

CMD ["sh", "-c", "exec uvicorn agents.orchestrator.hello_agent.main:app --host 0.0.0.0 --port ${PORT}"]
