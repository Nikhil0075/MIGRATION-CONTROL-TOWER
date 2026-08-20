#!/usr/bin/env bash
# Idempotent GCP bootstrap for the Autonomous Data Migration Control Tower.
# Safe to re-run — every step checks for existence before creating.
#
# Prereqs (manual, interactive — not done by this script):
#   - GCP project already created, with billing / free-tier trial linked
#   - `gcloud auth login` and `gcloud auth application-default login`
#     already run for the account that owns that project
#
# Usage:
#   gcloud config set account <your-account>
#   gcloud config set project <your-project-id>
#   bash infrastructure/gcp_setup.sh
#
# Windows gotcha: the bundled `bq` launcher looks for an executable
# literally named `python3.13`, which the Windows python.org installer
# does not create — it installs `python.exe`. `bq` then fails with
#     ERROR: (bq) python3.13: command not found
# and `set -e` aborts the whole script at the BigQuery step, BEFORE the
# Pub/Sub dead-lettering below. Export the interpreter first:
#
#   CLOUDSDK_PYTHON="$(which python)" bash infrastructure/gcp_setup.sh
set -euo pipefail

PROJECT_ID="$(gcloud config get-value project 2>/dev/null)"
REGION="${GCP_REGION:-us-central1}"
BQ_DATASET="${BQ_DATASET:-migration_target}"
SA_ORCHESTRATOR="sa-orchestrator"

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "ERROR: no active gcloud project. Run: gcloud config set project <id>" >&2
  exit 1
fi

echo "==> Using project: $PROJECT_ID, region: $REGION"

echo "==> Enabling required APIs (safe to re-run)..."
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  pubsub.googleapis.com \
  bigquery.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com \
  --project="$PROJECT_ID"

echo "==> Ensuring Firestore database (Native mode) exists..."
if ! gcloud firestore databases describe --database="(default)" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --location="$REGION" \
    --type=firestore-native \
    --project="$PROJECT_ID"
else
  echo "    Firestore database already exists, skipping."
fi

# A second database, so that `pytest` never writes to the one the console
# reads. It used to: the live project accumulated hundreds of test_run_*
# documents and 9,891 orphaned subcollection records, and because
# collection-group queries here run unfiltered (no composite index for
# group + order_by, see CLAUDE.md), the console paid to stream all of it
# on every uncached request. tests/conftest.py points the suite here.
TEST_DATABASE="${MCT_TEST_FIRESTORE_DATABASE:-mct-tests}"
echo "==> Ensuring Firestore test database '$TEST_DATABASE' exists..."
if ! gcloud firestore databases describe --database="$TEST_DATABASE" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud firestore databases create     --database="$TEST_DATABASE"     --location="$REGION"     --type=firestore-native     --project="$PROJECT_ID"
else
  echo "    Test database already exists, skipping."
fi

echo "==> Ensuring BigQuery dataset '$BQ_DATASET' exists..."
if ! bq --project_id="$PROJECT_ID" show "${PROJECT_ID}:${BQ_DATASET}" >/dev/null 2>&1; then
  bq --project_id="$PROJECT_ID" mk --dataset --location="$REGION" "${PROJECT_ID}:${BQ_DATASET}"
else
  echo "    BigQuery dataset already exists, skipping."
fi

echo "==> Ensuring Pub/Sub topics exist (master doc §8.1 event topics)..."
# risk.blocked, plan.approved, migration.completed, cutover.approved,
# and cutover.completed were declared upfront per the doc's original
# §8.1 table, before the state machine (agents/orchestrator/
# run_lifecycle.py's _CANONICAL_TRANSITIONS) was finalized — their
# names don't quite match what actually happens at those transitions
# (e.g. nothing is "approved" when a plan is created; that's a human
# gate at a different state entirely). Day 10 Phase 2 added three new,
# precisely-named topics instead of forcing the old names to fit:
# risk.assessed, plan.created, validation.requested. The five original
# unused names stay provisioned (harmless, already created in most
# projects) and reserved for a future phase that event-wires Cutover.
TOPICS=(
  "migration.requested"
  "discovery.completed"
  "risk.assessed"
  "risk.blocked"          # reserved, unused — see comment above
  "plan.created"
  "plan.approved"         # reserved, unused — see comment above
  "validation.requested"
  "migration.completed"   # reserved, unused — see comment above
  "validation.failed"
  "validation.passed"
  "cutover.approved"      # authenticated console approval command
  "cutover.completed"     # durable completion event
  "assessment.requested"  # operator-console guided assessment action
  "wave.override.requested" # audited operator wave override
  "dead-letter"           # poison messages, see the dead-lettering step below
)
for topic in "${TOPICS[@]}"; do
  if ! gcloud pubsub topics describe "$topic" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud pubsub topics create "$topic" --project="$PROJECT_ID"
  else
    echo "    Topic '$topic' already exists, skipping."
  fi
