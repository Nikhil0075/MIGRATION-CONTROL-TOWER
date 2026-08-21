# The 9-service topology (docs/ARCHITECTURE.md). Gated behind
# var.deploy_cloud_run_services (default false) — see variables.tf for
# why. Every service is --no-allow-unauthenticated by construction (no
# `google_cloud_run_v2_service_iam_member` grants allUsers anywhere in
# this file) except the frontend, which is deliberately public.

locals {
  # service name -> {agent SA key, allowed callers (by SA key), needs unixodbc image}
  services = {
    "control-tower-ui" = {
      sa_key          = "sa-orchestrator" # matches frontend/Dockerfile's existing deploy comment
      allowed_callers = [] # public-facing console, not a capability/push target itself
      public          = true
    }
    "orchestrator" = {
      sa_key = "sa-orchestrator"
      # Called by: Pub/Sub push (sa-pubsub-invoker) for its 7 mounted
      # consumers. Not called agent-to-agent (it's the dispatcher, not a
      # capability provider).
      allowed_callers = ["sa-pubsub-invoker"]
      public          = false
    }
    "discovery-agent" = {
      sa_key          = "sa-discovery"
      allowed_callers = ["sa-orchestrator", "sa-pubsub-invoker"]
      public          = false
    }
    "lineage-agent" = {
      sa_key          = "sa-lineage"
      allowed_callers = ["sa-orchestrator"]
      public          = false
    }
    "risk-agent" = {
      sa_key          = "sa-risk"
      allowed_callers = ["sa-orchestrator"]
      public          = false
    }
    "planner-agent" = {
      sa_key          = "sa-planner"
      allowed_callers = ["sa-orchestrator"]
      public          = false
    }
    "validation-agent" = {
      sa_key          = "sa-validation"
      allowed_callers = ["sa-orchestrator"]
      public          = false
    }
    "cutover-agent" = {
      sa_key          = "sa-cutover"
      allowed_callers = ["sa-orchestrator", "sa-pubsub-invoker"]
      public          = false
    }
    "finance-impact-agent" = {
      sa_key          = "sa-finance-impact"
      allowed_callers = ["sa-orchestrator"]
      public          = false
    }
  }

  service_account_email_by_key = merge(
    { for k, sa in google_service_account.agent : k => sa.email },
    { "sa-pubsub-invoker" = google_service_account.pubsub_invoker.email },
  )
}

resource "google_cloud_run_v2_service" "service" {
  for_each = var.deploy_cloud_run_services ? local.services : {}

  project  = var.project_id
  name     = each.key
  location = var.region

  template {
    service_account = local.service_account_email_by_key[each.value.sa_key]

    scaling {
      max_instance_count = var.cloud_run_max_instances
    }

    containers {
      image = var.service_image_tags[each.key] # deliberately no default — see variables.tf

      env {
        name  = "GCP_PROJECT_ID"
        value = var.project_id
      }
      env {
        name  = "ALLOWED_CALLER_SERVICE_ACCOUNTS"
        value = join(",", [for k in each.value.allowed_callers : local.service_account_email_by_key[k]])
      }
      # SERVICE_AUDIENCE intentionally NOT set here — it would need this
      # service's own URL, which Terraform cannot reference from within
      # the same resource's own config. Set it in a follow-up
      # `gcloud run services update <name> --update-env-vars
      # SERVICE_AUDIENCE=$(gcloud run services describe <name>
      # --format='value(status.url)')` per service after the first
      # apply — see README.md's post-deploy checklist. Until that's
      # done, OIDC audience checking is permissive (any audience), which
      # tools/capability_http_server.py's own docstring already flags as
      # a real gap, not a hidden one.
    }
  }

  # Ingress: public for the console, internal-plus-load-balancer for
  # everything else (Cloud Run's own "internal and Cloud Load Balancing"
  # setting) — not fully public even before the explicit IAM check below.
  ingress = each.value.public ? "INGRESS_TRAFFIC_ALL" : "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"
}

resource "google_cloud_run_v2_service_iam_member" "public_frontend" {
  for_each = var.deploy_cloud_run_services ? { "control-tower-ui" = true } : {}

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.service["control-tower-ui"].name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# run.invoker for every explicitly-allowed caller — no service is
# invokable by an identity not named in local.services[*].allowed_callers.
resource "google_cloud_run_v2_service_iam_member" "allowed_callers" {
  for_each = var.deploy_cloud_run_services ? {
    for pair in flatten([
      for svc_name, svc in local.services : [
        for caller in svc.allowed_callers : { key = "${svc_name}--${caller}", service = svc_name, caller = caller }
      ]
    ]) : pair.key => pair
  } : {}

  project  = var.project_id
  location = var.region
  name     = google_cloud_run_v2_service.service[each.value.service].name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${local.service_account_email_by_key[each.value.caller]}"
}
