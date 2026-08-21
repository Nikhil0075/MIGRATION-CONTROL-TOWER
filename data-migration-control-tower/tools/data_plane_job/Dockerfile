# Data-plane Cloud Run Job (Deploy & Harden Phase 3). Build context is
# the REPO ROOT. This is a Job, not a Service — no uvicorn/HTTP server,
# runs once per invocation and exits.
#
#   gcloud run jobs create migration-data-plane --source . \
#     --dockerfile tools/data_plane_job/Dockerfile --region us-central1 \
#     --service-account sa-data-plane-job@PROJECT_ID.iam.gserviceaccount.com \
#     --set-env-vars GCP_PROJECT_ID=autonomous-data-migration
#
# pyodbc+psycopg variant: this job's whole purpose is opening a live
# source connection (whichever adapter the resolved binding names).

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
COPY config/ config/

ENV PYTHONPATH=/app

RUN useradd --system --uid 10001 --create-home controltower
USER controltower

CMD ["python", "-m", "tools.data_plane_job.run_job"]
