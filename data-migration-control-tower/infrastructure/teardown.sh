#!/usr/bin/env bash
# Idempotent inverse of gcp_setup.sh (Day 10 hardening, Phase 6).
#
# Deletes the Pub/Sub topics/subscriptions, BigQuery dataset, service
# accounts gcp_setup.sh creates, the hello-agent Cloud Run service, and
# the control-tower Artifact Registry repo (both container images).
# Firestore data is deliberately NOT touched by default (see the
# --delete-firestore section below) — this project's own safety
# discipline treats permanent deletion carefully, and a project-wide
# Firestore wipe is the single hardest action here to reverse.
#
# Deliberately left OUT OF SCOPE, on the same reversibility principle —
# these are either cheap to leave enabled/inactive or too easy to want
# back by accident:
#   - Enabled GCP APIs (run.googleapis.com, firestore.googleapis.com,
#     etc.) — disabling an API can silently break other projects in the
#     same GCP project and costs nothing to leave enabled.
#   - Any budget/billing alert configured in the console — billing
#     config isn't something this script should touch blind.
#   - The Firebase project configuration (Authentication > Sign-in
#     method) set up manually per frontend/README.md's "Approval auth"
#     section — console-only setup, not scripted in the first place, so
#     there's nothing here to tear back down.
#
# Usage:
#   bash infrastructure/teardown.sh --dry-run     # lists what would be deleted, deletes nothing
#   bash infrastructure/teardown.sh                # deletes Pub/Sub + BigQuery dataset + service accounts, asks to confirm first
#   bash infrastructure/teardown.sh --delete-firestore   # also deletes the Firestore database (separate, louder confirmation)
set -euo pipefail

PROJECT_ID="$(gcloud config get-value project 2>/dev/null)"
BQ_DATASET="${BQ_DATASET:-migration_target}"
REGION="${GCP_REGION:-us-central1}"
AR_REPO="control-tower"
CLOUD_RUN_SERVICE="hello-agent"

DRY_RUN=false
DELETE_FIRESTORE=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --delete-firestore) DELETE_FIRESTORE=true ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "ERROR: no active gcloud project. Run: gcloud config set project <id>" >&2
  exit 1
fi

TOPICS=(
  "migration.requested" "discovery.completed" "risk.assessed" "risk.blocked"
  "plan.created" "plan.approved" "validation.requested" "migration.completed"
  "validation.failed" "validation.passed" "cutover.approved" "cutover.completed"
  "assessment.requested" "wave.override.requested"
)
SUBSCRIPTIONS=(
  "migration-requested-sub" "discovery-completed-sub" "risk-assessed-sub"
  "plan-created-sub" "validation-requested-sub" "validation-passed-sub" "validation-failed-sub"
  "assessment-requested-sub"
  "cutover-approved-sub"
)
SERVICE_ACCOUNTS=(
  "sa-orchestrator" "sa-discovery" "sa-lineage" "sa-risk" "sa-planner"
  "sa-validation" "sa-cutover" "sa-finance-impact"
)

echo "==> Project: $PROJECT_ID"
echo "==> This would delete:"
echo "    Pub/Sub subscriptions: ${SUBSCRIPTIONS[*]}"
echo "    Pub/Sub topics:        ${TOPICS[*]}"
echo "    BigQuery dataset:      ${PROJECT_ID}:${BQ_DATASET}"
echo "    Service accounts:      ${SERVICE_ACCOUNTS[*]}"
echo "    Cloud Run service:     ${CLOUD_RUN_SERVICE} (region ${REGION})"
echo "    Artifact Registry repo: ${AR_REPO} (region ${REGION}) — deletes both the hello-agent and control-tower-ui images"
if [ "$DELETE_FIRESTORE" = true ]; then
  echo "    Firestore database:    (default) — ALL migration_runs/agent_registry/memory_bank/etc. data, irreversibly"
fi
echo ""

if [ "$DRY_RUN" = true ]; then
  echo "--dry-run: nothing deleted."
  exit 0
fi

read -r -p "Proceed with deletion? [y/N] " confirm
if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
  echo "Aborted — nothing deleted."
  exit 0
fi

echo "==> Deleting Pub/Sub subscriptions..."
for sub in "${SUBSCRIPTIONS[@]}"; do
  gcloud pubsub subscriptions delete "$sub" --project="$PROJECT_ID" --quiet 2>/dev/null \
    && echo "    deleted $sub" || echo "    $sub already gone, skipping."
done

echo "==> Deleting Pub/Sub topics..."
for topic in "${TOPICS[@]}"; do
  gcloud pubsub topics delete "$topic" --project="$PROJECT_ID" --quiet 2>/dev/null \
    && echo "    deleted $topic" || echo "    $topic already gone, skipping."
done

echo "==> Deleting BigQuery dataset ${PROJECT_ID}:${BQ_DATASET}..."
bq --project_id="$PROJECT_ID" rm -r -f -d "${PROJECT_ID}:${BQ_DATASET}" 2>/dev/null \
  && echo "    deleted ${BQ_DATASET}" || echo "    ${BQ_DATASET} already gone, skipping."

echo "==> Deleting Cloud Run service ${CLOUD_RUN_SERVICE} (region ${REGION})..."
gcloud run services delete "$CLOUD_RUN_SERVICE" --region="$REGION" --project="$PROJECT_ID" --quiet 2>/dev/null \
  && echo "    deleted $CLOUD_RUN_SERVICE" || echo "    $CLOUD_RUN_SERVICE already gone, skipping."

echo "==> Deleting Artifact Registry repo ${AR_REPO} (region ${REGION}, both container images)..."
gcloud artifacts repositories delete "$AR_REPO" --location="$REGION" --project="$PROJECT_ID" --quiet 2>/dev/null \
  && echo "    deleted $AR_REPO" || echo "    $AR_REPO already gone, skipping."

echo "==> Removing IAM bindings and deleting service accounts..."
for sa in "${SERVICE_ACCOUNTS[@]}"; do
  sa_email="${sa}@${PROJECT_ID}.iam.gserviceaccount.com"
  if gcloud iam service-accounts describe "$sa_email" --project="$PROJECT_ID" >/dev/null 2>&1; then
    for role in roles/datastore.user roles/pubsub.publisher roles/pubsub.subscriber roles/bigquery.dataEditor; do
      gcloud projects remove-iam-policy-binding "$PROJECT_ID" \
        --member="serviceAccount:${sa_email}" --role="$role" --condition=None --quiet >/dev/null 2>&1 || true
    done
    gcloud iam service-accounts delete "$sa_email" --project="$PROJECT_ID" --quiet
    echo "    deleted $sa_email"
  else
    echo "    $sa_email already gone, skipping."
  fi
done

if [ "$DELETE_FIRESTORE" = true ]; then
  echo ""
  echo "==> Firestore database deletion requested."
  read -r -p "This IRREVERSIBLY deletes every migration_runs/agent_registry/memory_bank/etc. document. Type the project ID ('$PROJECT_ID') to confirm: " project_confirm
  if [ "$project_confirm" = "$PROJECT_ID" ]; then
    gcloud firestore databases delete --database="(default)" --project="$PROJECT_ID" --quiet
    echo "    Firestore database deleted."
  else
    echo "    Confirmation did not match project ID — Firestore NOT deleted."
  fi
fi

echo ""
echo "==> Teardown complete. APIs (run.googleapis.com etc.) are left enabled —"
echo "    re-run infrastructure/gcp_setup.sh any time to rebuild everything above."