done

echo "==> Ensuring Pub/Sub pull subscriptions exist (Day 4 orchestrator, tools/events.py)..."
# Pull subscriptions today (agents/orchestrator/orchestrator.py polls
# these); Eventarc push-triggered Cloud Run is the documented next step
# (see infrastructure/README.md's Rung ladder note). Day 10 Phase 2
# added the five subscriptions below (risk.assessed through
# validation.failed) when the event chain was extended past
# RISK_ASSESSED through Planner, migration execution, and Validation's
# recovery loop — see agents/orchestrator/orchestrator.py's docstring.
declare -A SUBSCRIPTIONS=(
  ["migration-requested-sub"]="migration.requested"
  ["discovery-completed-sub"]="discovery.completed"
  ["risk-assessed-sub"]="risk.assessed"
  ["plan-created-sub"]="plan.created"
  ["validation-requested-sub"]="validation.requested"
  ["validation-passed-sub"]="validation.passed"
  ["approval-preparation-sub"]="validation.passed"
  ["validation-failed-sub"]="validation.failed"
  ["assessment-requested-sub"]="assessment.requested"
  ["cutover-approved-sub"]="cutover.approved"
  # Without this the dead-letter topic is a black hole: messages land
  # there, no subscription retains them, and they are dropped. That is
  # worse than no dead-lettering, because you believe you captured them.
  ["dead-letter-sub"]="dead-letter"
)
for sub in "${!SUBSCRIPTIONS[@]}"; do
  topic="${SUBSCRIPTIONS[$sub]}"
  if ! gcloud pubsub subscriptions describe "$sub" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud pubsub subscriptions create "$sub" \
      --topic="$topic" \
      --ack-deadline=60 \
      --project="$PROJECT_ID"
  else
    echo "    Subscription '$sub' already exists, skipping."
  fi
done

echo "==> Attaching dead-letter policies (Day 11 Phase 10)..."
# Why this exists. The one-shot workers exited after a single failure, so
# a permanently-failing message was simply left in the subscription. The
# in-process supervisor (tools/worker_supervisor.py) loops, and `nack`
# sets the ack deadline to 0 — a poison message comes straight back. Its
# per-consumer exponential backoff stops that from spinning the CPU, but
# backoff alone never ENDS: without a dead-letter policy the same bad
# message is retried until its retention expires, and the consumer makes
# no progress on anything queued behind it.
#
# One shared dead-letter topic rather than one per subscription. Pub/Sub
# stamps each dead-lettered message with the attributes
# CloudPubSubDeadLetterSourceSubscription and
# ...SourceTopicPublishTime, so provenance survives and nine near-empty
# topics do not have to.
DEAD_LETTER_TOPIC="dead-letter"
DEAD_LETTER_SUB="dead-letter-sub"
# 10 attempts, not the minimum 5. With the supervisor's backoff (5s
# doubling to a 120s cap) that is roughly ten minutes of retrying, which
# outlasts a transient Firestore or network blip but still ends. 5 would
# dead-letter a healthy message during a brief outage; 100 would take
# most of a day to give up.
MAX_DELIVERY_ATTEMPTS=10

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
# The Pub/Sub service agent — Google's own identity, not ours. It is what
# actually forwards a dead-lettered message, and it needs BOTH bindings
# below. This is the step that is easy to miss and silent when missed:
# without them Pub/Sub cannot forward, so the message is redelivered
# forever and the dead-letter policy appears configured while doing
# nothing at all.
PUBSUB_SA="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

gcloud pubsub topics add-iam-policy-binding "$DEAD_LETTER_TOPIC"   --member="serviceAccount:${PUBSUB_SA}"   --role=roles/pubsub.publisher   --project="$PROJECT_ID" --quiet >/dev/null

