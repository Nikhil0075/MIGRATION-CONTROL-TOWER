# Finance Reporting Impact agent service (Deploy & Harden Phase 2b).
# Build context is the REPO ROOT. Lean variant — see
# agents/lineage/Dockerfile's note.
#
#   gcloud run deploy finance-impact-agent --source . \
#     --dockerfile agents/finance/Dockerfile --region us-central1 \
#     --service-account sa-finance-impact@PROJECT_ID.iam.gserviceaccount.com \
#     --no-allow-unauthenticated \
#     --set-env-vars GCP_PROJECT_ID=autonomous-data-migration

FROM python:3.11-slim

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

CMD ["sh", "-c", "exec uvicorn agents.finance.service_main:app --host 0.0.0.0 --port ${PORT}"]
