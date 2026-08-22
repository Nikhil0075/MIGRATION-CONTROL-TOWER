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

      # Direct VPC egress (network.tf) — needed only when this job's
      # actual target is the Cloud SQL demo source: PostgresAdapter
      # connects via raw `psycopg.connect(host=...)` (tools/adapters/postgres_adapter.py),
      # not the Cloud SQL Python Connector, so it needs a real network
      # route to Cloud SQL's private IP, not just Cloud SQL Admin API
      # access (which roles/cloudsql.client alone would grant).
      # PRIVATE_RANGES_ONLY keeps this job's Firestore/Pub/Sub/BigQuery
      # calls on the public internet path instead of forcing all
      # egress through the VPC.
      dynamic "vpc_access" {
        for_each = var.enable_cloud_sql ? [1] : []
        content {
          network_interfaces {
            network    = data.google_compute_network.default[0].id
            subnetwork = data.google_compute_subnetwork.default[0].id
          }
          egress = "PRIVATE_RANGES_ONLY"
        }
      }
    }
  }
}

# One-off schema/data/grants bootstrap (Deploy & Harden Phase 5 close-out
# — "wire up the Cloud SQL discovery path"). Its own dedicated SA, never
# shared with the data-plane job's own SA above: this is the only thing
# in the whole deployment that ever reads the postgres superuser
# password, and least-privilege means that access stays scoped to
# exactly this one job. See tools/data_plane_job/bootstrap_retail_db.py
# for what it actually does — idempotent, safe to re-run.
resource "google_service_account" "db_bootstrap" {
  count = var.enable_cloud_sql ? 1 : 0

  project      = var.project_id
  account_id   = "sa-db-bootstrap"
  display_name = "One-off Cloud SQL schema/grants bootstrap (Deploy & Harden Phase 5)"
}

resource "google_project_iam_member" "db_bootstrap_cloudsql_client" {
  count = var.enable_cloud_sql ? 1 : 0

  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.db_bootstrap[0].email}"
}

resource "google_secret_manager_secret_iam_member" "db_bootstrap_reads_superuser_password" {
  count = var.enable_cloud_sql ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.postgres_superuser_password[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.db_bootstrap[0].email}"
}

resource "google_cloud_run_v2_job" "db_bootstrap" {
  count = var.enable_cloud_sql ? 1 : 0

  project  = var.project_id
  name     = "postgres-retail-db-bootstrap"
  location = var.region

  template {
    template {
      service_account = google_service_account.db_bootstrap[0].email
      max_retries      = 0
      # Reuses the data-plane job's own image (same Dockerfile, same
      # psycopg dependency) — only the command/args differ, so no
      # separate image build is needed for this one-off job.
      containers {
        image   = var.data_plane_job_image_tag
        command = ["python"]
        args    = ["-m", "tools.data_plane_job.bootstrap_retail_db"]

        env {
          name  = "POSTGRES_HOST"
          value = google_sql_database_instance.postgres_retail_exec[0].private_ip_address
        }
        env {
          name  = "POSTGRES_PORT"
          value = "5432"
        }
        env {
          name  = "POSTGRES_DATABASE"
          value = google_sql_database.retail[0].name
        }
        # Cloud Run's native Secret Manager env-var binding — the value
        # never appears in this job's own config (`gcloud run jobs
        # describe` shows only the secret reference, not the secret
        # itself), unlike a plain `env { value = ... }` would.
        env {
          name = "POSTGRES_SUPERUSER_PASSWORD"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.postgres_superuser_password[0].secret_id
              version = "latest"
            }
          }
        }
      }

      vpc_access {
        network_interfaces {
          network    = data.google_compute_network.default[0].id
          subnetwork = data.google_compute_subnetwork.default[0].id
        }
        egress = "PRIVATE_RANGES_ONLY"
      }
    }
  }
}
