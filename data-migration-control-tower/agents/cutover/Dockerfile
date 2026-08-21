# Cutover agent service (Deploy & Harden Phase 2b). Build context is the
# REPO ROOT. Lean variant — Cutover writes to BigQuery via
# tools/bigquery_tools.py (google-cloud-bigquery, pure Python client
# library, no ODBC driver needed) rather than a live source connection,
# so it doesn't need Discovery/Validation's unixODBC build step.
#
#   gcloud run deploy cutover-agent --source . \
#     --dockerfile agents/cutover/Dockerfile --region us-central1 \
#     --service-account sa-cutover@PROJECT_ID.iam.gserviceaccount.com \
#     --no-allow-unauthenticated \
#     --set-env-vars GCP_PROJECT_ID=autonomous-data-migration

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/ tools/
COPY contracts/ contracts/
COPY policies/ policies/
COPY agents/cutover/ agents/cutover/
COPY agents/__init__.py agents/__init__.py

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --system --uid 10001 --create-home controltower
USER controltower

CMD ["sh", "-c", "exec uvicorn agents.cutover.service_main:app --host 0.0.0.0 --port ${PORT}"]
