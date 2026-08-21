# Reproduces infrastructure/gcp_setup.sh's exact per-agent role grants
# (§19's Rung 2: "one Google service account per agent, with IAM
# bindings that genuinely differ") as declarative resources. If these
# service accounts already exist (they do, in the dev project this
# module targets), import them first — see README.md's import checklist
# — rather than letting `terraform apply` fail on "already exists" or,
# worse, silently adopt a resource it didn't create.
#
# Fine-grained tool/action permission enforcement is still the
# deterministic policy engine's job (tools/policy_engine.py against
# policies/agent_permissions.yaml), never IAM — see docs/GOVERNANCE.md.
# IAM here only establishes distinct, attributable identity per agent.

locals {
  # sa-orchestrator is handled separately below (it also gets bigquery +
  # storage roles gcp_setup.sh's dedicated block grants it, on top of the
  # baseline every other agent SA gets).
  non_orchestrator_agent_sas = [for sa in var.agent_service_account_ids : sa if sa != "sa-orchestrator"]

  vertex_ai_agent_sas   = ["sa-discovery", "sa-lineage", "sa-planner"]
  bigquery_editor_agent_sas = ["sa-validation", "sa-cutover"]
}

resource "google_service_account" "agent" {
  for_each = toset(var.agent_service_account_ids)

  project      = var.project_id
  account_id   = each.value
  display_name = each.value
}

resource "google_service_account" "pubsub_invoker" {
  project      = var.project_id
  account_id   = var.pubsub_invoker_service_account_id
  display_name = "Pub/Sub push-subscription invoker (Deploy & Harden Phase 2)"
}

# -- Baseline roles for every agent SA (Firestore + Pub/Sub publish) ----

resource "google_project_iam_member" "agent_datastore_user" {
  for_each = toset(var.agent_service_account_ids)

  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.agent[each.value].email}"
}

resource "google_project_iam_member" "agent_pubsub_publisher" {
  for_each = toset(var.agent_service_account_ids)

  project = var.project_id
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${google_service_account.agent[each.value].email}"
}

# sa-orchestrator additionally gets pubsub.subscriber + bigquery.dataEditor
# + aiplatform.user (it consumes every topic and, pre-Phase-2c dynamic
# import, transitively needs what discovery/validation need too).

resource "google_project_iam_member" "orchestrator_extra" {
  for_each = toset(["roles/pubsub.subscriber", "roles/bigquery.dataEditor", "roles/aiplatform.user"])

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent["sa-orchestrator"].email}"
}

resource "google_storage_bucket_iam_member" "orchestrator_reports_bucket" {
  for_each = toset(["roles/storage.objectCreator", "roles/storage.objectViewer"])

  bucket = google_storage_bucket.reports.name
  role   = each.value
  member = "serviceAccount:${google_service_account.agent["sa-orchestrator"].email}"
}

# -- Vertex AI: only agents with bounded model reasoning -----------------

resource "google_project_iam_member" "vertex_ai_agents" {
  for_each = toset(local.vertex_ai_agent_sas)

  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.agent[each.value].email}"
}

# -- BigQuery: validation (reconciliation) + cutover (post-cutover monitoring) --

resource "google_project_iam_member" "bigquery_editor_agents" {
  for_each = toset(local.bigquery_editor_agent_sas)

  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.agent[each.value].email}"
}
