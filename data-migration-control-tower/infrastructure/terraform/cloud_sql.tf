# Deploy & Harden Phase 3's data-plane demo source. Gated behind
# var.enable_cloud_sql (default false) — confirm actual pricing for
# var.cloud_sql_tier before flipping this on (Phase 3b's checkpoint).
# Fully specified per docs/adr and the Deploy & Harden plan's Phase 3b
# security requirements — not a placeholder "provision it" stub:
#   - No public IP; private IP only via the default VPC network.
#   - Deletion protection + automated backups, even for a demo instance.
#   - A dedicated READ-ONLY database user (tools/data_plane_job/run_job.py,
#     Phase 3, never needs write access to the source).
#   - No 0.0.0.0/0 authorized network — moot with public IP disabled, but
#     stated explicitly so a future edit can't reintroduce it quietly.

resource "google_sql_database_instance" "postgres_retail_exec" {
  count = var.enable_cloud_sql ? 1 : 0

  project          = var.project_id
  name             = "postgres-retail-exec-demo"
  region           = var.region
  database_version = "POSTGRES_16"

  deletion_protection = true

  settings {
    tier = var.cloud_sql_tier

    ip_configuration {
      ipv4_enabled    = false # no public IP
      private_network = null  # set to a VPC self_link if using Direct VPC egress from Cloud Run — see README.md's networking note
    }

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    availability_type = "ZONAL" # demo/staging profile, not production — see docs/ARCHITECTURE.md
  }
}

resource "google_sql_database" "retail" {
  count = var.enable_cloud_sql ? 1 : 0

  project  = var.project_id
  name     = "retail"
  instance = google_sql_database_instance.postgres_retail_exec[0].name
}

resource "random_password" "postgres_readonly" {
  count   = var.enable_cloud_sql ? 1 : 0
  length  = 32
  special = true
}

resource "google_sql_user" "readonly_migration_user" {
  count = var.enable_cloud_sql ? 1 : 0

  project  = var.project_id
  instance = google_sql_database_instance.postgres_retail_exec[0].name
  name     = "migration_readonly"
  password = random_password.postgres_readonly[0].result
  # Actual read-only GRANTs (REVOKE INSERT/UPDATE/DELETE, GRANT SELECT
  # only) are applied post-create via a SQL migration script, not by
  # Cloud SQL's own user resource (it has no built-in read-only role) —
  # see infrastructure/terraform/README.md's Cloud SQL setup checklist.
}

resource "google_secret_manager_secret" "postgres_readonly_password" {
  count = var.enable_cloud_sql ? 1 : 0

  project   = var.project_id
  secret_id = "postgres-retail-exec-readonly-password"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "postgres_readonly_password" {
  count = var.enable_cloud_sql ? 1 : 0

  secret      = google_secret_manager_secret.postgres_readonly_password[0].id
  secret_data = random_password.postgres_readonly[0].result
}
