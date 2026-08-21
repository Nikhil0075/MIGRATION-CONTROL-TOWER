#!/usr/bin/env bash
# One-time Workload Identity Federation bootstrap for GitHub Actions
# (Deploy & Harden Phase 2e). RUN THIS YOURSELF — per your own decision
# this session, this grants an external identity (GitHub) federated
# access into your GCP project, and that's worth your own eyes on it
# before it's live. This script is not executed by any agent session;
# it's handed to you to read and run.
#
# Idempotent — safe to re-run (each step checks for existence first),
# same convention as infrastructure/gcp_setup.sh.
#
# After running this, set these two GitHub Actions repo VARIABLES
# (Settings -> Secrets and variables -> Actions -> Variables tab — NOT
# secrets; WIF needs no long-lived key, that's the whole point):
#   WIF_PROVIDER    = (printed at the end)
#   WIF_DEPLOYER_SA = (printed at the end)
#
# Then create two GitHub Environments (Settings -> Environments):
#   staging     — no protection rules needed
#   production  — "Required reviewers" enabled, add yourself
#
# Usage: bash infrastructure/wif_bootstrap.sh
set -euo pipefail

PROJECT_ID="$(gcloud config get-value project 2>/dev/null)"
GITHUB_REPO="${GITHUB_REPO:-Nikhil0075/MIGRATION-CONTROL-TOWER}"
GITHUB_BRANCH="${GITHUB_BRANCH:-main}"
POOL_ID="github-pool"
PROVIDER_ID="github-provider"
DEPLOYER_SA="sa-github-deployer"

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "ERROR: no active gcloud project. Run: gcloud config set project <id>" >&2
  exit 1
fi

PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

echo "==> Enabling IAM Credentials + STS APIs (needed for WIF)..."
gcloud services enable iamcredentials.googleapis.com sts.googleapis.com --project="$PROJECT_ID"

echo "==> Ensuring Workload Identity Pool '$POOL_ID' exists..."
if ! gcloud iam workload-identity-pools describe "$POOL_ID" --project="$PROJECT_ID" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL_ID" \
    --project="$PROJECT_ID" --location=global \
    --display-name="GitHub Actions (Deploy & Harden)"
else
  echo "    Already exists, skipping."
fi

echo "==> Ensuring OIDC provider '$PROVIDER_ID' exists, scoped to $GITHUB_REPO @ $GITHUB_BRANCH..."
if ! gcloud iam workload-identity-pools providers describe "$PROVIDER_ID" \
      --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID" >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
    --project="$PROJECT_ID" --location=global --workload-identity-pool="$POOL_ID" \
    --display-name="GitHub Actions OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.ref=assertion.ref" \
    --attribute-condition="assertion.repository == '${GITHUB_REPO}' && assertion.ref == 'refs/heads/${GITHUB_BRANCH}'"
    # The --attribute-condition is what scopes trust to the EXACT repo
    # and branch (Deploy & Harden Phase 2e's hardening requirement) —
    # a bare attribute.repository mapping with no condition would trust
    # ANY branch/PR from that repo, including forks if the workflow ever
    # ran on pull_request instead of push.
else
  echo "    Already exists, skipping."
fi

echo "==> Ensuring deploy-only service account '$DEPLOYER_SA' exists..."
DEPLOYER_EMAIL="${DEPLOYER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$DEPLOYER_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$DEPLOYER_SA" \
    --project="$PROJECT_ID" \
    --display-name="GitHub Actions deploy-only identity (Deploy & Harden Phase 2e)"
else
  echo "    Already exists, skipping."
fi

echo "==> Granting narrowly-scoped deploy roles (run.developer, not run.admin)..."
for role in roles/run.developer roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${DEPLOYER_EMAIL}" \
    --role="$role" --condition=None --quiet >/dev/null
done

echo "==> Granting iam.serviceAccountUser ONLY on the named runtime SAs (not project-wide)..."
for sa in sa-orchestrator sa-discovery sa-lineage sa-risk sa-planner sa-validation sa-cutover sa-finance-impact; do
  RUNTIME_SA_EMAIL="${sa}@${PROJECT_ID}.iam.gserviceaccount.com"
  gcloud iam service-accounts add-iam-policy-binding "$RUNTIME_SA_EMAIL" \
    --member="serviceAccount:${DEPLOYER_EMAIL}" \
    --role="roles/iam.serviceAccountUser" \
    --project="$PROJECT_ID" --quiet >/dev/null
done

echo "==> Binding the deployer SA to the WIF pool, scoped to $GITHUB_REPO..."
gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_EMAIL" \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/attribute.repository/${GITHUB_REPO}" \
  --quiet >/dev/null

echo ""
echo "==> Done. Set these as GitHub Actions repo VARIABLES (not secrets):"
echo ""
echo "WIF_PROVIDER=projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_ID}/providers/${PROVIDER_ID}"
echo "WIF_DEPLOYER_SA=${DEPLOYER_EMAIL}"
echo ""
echo "Then create GitHub Environments 'staging' (no protection) and 'production'"
echo "(Required reviewers enabled) before .github/workflows/deploy.yml can run end to end."
