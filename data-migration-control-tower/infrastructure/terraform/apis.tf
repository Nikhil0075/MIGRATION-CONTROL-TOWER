# Matches infrastructure/gcp_setup.sh's `gcloud services enable` step.
# disable_on_destroy=false — disabling an API on `terraform destroy`
# would break anything else in the project still using it (this project
# deploys into its existing dev project, not an isolated one — see
# docs/ARCHITECTURE.md's "staging deployment note").

locals {
  required_apis = [
    "run.googleapis.com",
    "firestore.googleapis.com",
    "pubsub.googleapis.com",
    "bigquery.googleapis.com",
    "bigquerystorage.googleapis.com",
    "aiplatform.googleapis.com",
    "storage.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "sqladmin.googleapis.com",
    "cloudtrace.googleapis.com",
    "monitoring.googleapis.com",
    "logging.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "cloudbilling.googleapis.com",
    "billingbudgets.googleapis.com",
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_apis)

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}
