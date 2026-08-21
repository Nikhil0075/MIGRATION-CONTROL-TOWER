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
