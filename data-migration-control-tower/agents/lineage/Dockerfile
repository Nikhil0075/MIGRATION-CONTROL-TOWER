# Lineage agent service (Deploy & Harden Phase 2b). Build context is the
# REPO ROOT.
#
#   gcloud run deploy lineage-agent --source . \
#     --dockerfile agents/lineage/Dockerfile --region us-central1 \
#     --service-account sa-lineage@PROJECT_ID.iam.gserviceaccount.com \
#     --no-allow-unauthenticated \
#     --set-env-vars GCP_PROJECT_ID=autonomous-data-migration
#
# Lean variant: Lineage reads Firestore-catalogued metadata, never a live
# source connection — no unixODBC/pyodbc build step needed, unlike
# Discovery/Validation's Dockerfiles.

FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/ tools/
COPY contracts/ contracts/
COPY policies/ policies/
COPY agents/lineage/ agents/lineage/
COPY agents/__init__.py agents/__init__.py

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --system --uid 10001 --create-home controltower
USER controltower

CMD ["sh", "-c", "exec uvicorn agents.lineage.service_main:app --host 0.0.0.0 --port ${PORT}"]
