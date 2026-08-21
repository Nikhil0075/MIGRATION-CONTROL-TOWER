# The async data-plane executor's Cloud Run Job (Deploy & Harden Phase 3,
# docs/adr/0003-async-data-plane-job.md). Gated behind
# var.enable_data_plane_job (default false) — needs a real built image
# first (tools/data_plane_job/Dockerfile), same reasoning as
# deploy_cloud_run_services.
#
# Its own narrowly-scoped service account, distinct from every agent SA
# (docs/GOVERNANCE.md's least-privilege pattern applied to the job too):
# Cloud SQL Client, target-BigQuery-dataset writer only, write access to
# its own execution documents (not project-wide Firestore), Pub/Sub
# publisher for migration.completed only, and only the Secret Manager
# references it needs.

resource "google_service_account" "data_plane_job" {
  count = var.enable_data_plane_job || var.enable_cloud_sql ? 1 : 0

  project      = var.project_id
  account_id   = "sa-data-plane-job"
  display_name = "Async data-plane Cloud Run Job (Deploy & Harden Phase 3)"
}

resource "google_project_iam_member" "data_plane_job_datastore_user" {
  count = var.enable_data_plane_job || var.enable_cloud_sql ? 1 : 0

  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.data_plane_job[0].email}"
}

resource "google_project_iam_member" "data_plane_job_bigquery_editor" {
  count = var.enable_data_plane_job || var.enable_cloud_sql ? 1 : 0

  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.data_plane_job[0].email}"
}

resource "google_project_iam_member" "data_plane_job_pubsub_publisher" {
  count = var.enable_data_plane_job || var.enable_cloud_sql ? 1 : 0

  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.data_plane_job[0].email}"
}

resource "google_project_iam_member" "data_plane_job_cloudsql_client" {
  count = var.enable_data_plane_job || var.enable_cloud_sql ? 1 : 0

  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.data_plane_job[0].email}"
}

resource "google_secret_manager_secret_iam_member" "data_plane_job_reads_postgres_password" {
  count = var.enable_cloud_sql ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.postgres_readonly_password[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.data_plane_job[0].email}"
}

resource "google_cloud_run_v2_job" "data_plane" {
  count = var.enable_data_plane_job ? 1 : 0

  project  = var.project_id
  name     = "migration-data-plane"
  location = var.region

  template {
    template {
      service_account = google_service_account.data_plane_job[0].email
      max_retries      = 0 # a failed job publishes migration.completed with status=FAILED
                            # itself (tools/data_plane_job/run_job.py) — a platform-level
                            # retry would run it twice without the orchestrator knowing,
                            # since handle_migration_completed's counter already decremented.

      containers {
        image = var.data_plane_job_image_tag # deliberately no default, same reasoning as service_image_tags
      }
    }
  }
}
