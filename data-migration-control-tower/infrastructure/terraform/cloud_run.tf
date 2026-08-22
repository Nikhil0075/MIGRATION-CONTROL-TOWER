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

      # Firebase Web SDK config — control-tower-ui only, and only when
      # var.firebase_web_config is actually supplied (see its own
      # description for why this isn't a secret and why there's no
      # default). Discovered live (Deploy & Harden Phase 5 close-out):
      # without this, the deployed console shows "Firebase
      # authentication is not configured" with no way to sign in.
      dynamic "env" {
        for_each = each.key == "control-tower-ui" && var.firebase_web_config != null ? {
          FIREBASE_API_KEY             = var.firebase_web_config.api_key
          FIREBASE_AUTH_DOMAIN         = var.firebase_web_config.auth_domain
          FIREBASE_PROJECT_ID          = var.firebase_web_config.project_id
          FIREBASE_STORAGE_BUCKET      = var.firebase_web_config.storage_bucket
          FIREBASE_MESSAGING_SENDER_ID = var.firebase_web_config.messaging_sender_id
          FIREBASE_APP_ID              = var.firebase_web_config.app_id
        } : {}
        content {
          name  = env.key
          value = env.value
        }
      }

      # Role bootstrap allowlists (frontend/security.py) — control-tower-ui
      # only, empty by default. Deliberately small/global per that
      # module's own docstring; real per-estate access should move to
      # Firebase custom claims instead of growing this list indefinitely.
      dynamic "env" {
        for_each = each.key == "control-tower-ui" && length(var.operator_allowlist) > 0 ? {
          OPERATOR_ALLOWLIST = join(",", var.operator_allowlist)
        } : {}
        content {
          name  = env.key
          value = env.value
        }
      }
      dynamic "env" {
        for_each = each.key == "control-tower-ui" && length(var.approver_allowlist) > 0 ? {
          APPROVER_ALLOWLIST = join(",", var.approver_allowlist)
        } : {}
        content {
          name  = env.key
          value = env.value
        }
      }
      # Async data-plane executor selection (Deploy & Harden Phase 3,
      # docs/adr/0003) — orchestrator only. Found missing live
      # (2026-08-22): google_cloud_run_v2_job.data_plane existed and was
      # reachable, but tools/orchestrator/orchestrator.py::
      # _select_data_plane_executor() reads DATA_PLANE_EXECUTOR from the
      # environment and had nothing to read — every run silently took
      # the synchronous InMemoryExecutor path instead, never exercising
      # the Cloud Run Job this variable exists to enable. Without this,
      # enable_data_plane_job only creates the job resource; it never
      # wires anything to actually invoke it.
      dynamic "env" {
        for_each = each.key == "orchestrator" && var.enable_data_plane_job ? {
          DATA_PLANE_EXECUTOR = "cloud_run_job"
          # .id, not .name: tools/data_plane_executors/cloud_run_job_
          # executor.py's own JOB_NAME_ENV_VAR docstring requires the
          # fully-qualified projects/P/locations/R/jobs/NAME form (the
          # Cloud Run Jobs v2 API's RunJobRequest.name field rejects a
          # bare job name) — .name on this resource is the bare id
          # alone. Caught live: the bare form let execute_remote() write
          # its PENDING migration_executions doc and then crash inside
          # _submit_job(), which (before handle_planned's own
          # already-migrating guard existed) fed straight back into the
          # illegal-transition redelivery loop this same investigation
          # found and fixed.
          DATA_PLANE_JOB_NAME = google_cloud_run_v2_job.data_plane[0].id
        } : {}
        content {
          name  = env.key
          value = env.value
        }
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
      #
      # GOTCHA, caught in `terraform plan` review before it shipped
      # (2026-08-22), same family as the IAM-wipe gotcha below: on an
      # environment where SERVICE_AUDIENCE was already set out-of-band
      # via `gcloud run services update`, running `terraform apply` after
      # adding a NEW dynamic "env" block above (e.g. the async
      # data-plane one) does not just add the new vars — the plan showed
      # it renaming the out-of-band SERVICE_AUDIENCE entry itself, i.e.
      # Terraform's env list is authoritative and would have deleted it,
      # since apply time it isn't declared anywhere in this file. Once
      # SERVICE_AUDIENCE has been set out-of-band for a service, any
      # further env-var change to that service must also go through
      # `gcloud run services update --update-env-vars` (which merges)
      # rather than `terraform apply` (which replaces the whole list),
      # until SERVICE_AUDIENCE is itself brought under Terraform
      # management — not done here, since that needs a two-pass apply
      # (create the service, then set audience from its own output) that
      # this module doesn't yet support.
    }

    # Direct VPC egress, orchestrator only (Deploy & Harden Phase 5
    # close-out — "wire up the Cloud SQL discovery path"). Discovery's
    # AgentCard is still runtime.type=local (docs/compliance_matrix.md's
    # own honest note), so tools/registry.py::invoke_capability() runs
    # Discovery's code IN-PROCESS inside the orchestrator, under
    # sa-orchestrator's identity — meaning the orchestrator's own network
    # path is what needs to reach Cloud SQL's private IP, not
    # discovery-agent's separately-deployed (but not yet actually
    # dispatched-to) service. Confirmed live during the earlier
    # investigation into why Discovery's SQL Server calls run under
    # sa-orchestrator at all. PRIVATE_RANGES_ONLY keeps every other
    # outbound call (Firestore, Pub/Sub, BigQuery, every agent-to-agent
    # HTTP capability call) on the normal public-internet path.
    dynamic "vpc_access" {
      for_each = each.key == "orchestrator" && var.enable_cloud_sql ? [1] : []
      content {
        network_interfaces {
          network    = data.google_compute_network.default[0].id
          subnetwork = data.google_compute_subnetwork.default[0].id
        }
        egress = "PRIVATE_RANGES_ONLY"
      }
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

# GOTCHA discovered live (Deploy & Harden Phase 5 close-out): a
# `terraform apply -replace=...` on google_cloud_run_v2_service.service["control-tower-ui"]
# destroys and recreates the service — which wipes its IAM policy on GCP's
# side — but this iam_member resource's own attributes never change (it
# targets the service by its stable *name*, not an internal ID), so
# Terraform sees "no changes needed" and does NOT reapply it. The result:
# a `-replace` on the frontend service silently makes the whole console
# 403 for every visitor until this binding is reapplied by hand or via
# its own `-replace`. If you ever `-replace` control-tower-ui again,
# `-replace` this resource in the SAME apply, or immediately run:
#   gcloud run services add-iam-policy-binding control-tower-ui \
#     --member=allUsers --role=roles/run.invoker --region=REGION

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

# Same GOTCHA as google_cloud_run_v2_service_iam_member.public_frontend's
# comment above, confirmed a second time live (Deploy & Harden Phase 5
# close-out, orchestrator specifically): `-replace`-ing a service wipes
# its whole IAM policy, and these caller bindings won't be reapplied
# automatically since their own attributes don't change. This bit
# orchestrator concretely: after a `-replace` to pick up a code fix,
# sa-pubsub-invoker could no longer call any of orchestrator's 8 push
# routes until this binding was reapplied by hand. If you `-replace` any
# service, `-replace` (or otherwise reapply) its allowed_callers entries
# in the SAME apply.
