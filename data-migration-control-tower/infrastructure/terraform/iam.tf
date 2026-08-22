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

  # Matches gcp_setup.sh's AGENT_SAS map exactly — importing these
  # already-existing SAs (README.md's import checklist) must not
  # overwrite each one's real display name with its bare account_id;
  # that regression was caught in a `terraform plan` dry run before the
  # first apply (`google_service_account.agent[*] display_name "Risk &
  # Compliance Agent" -> "sa-risk"`, etc.) and fixed here instead of
  # applied.
  agent_display_names = {
    "sa-orchestrator"   = "Migration Orchestrator (hello-agent, Day 1)"
    "sa-discovery"      = "Discovery Agent"
    "sa-lineage"        = "Lineage Agent"
    "sa-risk"           = "Risk & Compliance Agent"
    "sa-planner"        = "Migration Planner"
    "sa-validation"     = "Validation & Reconciliation Agent"
    "sa-cutover"        = "Cutover Agent"
    "sa-finance-impact" = "Finance Reporting Impact Agent (Finance Systems dept)"
  }
}

resource "google_service_account" "agent" {
  for_each = toset(var.agent_service_account_ids)

  project    = var.project_id
  account_id = each.value
  # Falls back to the bare account_id only for an SA this map doesn't
  # name explicitly (e.g. a future addition to agent_service_account_ids)
  # rather than erroring — better a plain-but-harmless display name than
  # a failed apply.
  display_name = lookup(local.agent_display_names, each.value, each.value)
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

# Discovered live (Deploy & Harden Phase 5 close-out): no agent SA had
# this at all, so every deployed service's tools/tracing.py span export
# failed with a 403 on every single request — not a propagation gap
# (docs/EVALUATION.md's already-known caveat about traceparent wiring),
# a total absence of write access. Baseline, same as datastore/pubsub
# above, since every service exports spans.
resource "google_project_iam_member" "agent_cloudtrace_agent" {
  for_each = toset(var.agent_service_account_ids)

  project = var.project_id
  role    = "roles/cloudtrace.agent"
  member  = "serviceAccount:${google_service_account.agent[each.value].email}"
}

# sa-orchestrator additionally gets pubsub.subscriber + bigquery.dataEditor
# + bigquery.jobUser + aiplatform.user (it consumes every topic and,
# pre-Phase-2c dynamic import, transitively needs what discovery/
# validation need too). bigquery.jobUser found missing live
# (2026-08-22): dataEditor alone lets a role write to a dataset once a
# query job exists, but does not include bigquery.jobs.create -- the
# separate permission needed to start one. Validation reconciliation
# (agents/validation/agent.py -> tools/bigquery_tools.py::get_row_count)
# runs in-process under sa-orchestrator, same as Discovery, and issues
# real BigQuery queries; every reconciliation check failed with
# Forbidden until this was added, having gotten all the way to
# VALIDATING for the first time only because the data-plane job's own,
# separate copy of this exact gap (see data_plane_job.tf) was just
# fixed first.
resource "google_project_iam_member" "orchestrator_extra" {
  for_each = toset([
    "roles/pubsub.subscriber", "roles/bigquery.dataEditor",
    "roles/bigquery.jobUser", "roles/aiplatform.user",
  ])

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

# Kept alongside dataEditor above for these SAs' own identities too
# (sa-validation, sa-cutover) even though today's in-process dispatch
# means sa-orchestrator's own copy (above) is what actually matters
# live -- once any of these flips to real cloud_run dispatch
# (docs/adr/0002), it would hit the exact same missing-jobUser gap that
# orchestrator_extra's own comment documents, silently, again.
resource "google_project_iam_member" "bigquery_job_user_agents" {
  for_each = toset(local.bigquery_editor_agent_sas)

  project = var.project_id
  role    = "roles/bigquery.jobUser"
  member  = "serviceAccount:${google_service_account.agent[each.value].email}"
}
