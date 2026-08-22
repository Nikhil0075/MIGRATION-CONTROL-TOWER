output "agent_service_account_emails" {
  value = { for k, sa in google_service_account.agent : k => sa.email }
}

output "pubsub_invoker_service_account_email" {
  value = google_service_account.pubsub_invoker.email
}

output "artifact_registry_repo_url" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.control_tower.repository_id}"
}

output "reports_bucket_name" {
  value = google_storage_bucket.reports.name
}

output "cloud_run_service_urls" {
  value = var.deploy_cloud_run_services ? { for k, s in google_cloud_run_v2_service.service : k => s.uri } : {}
}

output "cloud_sql_connection_name" {
  value = var.enable_cloud_sql ? google_sql_database_instance.postgres_retail_exec[0].connection_name : null
}

output "cloud_sql_private_ip_address" {
  value       = var.enable_cloud_sql ? google_sql_database_instance.postgres_retail_exec[0].private_ip_address : null
  description = "The value tools/data_plane_job/run_job.py's PostgresAdapter needs as connection_profile.host on the postgres_retail_exec_v1 pack's estate binding — set post-apply, same manual-step reasoning as SERVICE_AUDIENCE (README.md's post-deploy checklist)."
}
