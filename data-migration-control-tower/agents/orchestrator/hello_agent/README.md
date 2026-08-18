# hello-agent (Day 1)

Minimal agent proving the foundation plumbing works end to end before any
real reasoning agent (Discovery, Day 2) is built.

Tools: `list_source_tables()` (SQL Server metadata read), `write_firestore_row()`
(Firestore state-plane write).

## Run locally (proves source-metadata read)

```bash
cd data-migration-control-tower
python agents/orchestrator/hello_agent/local_run.py
```

## Deploy to Cloud Run (proves cloud runtime + state-plane write)

Run from the **repo root** (`data-migration-control-tower/`), not this
directory — the Docker build context must include the sibling `tools/`
package:

The installed gcloud SDK version may not support `--dockerfile`; if so,
build and push the image explicitly instead (build context must be the
repo root so `tools/` is included):

```bash
IMAGE=us-central1-docker.pkg.dev/<project-id>/control-tower/hello-agent:latest
docker build -f agents/orchestrator/hello_agent/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"

gcloud run deploy hello-agent \
  --image "$IMAGE" \
  --region us-central1 \
  --allow-unauthenticated \
  --service-account sa-orchestrator@<project-id>.iam.gserviceaccount.com \
  --set-env-vars GCP_PROJECT_ID=<project-id> \
  --memory 1Gi

SVC_URL=$(gcloud run services describe hello-agent --region us-central1 --format='value(status.url)')
curl "$SVC_URL/status"
curl -X POST -d '' "$SVC_URL/bootstrap-check"
```

`--memory 1Gi` is required — the real `google-adk` + `google-cloud-aiplatform`
import chain uses more than Cloud Run's 512Mi default at startup (confirmed
by a first deploy that failed the startup probe with `Memory limit of 512
MiB exceeded`).

`-d ''` on the POST is required — Cloud Run's frontend rejects a POST
with no body and no `Content-Length` header (411).

Note the health endpoint is `/status`, not `/healthz`: Cloud Run's
default `*.run.app` domain intercepts `/healthz` at the infrastructure
level and never forwards it to the container (reproduced identically on
Google's own stock Cloud Run quickstart image — confirmed 16 Aug 2026,
not app-specific).

Confirm the row via `gcloud firestore` or the Firestore console under the
`_bootstrap_check` collection.

## Why Cloud Run doesn't read the local DB directly

Cloud Run has no network path to a laptop's local Docker network. Day 1's
exit condition is proven in two halves instead of one fake hop:
source-metadata read locally, cloud-runtime + Firestore write on Cloud
Run. An optional `cloudflared`/`ngrok` tunnel can bridge the two for a
live demo later (see the plan file), but is not required for Day 1.
