# Build context is the REPO ROOT (frontend/app.py imports agents.* and
# tools.*, which are siblings of frontend/, not nested under it) — same
# convention as agents/orchestrator/hello_agent/Dockerfile (Day 1).
# Deploy with:
#
#   gcloud run deploy control-tower-ui \
#     --source . \
#     --dockerfile frontend/Dockerfile \
#     --region us-central1 --allow-unauthenticated \
#     --set-env-vars GCP_PROJECT_ID=autonomous-data-migration
#
# (run from the repo root: data-migration-control-tower/)
#
# Read-only surface (Day 10, master doc §17.2 Fri 28 Aug: "Cloud
# deployment and observability evidence capture"). This hosts the same
# frontend/app.py FastAPI backend that runs locally on `uvicorn
# frontend.app:app` — it talks to the same Firestore project, so a run
# advanced via any local script (run_full_migration.py, run_harness.py,
# ...) is immediately visible here. No SQL Server/BigQuery credentials
# are needed at this layer — the UI only ever reads Firestore.

FROM node:22-bookworm-slim AS client-build

WORKDIR /ui
COPY frontend/client/package.json frontend/client/package-lock.json ./
RUN npm ci
COPY frontend/client/ ./
RUN npm run typecheck && npm run build

FROM python:3.11-slim AS runtime

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tools/ tools/
COPY agents/ agents/
COPY contracts/ contracts/
COPY policies/ policies/
COPY packs/ packs/
COPY simulator/source_setup/estate.yaml simulator/source_setup/estate.yaml
COPY frontend/ frontend/
COPY --from=client-build /ui/web frontend/client/web

ENV PYTHONPATH=/app
ENV PORT=8080

RUN useradd --system --uid 10001 --create-home controltower
USER controltower

CMD ["sh", "-c", "exec uvicorn frontend.app:app --host 0.0.0.0 --port ${PORT}"]
