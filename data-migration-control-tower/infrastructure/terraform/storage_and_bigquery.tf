# Matches infrastructure/gcp_setup.sh's BigQuery dataset + reports bucket
# provisioning. The actual Cloud Billing export LINKAGE (billing account
# -> this dataset) stays a manual Cloud Console step regardless — see
# tools/billing_export.py's docstring and docs/RUNBOOK.md's "Actual
# cost — needs you" section; Terraform can create the empty dataset but
# not flip that billing-account-level switch.

resource "google_bigquery_dataset" "migration_target" {
  project    = var.project_id
  dataset_id = var.bq_dataset
  location   = var.region
}

resource "google_bigquery_dataset" "billing_export" {
  project    = var.project_id
  dataset_id = var.billing_export_dataset
  # Billing export refuses single-region datasets — must be a US/EU
  # multi-region (matches gcp_setup.sh's own comment on this).
  location = "US"
}

resource "google_storage_bucket" "reports" {
  project  = var.project_id
  name     = "${var.project_id}-${var.reports_bucket_suffix}"
  location = var.region

  uniform_bucket_level_access = true
  force_destroy               = false

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  # Private by construction — no public IAM binding is created anywhere
  # in this module. docs/THREAT_MODEL.md's "private report bucket"
  # mitigation (Phase 5) means confirming this stays true, not adding it.
}
