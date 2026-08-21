# Baseline alert policies (Deploy & Harden Phase 2f — expanded fully in
# Phase 5, docs/DURABILITY.md's SLO section). Created regardless of
# deploy_cloud_run_services, since the Pub/Sub-level alerts are useful
# even before any Cloud Run service exists (a pull-based consumer can
# still build backlog). Fire nowhere until var.alert_notification_channels
# has at least one entry — see variables.tf.

resource "google_monitoring_alert_policy" "pubsub_oldest_unacked_message_age" {
  project      = var.project_id
  display_name = "Pub/Sub oldest unacked message age (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "Oldest unacked message age > 5 minutes on any subscription"
    condition_threshold {
      filter          = "resource.type = \"pubsub_subscription\" AND metric.type = \"pubsub.googleapis.com/subscription/oldest_unacked_message_age\""
      comparison      = "COMPARISON_GT"
      threshold_value = 300
      duration        = "60s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MAX"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

resource "google_monitoring_alert_policy" "dead_letter_growth" {
  project      = var.project_id
  display_name = "Dead-letter topic receiving messages (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "dead-letter topic send count > 0"
    condition_threshold {
      filter          = "resource.type = \"pubsub_topic\" AND resource.label.topic_id = \"dead-letter\" AND metric.type = \"pubsub.googleapis.com/topic/send_message_operation_count\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

resource "google_monitoring_alert_policy" "cloud_run_5xx_rate" {
  count = var.deploy_cloud_run_services ? 1 : 0

  project      = var.project_id
  display_name = "Cloud Run 5xx rate (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "Any service's 5xx request count > 0"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.label.response_code_class = \"5xx\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "60s"
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

# ---------------------------------------------------------------------
# Phase 5 additions below. Two kinds of source:
#  (a) native GCP metrics (Cloud Run request latency, Cloud SQL) — the
#      metric.type strings are GCP's own documented names, high
#      confidence, but this environment cannot reach the live Metrics
#      Explorer to confirm them against the actual project, so verify
#      each one resolves (no "unknown metric" plan error) on first
#      `terraform plan` after enabling the gating flag.
#  (b) log-based metrics keyed on log lines that already exist in this
#      codebase today (quoted in each resource's filter/comment) rather
#      than inventing new instrumentation — see docs/DURABILITY.md's
#      observability section for the two conditions from the Phase 5
#      plan (wave-slot leak, assistant quota/safety event rate) that
#      have NO existing log line to key off yet and are therefore
#      listed there as a known gap, not faked here.
# ---------------------------------------------------------------------

resource "google_monitoring_alert_policy" "cloud_run_p95_latency" {
  count = var.deploy_cloud_run_services ? 1 : 0

  project      = var.project_id
  display_name = "Cloud Run p95 request latency > 5s (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "Any service's p95 request latency > 5000ms"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"run.googleapis.com/request_latencies\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5000
      duration        = "60s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_PERCENTILE_95"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

resource "google_monitoring_alert_policy" "cloud_run_job_failure_rate" {
  count = var.enable_data_plane_job ? 1 : 0

  project      = var.project_id
  display_name = "Data-plane Cloud Run Job failed executions (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "Job execution result=failed count > 0"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_job\" AND metric.type = \"run.googleapis.com/job/completed_execution_count\" AND metric.label.result = \"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

resource "google_monitoring_alert_policy" "cloud_sql_high_cpu" {
  count = var.enable_cloud_sql ? 1 : 0

  project      = var.project_id
  display_name = "Cloud SQL CPU utilization > 90% (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "CPU utilization > 0.9"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/cpu/utilization\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.9
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

resource "google_monitoring_alert_policy" "cloud_sql_low_disk" {
  count = var.enable_cloud_sql ? 1 : 0

  project      = var.project_id
  display_name = "Cloud SQL disk utilization > 85% (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "Disk utilization > 0.85"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/disk/utilization\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0.85
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

resource "google_monitoring_alert_policy" "cloud_sql_high_connections" {
  count = var.enable_cloud_sql ? 1 : 0

  project      = var.project_id
  display_name = "Cloud SQL connection count > 80% of the demo tier's ceiling (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    # db-f1-micro's practical connection ceiling is well under 100 —
    # this is a demo/staging profile (docs/ARCHITECTURE.md), so the
    # fixed threshold below is sized for that tier, not a production one.
    display_name = "Postgres connection count > 20"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND metric.type = \"cloudsql.googleapis.com/database/postgresql/num_backends\""
      comparison      = "COMPARISON_GT"
      threshold_value = 20
      duration        = "300s"
      aggregations {
        alignment_period   = "300s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

# -- Log-based metrics: keyed on log lines that already exist in code --

resource "google_logging_metric" "stuck_claimed_messages" {
  project = var.project_id
  name    = "deploy_harden_stuck_claimed_messages"
  # Matches the exact warning agents/orchestrator/orchestrator.py's
  # _dedup_claim() emits (line ~228) when a Pub/Sub redelivery finds a
  # prior "claimed" doc that was never marked "done" — a stuck claim
  # is treated as crash-recovery-redo automatically, but a *rate* of
  # these is itself a signal (an agent repeatedly crashing mid-handler,
  # or a handler that never reaches completion).
  filter = "resource.type=\"cloud_run_revision\" AND textPayload:\"has a stale claim\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "stuck_claimed_message_rate" {
  count = var.deploy_cloud_run_services ? 1 : 0

  project      = var.project_id
  display_name = "Stuck-claimed-message (crash-recovery-redo) rate (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "More than 3 stale-claim redos in 10 minutes"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.stuck_claimed_messages.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 3
      duration        = "0s"
      aggregations {
        alignment_period   = "600s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

resource "google_logging_metric" "gemini_narrative_fallback" {
  project = var.project_id
  name    = "deploy_harden_gemini_narrative_fallback"
  # Matches agents/orchestrator/recovery.py::_try_gemini_narrative's
  # exact log line (line ~146) — fires every time the "required Gemini
  # stage" (incident narrative generation) falls back to the
  # deterministic template because the live Vertex AI call failed.
  filter = "resource.type=\"cloud_run_revision\" AND textPayload:\"Gemini narrative unavailable\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "gemini_stage_failure_rate" {
  count = var.deploy_cloud_run_services ? 1 : 0

  project      = var.project_id
  display_name = "Required-Gemini-stage (incident narrative) failure rate (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "More than 5 deterministic-template fallbacks in 15 minutes"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.gemini_narrative_fallback.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 5
      duration        = "0s"
      aggregations {
        alignment_period   = "900s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

resource "google_logging_metric" "report_generation_failures" {
  project = var.project_id
  name    = "deploy_harden_report_generation_failures"
  # Matches frontend/report_service.py's logger.exception("Report
  # generation failed for %s", report_id) (line ~294).
  filter = "resource.type=\"cloud_run_revision\" AND textPayload:\"Report generation failed\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "report_generation_failure_rate" {
  count = var.deploy_cloud_run_services ? 1 : 0

  project      = var.project_id
  display_name = "Report generation failure rate (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "Any report generation failure in 15 minutes"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.report_generation_failures.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "900s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}

resource "google_logging_metric" "agent_framework_fallback_startups" {
  project = var.project_id
  name    = "deploy_harden_agent_framework_fallback_startups"
  # Matches every agents/*/agent.py's logger.warning(...) when
  # google-adk fails to import at module load time (e.g.
  # agents/discovery/agent.py line ~205) and the agent falls back to
  # Rung-2 direct tool-call dispatch. NOTE the scope this actually
  # measures: AGENT_FRAMEWORK is decided once per container/process
  # start, not per request — this metric is a count of *container
  # cold starts that landed in fallback mode*, which is still a real
  # and useful signal (what fraction of the fleet's images are missing
  # google-adk) but is not a per-invocation "hidden fallback path
  # trigger count." Documented honestly in docs/DURABILITY.md.
  filter = "resource.type=\"cloud_run_revision\" AND textPayload:\"using Rung-2 direct tool-call fallback\""
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }
}

resource "google_monitoring_alert_policy" "agent_framework_fallback_rate" {
  count = var.deploy_cloud_run_services ? 1 : 0

  project      = var.project_id
  display_name = "Agent container cold-starts landing in ADK-fallback mode (Deploy & Harden)"
  combiner     = "OR"

  conditions {
    display_name = "Any fallback-mode cold start in 15 minutes"
    condition_threshold {
      filter          = "resource.type = \"cloud_run_revision\" AND metric.type = \"logging.googleapis.com/user/${google_logging_metric.agent_framework_fallback_startups.name}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period   = "900s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  notification_channels = var.alert_notification_channels
}
