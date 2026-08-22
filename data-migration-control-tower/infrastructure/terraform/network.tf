# Private connectivity for Cloud SQL (Deploy & Harden Phase 3 close-out).
# cloud_sql.tf's instance has always declared "no public IP, private
# network only" as the intent, but `private_network = null` alone gives
# it no actual route — Cloud SQL private IP needs a real VPC Peering
# connection to Google's service producer network (Private Services
# Access), and anything reaching it (tools/data_plane_job/run_job.py's
# raw `psycopg.connect(host=...)`, not the Cloud SQL Python Connector —
# see tools/adapters/postgres_adapter.py) needs a network path there too,
# via Direct VPC egress on the Cloud Run Job that connects
# (data_plane_job.tf). Discovered live (Deploy & Harden Phase 5
# close-out) reviewing cloud_sql.tf before ever flipping enable_cloud_sql
# on — this was a real, would-have-failed-at-connect-time gap, not a
# hypothetical one.
#
# Uses the project's default auto-mode VPC network/subnet rather than
# creating a new one — this is a single dev/demo project (docs/ARCHITECTURE.md's
# "one Terraform mistake affects the only environment there is" note),
# so a dedicated VPC would add operational surface without a real
# isolation benefit here.

data "google_compute_network" "default" {
  count = var.enable_cloud_sql ? 1 : 0

  project = var.project_id
  name    = "default"
}

data "google_compute_subnetwork" "default" {
  count = var.enable_cloud_sql ? 1 : 0

  project = var.project_id
  region  = var.region
  name    = "default"
}

resource "google_compute_global_address" "private_services_access" {
  count = var.enable_cloud_sql ? 1 : 0

  project       = var.project_id
  name          = "cloud-sql-private-services-access"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 16
  network       = data.google_compute_network.default[0].id
}

resource "google_service_networking_connection" "private_services_access" {
  count = var.enable_cloud_sql ? 1 : 0

  network                 = data.google_compute_network.default[0].id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_services_access[0].name]
}
