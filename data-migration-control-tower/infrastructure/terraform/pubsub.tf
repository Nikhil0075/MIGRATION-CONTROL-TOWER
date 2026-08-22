# Matches infrastructure/gcp_setup.sh's Pub/Sub provisioning (§8.1 event
# topics), plus the new push-subscription wiring Deploy & Harden Phase 2b
# needs once deploy_cloud_run_services=true. Dead-lettering matches
# gcp_setup.sh's existing policy (10 delivery attempts).

resource "google_pubsub_topic" "topic" {
  for_each = toset(var.pubsub_topics)

  project = var.project_id
  name    = each.value
}

resource "google_pubsub_topic" "dead_letter" {
  project = var.project_id
  name    = "dead-letter"
}

# Pub/Sub's own service agent needs publish rights on the dead-letter
# topic to actually forward failed messages there.
data "google_project" "current" {
  project_id = var.project_id
}

resource "google_pubsub_topic_iam_member" "pubsub_service_agent_dead_letter_publisher" {
  project = var.project_id
  topic   = google_pubsub_topic.dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# -- Pull subscriptions (unchanged from today's shape — kept for any
#    consumer not yet split onto its own Cloud Run service, and for
#    local dev via tools/worker_supervisor.py) -----------------------

locals {
  # subscription_name -> topic — matches
  # tools/worker_supervisor.py::default_specs()'s ConsumerSpec list.
  pull_subscriptions = {
    "assessment-requested-sub"      = "assessment.requested"
    "migration-requested-sub"       = "migration.requested"
    "discovery-completed-sub"       = "discovery.completed"
    "risk-assessed-sub"             = "risk.assessed"
    "plan-created-sub"              = "plan.created"
    "validation-requested-sub"      = "validation.requested"
    "approval-preparation-sub"      = "validation.passed"
    "validation-failed-sub"         = "validation.failed"
    "cutover-approved-sub"          = "cutover.approved"
    # validation-passed-sub deliberately NOT created as a competing
    # consumer here — advance_through_validation's assertion-only
    # subscription is test/eval scoped, not part of the deployed
    # topology (see tools/worker_supervisor.py's own docstring on this).
    # Deploy & Harden Phase 3 (docs/adr/0003) — the async data-plane
    # job's completion event.
    "migration-completed-sub"       = "migration.completed"
  }
}

resource "google_pubsub_subscription" "pull" {
  for_each = local.pull_subscriptions

  project = var.project_id
  name    = each.key
  topic   = google_pubsub_topic.topic[each.value].name

  ack_deadline_seconds = var.ack_deadline_seconds

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }
}

resource "google_pubsub_subscription" "dead_letter_sub" {
  project = var.project_id
  name    = "dead-letter-sub"
  topic   = google_pubsub_topic.dead_letter.name
}

# -- Push subscriptions (Deploy & Harden Phase 2b): one per consumer the
#    orchestrator/discovery/cutover Cloud Run services own, only created
#    once those services actually exist (deploy_cloud_run_services=true).
#    Wiring this in the SAME Terraform config as the services avoids the
#    two-phase "deploy, then wire subscriptions" chicken-and-egg problem
#    a hand-run gcloud script would have — Terraform resolves the
#    dependency ordering (service URL -> push endpoint) automatically. --

locals {
  # push subscription name -> {topic, path on the owning service}.
  #
  # Every path below ends in a trailing slash — discovered live (Deploy
  # & Harden Phase 5 close-out): agents/orchestrator/service_main.py
  # (and the discovery/cutover equivalents) each `app.mount("/push/NAME",
  # sub_app)` a sub-app whose own route is registered at "/". Starlette's
  # default `redirect_slashes=True` means a request to the BARE mount
  # path (no trailing slash) 307-redirects to add one, before any
  # handler or auth code ever runs. Pub/Sub's push delivery does not
  # follow redirects, so every single delivery to a path missing the
  # trailing slash failed silently forever — no error surfaced anywhere
  # except a 307 in the Cloud Run request log, Pub/Sub just kept
  # redelivering (and redelivering, and redelivering) until
  # max_delivery_attempts. Confirmed locally with FastAPI's TestClient
  # before touching the live config: POST /push/migration -> 307;
  # POST /push/migration/ -> reaches the real auth check. Every one of
  # these 10 paths had the same bug, not just this one.
  push_targets = {
    "migration-requested-push"  = { topic = "migration.requested", service = "orchestrator", path = "/push/migration/" }
    "discovery-completed-push"  = { topic = "discovery.completed", service = "orchestrator", path = "/push/discovery/" }
    "risk-assessed-push"        = { topic = "risk.assessed", service = "orchestrator", path = "/push/risk/" }
    "plan-created-push"         = { topic = "plan.created", service = "orchestrator", path = "/push/plan/" }
    "validation-requested-push" = { topic = "validation.requested", service = "orchestrator", path = "/push/validation/" }
    "validation-passed-push"    = { topic = "validation.passed", service = "orchestrator", path = "/push/approval/" }
    "validation-failed-push"    = { topic = "validation.failed", service = "orchestrator", path = "/push/recovery/" }
    "assessment-requested-push" = { topic = "assessment.requested", service = "discovery-agent", path = "/push/assessment/" }
    "cutover-approved-push"     = { topic = "cutover.approved", service = "cutover-agent", path = "/push/cutover/" }
    # Deploy & Harden Phase 3 (docs/adr/0003) — the async data-plane
    # job's completion event, owned by the orchestrator like its other
    # 6 state-machine-step consumers.
    "migration-completed-push"  = { topic = "migration.completed", service = "orchestrator", path = "/push/migrationcompleted/" }
  }
}

resource "google_pubsub_subscription" "push" {
  for_each = var.deploy_cloud_run_services ? local.push_targets : {}

  project = var.project_id
  name    = each.key
  topic   = google_pubsub_topic.topic[each.value.topic].name

  ack_deadline_seconds = var.ack_deadline_seconds

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.service[each.value.service].uri}${each.value.path}"
    oidc_token {
      service_account_email = google_service_account.pubsub_invoker.email
      # Explicit, not defaulted — discovered live (Deploy & Harden Phase
      # 5 close-out), the second bug this same investigation found.
      # Left unset, GCP defaults the OIDC token's `aud` claim to the
      # full push_endpoint (including this route's own path), which
      # tools/capability_http_server.py::verify_caller_identity() then
      # checks via id_token.verify_oauth2_token(..., audience=SERVICE_AUDIENCE).
      # A single Cloud Run service (the orchestrator) owns 8 different
      # push routes, but SERVICE_AUDIENCE is ONE env var for the whole
      # service — it cannot equal 8 different per-route URLs at once, so
      # every route but whichever one happened to match failed OIDC
      # verification with 401. Pinning every route on one service to
      # that service's own base URI (matching README.md's post-deploy
      # SERVICE_AUDIENCE step, which must use this SAME uri value, not
      # any other valid-but-differently-formatted Cloud Run hostname
      # alias) fixes this for all of them at once.
      audience = google_cloud_run_v2_service.service[each.value.service].uri
    }
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.dead_letter.id
    max_delivery_attempts = var.max_delivery_attempts
  }
}
