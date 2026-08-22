# Deploy & Harden Phase 2a. Defaults chosen so a first `terraform apply`
# with no overrides creates only foundational, low-cost resources —
# every genuinely expensive or hard-to-reverse resource is gated behind
# an explicit `enable_*`/`deploy_*` flag defaulting to false. See each
# variable's description for exactly what it gates and why.

variable "project_id" {
  type        = string
  description = "GCP project — this repo's default dev/demo project is autonomous-data-migration."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "bq_dataset" {
  type    = string
  default = "migration_target"
}

variable "billing_export_dataset" {
  type    = string
  default = "billing_export"
}

variable "reports_bucket_suffix" {
  type    = string
  default = "migration-control-tower-reports"
}

variable "artifact_registry_repo" {
  type    = string
  default = "control-tower"
}

# -- Per-agent service accounts (matches infrastructure/gcp_setup.sh's
#    existing SA_* names — Terraform importing these, not recreating
#    them, is the intended first apply; see the module README). --------
variable "agent_service_account_ids" {
  type        = list(string)
  default     = ["sa-orchestrator", "sa-discovery", "sa-lineage", "sa-risk", "sa-planner", "sa-validation", "sa-cutover", "sa-finance-impact"]
  description = "One IAM service account per deployable service (Deploy & Harden Phase 2b's 9-service topology minus frontend, which reuses sa-orchestrator, matching the existing frontend/Dockerfile deploy comment)."
}

variable "pubsub_invoker_service_account_id" {
  type        = string
  default     = "sa-pubsub-invoker"
  description = "Dedicated identity Pub/Sub push subscriptions authenticate as when calling a Cloud Run service — distinct from any agent's own workload SA (docs/THREAT_MODEL.md's elevation-of-privilege mitigation: a compromised push endpoint can't be traced back to an over-broad workload credential)."
}

# -- Pub/Sub -----------------------------------------------------------

variable "pubsub_topics" {
  type = list(string)
  default = [
    "migration.requested", "discovery.completed", "risk.assessed", "risk.blocked",
    "plan.created", "plan.approved", "validation.requested", "migration.completed",
    "validation.failed", "validation.passed", "cutover.approved", "cutover.completed",
    "assessment.requested", "wave.override.requested",
  ]
  description = "Matches infrastructure/gcp_setup.sh's TOPICS array (docs/ARCHITECTURE.md) — kept in sync by hand until gcp_setup.sh is retired in favor of this module. migration.completed is the Deploy & Harden Phase 3 async data-plane job completion event (docs/adr/0003), no longer 'reserved, unused' as gcp_setup.sh's comment once said."
}

variable "max_delivery_attempts" {
  type    = number
  default = 10
}

variable "ack_deadline_seconds" {
  type    = number
  default = 60
}

# -- Cloud Run: gated, not automatic ------------------------------------

variable "deploy_cloud_run_services" {
  type        = bool
  default     = false
  description = <<-EOT
    Gates creation of all 9 Cloud Run services. Defaults false because a
    google_cloud_run_v2_service resource requires a real container image
    to already exist in Artifact Registry — the FIRST apply with this
    true must happen only after at least one successful image build/push
    (manual, or via the CI/CD workflow once its one-time WIF bootstrap is
    done — see infrastructure/terraform/README.md). Flipping this on
    creates real, billed, publicly-routable-unless-configured resources.
  EOT
}

variable "service_image_tags" {
  type        = map(string)
  default     = {}
  description = "capability/service name -> full image reference (e.g. 'us-central1-docker.pkg.dev/PROJECT/control-tower/discovery-agent@sha256:...'). Required per service when deploy_cloud_run_services=true — deliberately no default, so a missing entry fails the plan rather than deploying a placeholder image."
}

variable "cloud_run_max_instances" {
  type    = number
  default = 5
}

variable "firebase_web_config" {
  type = object({
    api_key             = string
    auth_domain         = string
    project_id          = string
    storage_bucket      = string
    messaging_sender_id = string
    app_id              = string
  })
  default     = null
  description = <<-EOT
    Firebase Web SDK config for control-tower-ui, matching what
    frontend/api_v1.py::_firebase_web_config() reads
    (FIREBASE_API_KEY/FIREBASE_AUTH_DOMAIN/FIREBASE_PROJECT_ID/
    FIREBASE_STORAGE_BUCKET/FIREBASE_MESSAGING_SENDER_ID/FIREBASE_APP_ID).
    These are NOT secrets — the Firebase client SDK config is safe to
    expose (restricted by Firebase Security Rules and API key
    restrictions, not by secrecy — see firestore.rules's own header
    comment on this exact point) — but there is no sane cross-project
    default, so this stays null (Firebase auth shows as "not
    configured" in the UI, matching frontend/client's own honest
    warning state) until a real project's values are supplied. Discovered
    live (Deploy & Harden Phase 5 close-out): before this variable
    existed, the only place a real config lived was the gitignored
    frontend/static/firebase-config.js, which never made it into any
    container image — so control-tower-ui's live Firebase auth was
    silently unconfigured despite a real project already being set up.
  EOT
}

# -- Cloud SQL (Deploy & Harden Phase 3): gated, not automatic ----------

variable "enable_cloud_sql" {
  type        = bool
  default     = false
  description = "Gates the Cloud SQL for PostgreSQL instance (Phase 3's data-plane demo source). Defaults false — provisioning it is a real, continuously-billed resource; confirm actual pricing for the chosen tier first, per the Deploy & Harden plan's Phase 3b checkpoint."
}

variable "cloud_sql_tier" {
  type    = string
  default = "db-f1-micro"
}

# -- Cloud Run Job (Deploy & Harden Phase 3): gated, not automatic ------

variable "enable_data_plane_job" {
  type        = bool
  default     = false
  description = "Gates the CloudRunJobExecutor's Cloud Run Job (Phase 3) — same reasoning as deploy_cloud_run_services: needs a real built image first."
}

variable "data_plane_job_image_tag" {
  type    = string
  default = ""
}

# -- Monitoring ----------------------------------------------------------

variable "alert_notification_channels" {
  type        = list(string)
  default     = []
  description = "Existing Cloud Monitoring notification channel IDs (email/Slack/etc) to attach alert policies to. Empty means alert policies are created but fire nowhere until at least one channel is added — intentionally not auto-creating a channel here, since that usually means an email address this module shouldn't guess."
}