for sub in "${!SUBSCRIPTIONS[@]}"; do
  # Never dead-letter the dead-letter subscription onto itself.
  [ "$sub" = "$DEAD_LETTER_SUB" ] && continue

  gcloud pubsub subscriptions add-iam-policy-binding "$sub"     --member="serviceAccount:${PUBSUB_SA}"     --role=roles/pubsub.subscriber     --project="$PROJECT_ID" --quiet >/dev/null

  # `update`, unconditionally, rather than only on create. Every one of
  # these subscriptions already exists in an established project, so a
  # create-only guard would leave exactly those projects — the ones
  # actually running the workers — with no dead-letter policy. That is
  # the same drift that left assessment.requested unprovisioned while the
  # script claimed otherwise. update is idempotent.
  gcloud pubsub subscriptions update "$sub"     --dead-letter-topic="$DEAD_LETTER_TOPIC"     --max-delivery-attempts="$MAX_DELIVERY_ATTEMPTS"     --project="$PROJECT_ID" --quiet >/dev/null
  echo "    '$sub' -> '$DEAD_LETTER_TOPIC' after $MAX_DELIVERY_ATTEMPTS attempts."
done

echo "==> Ensuring service account '$SA_ORCHESTRATOR' exists..."
SA_EMAIL="${SA_ORCHESTRATOR}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_ORCHESTRATOR" \
    --display-name="Migration Orchestrator (hello-agent, Day 1)" \
    --project="$PROJECT_ID"
else
  echo "    Service account already exists, skipping."
fi

echo "==> Granting least-privilege roles to $SA_EMAIL..."
for role in roles/datastore.user roles/pubsub.publisher roles/pubsub.subscriber roles/bigquery.dataEditor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$role" \
    --condition=None \
    --quiet >/dev/null
done

echo "==> Ensuring per-agent service accounts exist (Block C, Agent Identity, §19's Rung 2:"
echo "    'one Google service account per agent, with IAM bindings that genuinely differ')..."
# Baseline roles for every agent SA: Firestore (state plane) + Pub/Sub
# publish (future event-driven agents). Fine-grained tool/action
# permission enforcement is the deterministic policy engine's job
# (tools/policy_engine.py against policies/agent_permissions.yaml /
# the registry card), not IAM — IAM here establishes distinct,
# attributable identity per agent, which is what §19's Rung 2 asks for.
declare -A AGENT_SAS=(
  ["sa-discovery"]="Discovery Agent"
  ["sa-lineage"]="Lineage Agent"
  ["sa-risk"]="Risk & Compliance Agent"
  ["sa-planner"]="Migration Planner"
  ["sa-validation"]="Validation & Reconciliation Agent"
  ["sa-cutover"]="Cutover Agent"
  ["sa-finance-impact"]="Finance Reporting Impact Agent (Finance Systems dept)"
)
for sa in "${!AGENT_SAS[@]}"; do
  display_name="${AGENT_SAS[$sa]}"
  sa_email="${sa}@${PROJECT_ID}.iam.gserviceaccount.com"
  if ! gcloud iam service-accounts describe "$sa_email" --project="$PROJECT_ID" >/dev/null 2>&1; then
    gcloud iam service-accounts create "$sa" --display-name="$display_name" --project="$PROJECT_ID"
    # New service accounts can take a few seconds to propagate before
    # they're valid IAM policy-binding members; avoids a flaky
    # "does not exist" error on the very next call.
    sleep 10
  else
    echo "    Service account '$sa' already exists, skipping."
  fi
  for role in roles/datastore.user roles/pubsub.publisher; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:${sa_email}" \
      --role="$role" \
      --condition=None \
      --quiet >/dev/null
  done
done
# validation and cutover additionally query BigQuery (reconciliation,
# post-cutover monitoring).
for sa in sa-validation sa-cutover; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${sa}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role=roles/bigquery.dataEditor \
    --condition=None \
    --quiet >/dev/null
done

echo "==> Done. Summary:"
echo "    Project:        $PROJECT_ID"
echo "    Region:         $REGION"
echo "    Firestore:      (default) native mode"
echo "    Firestore test: $TEST_DATABASE (pytest writes here, never (default))"
echo "    BigQuery:       ${PROJECT_ID}:${BQ_DATASET}"
echo "    Pub/Sub topics: ${TOPICS[*]}"
echo "    Pub/Sub subs:   ${!SUBSCRIPTIONS[*]}"
echo "    Dead letters:   -> '$DEAD_LETTER_TOPIC' after $MAX_DELIVERY_ATTEMPTS attempts;"
echo "                    inspect with: gcloud pubsub subscriptions pull $DEAD_LETTER_SUB --limit=10"
echo "    Orchestrator SA: $SA_EMAIL"
echo "    Agent SAs:       ${!AGENT_SAS[*]}"
echo ""
echo "Note: these are real distinct identities (Rung 2 of §19's ladder), not"
echo "yet workload identity federation (Rung 1) or IAM-Condition-scoped"
echo "per-collection Firestore access — fine-grained authorization stays with"
echo "tools/policy_engine.py, which is the load-bearing enforcement point today."
