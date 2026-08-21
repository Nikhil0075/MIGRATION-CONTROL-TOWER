# Terraform module — Deploy & Harden Phase 2a

`docs/adr/0004-terraform-for-infrastructure.md` explains why this exists. **Not validated against a
real `terraform validate`/`plan` in this environment** — neither `terraform` nor `tofu` is
installed here. Run both, and read their output carefully, before the first `apply`. Treat this
module as reviewed-by-construction, not proven-by-execution, until that happens.

## Bootstrap (one time)

```bash
bash ../terraform_state_bootstrap.sh
# then edit versions.tf's backend "gcs" { bucket = "..." } to match what that printed
terraform init
```

## Importing existing resources (do this before the first real `apply`)

This project's dev/demo GCP project already has `infrastructure/gcp_setup.sh`-created resources —
service accounts, Pub/Sub topics/subscriptions, the BigQuery dataset, the reports bucket. Applying
this module cold would try to create duplicates and fail on "already exists," or worse, silently
adopt them without you reviewing what Terraform now controls. Import each one explicitly first:

```bash
# Service accounts (repeat per agent_service_account_ids entry)
terraform import 'google_service_account.agent["sa-orchestrator"]' \
  projects/PROJECT_ID/serviceAccounts/sa-orchestrator@PROJECT_ID.iam.gserviceaccount.com

# Pub/Sub topics (repeat per pubsub_topics entry)
terraform import 'google_pubsub_topic.topic["migration.requested"]' \
  projects/PROJECT_ID/topics/migration.requested

# BigQuery datasets
terraform import google_bigquery_dataset.migration_target PROJECT_ID/migration_target
terraform import google_bigquery_dataset.billing_export PROJECT_ID/billing_export

# Reports bucket
terraform import google_storage_bucket.reports PROJECT_ID-migration-control-tower-reports
```

After importing everything, run `terraform plan` and read it in full — it should show **no
changes** (or only benign metadata diffs) for imported resources. A plan showing it wants to
*recreate* an imported resource means the config doesn't match reality yet; fix the config, don't
apply blindly.

## Safe rollout sequence

1. `terraform plan` with defaults (`deploy_cloud_run_services = false`, `enable_cloud_sql = false`,
   `enable_data_plane_job = false`) — this touches only foundational, cheap-or-free resources (APIs,
   IAM, Pub/Sub, BigQuery datasets, the reports bucket, Artifact Registry). Review, then `apply`.
2. Build and push at least one real image per service to the Artifact Registry repo this module
   just created (`outputs.cloud_run_service_urls` will show empty until step 3).
3. Set `service_image_tags` for every service, flip `deploy_cloud_run_services = true`, `plan`
   again — this is the step that creates real, billed, running services. Review the plan
   specifically for `google_cloud_run_v2_service` and `google_pubsub_subscription.push` before
   applying.
4. Per-service `gcloud run services update <name> --update-env-vars SERVICE_AUDIENCE=$(gcloud run
   services describe <name> --format='value(status.url)')` — see `cloud_run.tf`'s comment on why
   this can't be done inside the same `apply` that creates the service.
5. Only once a Postgres pricing tier is confirmed (Phase 3b's checkpoint): `enable_cloud_sql = true`.
   Then apply the read-only-user SQL migration referenced in `cloud_sql.tf`'s comment (Cloud SQL's
   own user resource has no built-in read-only role — the actual `REVOKE`/`GRANT SELECT` statements
   are a separate `psql` step, not Terraform).
6. Only once a data-plane job image is built: `enable_data_plane_job = true` (Phase 3 — this
   resource isn't defined in this module yet; added when Phase 3 lands).

## Direct VPC egress note (Cloud SQL)

`cloud_sql.tf`'s `private_network = null` is a placeholder — Cloud Run reaching a private-IP Cloud
SQL instance needs either the Cloud SQL Auth Proxy sidecar pattern or Direct VPC egress configured
on each Cloud Run service's `template.vpc_access` block (not yet added to `cloud_run.tf`). Wire this
before `enable_cloud_sql = true` actually gets used by `tools/data_plane_executors/cloud_run_job_executor.py`.

## Teardown

`terraform destroy` for everything this module manages. `infrastructure/teardown.sh` still exists
for anything intentionally left outside Terraform's state (see its own header) or as a fast
off-switch if Terraform itself is unavailable.
