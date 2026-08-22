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

  # Private Services Access peering (network.tf) must exist before Cloud
  # SQL can actually attach a private IP to it — without this explicit
  # dependency Terraform could try to create the instance and the
  # peering connection in either order.
  depends_on = [google_service_networking_connection.private_services_access]

  settings {
    tier = var.cloud_sql_tier

    ip_configuration {
      ipv4_enabled    = false # no public IP
      private_network = data.google_compute_network.default[0].id # network.tf
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

# sa-orchestrator reads this — not sa-discovery — because Discovery's
# AgentCard is still runtime.type=local, so its code runs in-process
# inside the orchestrator (see cloud_run.tf's vpc_access comment on the
# same point). Re-scope this to sa-discovery instead once that AgentCard
# is flipped to runtime.type=cloud_run and dispatch genuinely moves to
# discovery-agent's own deployed service.
resource "google_secret_manager_secret_iam_member" "orchestrator_reads_postgres_readonly_password" {
  count = var.enable_cloud_sql ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.postgres_readonly_password[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent["sa-orchestrator"].email}"
}

# sa-cutover reads this too -- unlike every other agent, Cutover genuinely
# runs as its own deployed service (confirmed live 2026-08-22: cutover.
# approved is processed by cutover-agent's own Cloud Run revision, under
# sa-cutover's own identity), so it needs its own grant rather than
# inheriting sa-orchestrator's. Post-cutover monitoring
# (agents/cutover/agent.py::trigger_post_cutover_monitoring) resolves this
# same secret to build a source adapter for its health check.
resource "google_secret_manager_secret_iam_member" "cutover_reads_postgres_readonly_password" {
  count = var.enable_cloud_sql ? 1 : 0

  project   = var.project_id
  secret_id = google_secret_manager_secret.postgres_readonly_password[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent["sa-cutover"].email}"
}

# Bootstrap superuser credential (Deploy & Harden Phase 5 close-out) —
# used exactly once, by tools/data_plane_job/bootstrap_retail_db.py via
# the db_bootstrap job below, to create the retail schema/sample data
# and apply the actual REVOKE/GRANT this file's own header comment says
# "is applied post-create via a SQL migration script." Never handed to
# any agent SA — only the bootstrap job's own SA reads it.
resource "random_password" "postgres_superuser" {
  count   = var.enable_cloud_sql ? 1 : 0
  length  = 32
  special = true
}

resource "google_sql_user" "postgres_superuser" {
  count = var.enable_cloud_sql ? 1 : 0

  project  = var.project_id
  instance = google_sql_database_instance.postgres_retail_exec[0].name
  name     = "postgres"
  password = random_password.postgres_superuser[0].result
}

resource "google_secret_manager_secret" "postgres_superuser_password" {
  count = var.enable_cloud_sql ? 1 : 0

  project   = var.project_id
  secret_id = "postgres-retail-exec-superuser-password"

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "postgres_superuser_password" {
  count = var.enable_cloud_sql ? 1 : 0

  secret      = google_secret_manager_secret.postgres_superuser_password[0].id
  secret_data = random_password.postgres_superuser[0].result
}
