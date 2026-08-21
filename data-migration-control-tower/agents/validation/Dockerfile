# Validation agent service (Deploy & Harden Phase 2b). Build context is
# the REPO ROOT.
#
#   gcloud run deploy validation-agent --source . \
#     --dockerfile agents/validation/Dockerfile --region us-central1 \
#     --service-account sa-validation@PROJECT_ID.iam.gserviceaccount.com \
#     --no-allow-unauthenticated \
#     --set-env-vars GCP_PROJECT_ID=autonomous-data-migration
#
# pyodbc+psycopg variant: Validation queries both source and target for
# reconciliation, so it needs the same live-DB drivers Discovery does.

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
COPY agents/validation/ agents/validation/
COPY agents/__init__.py agents/__init__.py

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --system --uid 10001 --create-home controltower
USER controltower

CMD ["sh", "-c", "exec uvicorn agents.validation.service_main:app --host 0.0.0.0 --port ${PORT}"]
