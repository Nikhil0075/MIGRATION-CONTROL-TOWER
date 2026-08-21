#!/usr/bin/env bash
# Creates the GCS bucket Terraform stores its own state in (Deploy &
# Harden Phase 2a, docs/adr/0004-terraform-for-infrastructure.md) —
# Terraform cannot create the bucket it needs before it can run, so this
# one bootstrap step stays a shell script even after everything else
# moves into infrastructure/terraform/. Idempotent, same convention as
# infrastructure/gcp_setup.sh.
#
# Usage: bash infrastructure/terraform_state_bootstrap.sh
set -euo pipefail

PROJECT_ID="$(gcloud config get-value project 2>/dev/null)"
REGION="${GCP_REGION:-us-central1}"
BUCKET="${TF_STATE_BUCKET:-${PROJECT_ID}-tfstate}"

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "ERROR: no active gcloud project. Run: gcloud config set project <id>" >&2
  exit 1
fi

echo "==> Ensuring Terraform state bucket gs://${BUCKET} exists..."
if ! gcloud storage buckets describe "gs://${BUCKET}" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --uniform-bucket-level-access
  gcloud storage buckets update "gs://${BUCKET}" --versioning
else
  echo "    Already exists, skipping."
fi

echo "==> Done. Set infrastructure/terraform/versions.tf's backend \"gcs\" { bucket = \"${BUCKET}\" }"
echo "    then run: terraform init (from infrastructure/terraform/)"
