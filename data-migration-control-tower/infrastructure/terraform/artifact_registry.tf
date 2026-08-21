# One Docker repository holding every service's images (Deploy & Harden
# Phase 2b/2e — CI/CD pushes here, `google_cloud_run_v2_service` below
# pulls from here).

resource "google_artifact_registry_repository" "control_tower" {
  project       = var.project_id
  location      = var.region
  repository_id = var.artifact_registry_repo
  format        = "DOCKER"
  description   = "Deploy & Harden Phase 2 — images for all 9 Cloud Run services + the data-plane job."
}
