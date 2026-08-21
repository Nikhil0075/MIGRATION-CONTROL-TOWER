"""Offline structural checks for infrastructure/terraform/monitoring.tf
(Deploy & Harden Phase 5's observability expansion).

No `terraform` binary is available in this dev environment (confirmed
during Phase 2), so this validates the HCL directly with python-hcl2
rather than shelling out to `terraform validate`/`plan` — real syntax
and cross-reference checks, but not a substitute for a live `terraform
plan` against the actual project before the first apply that enables
any of these gating flags.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

hcl2 = pytest.importorskip("hcl2", reason="python-hcl2 not installed — see requirements.txt")

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

MONITORING_TF = REPO_ROOT / "infrastructure" / "terraform" / "monitoring.tf"


@pytest.fixture(scope="module")
def parsed():
    with open(MONITORING_TF, encoding="utf-8") as f:
        return hcl2.load(f)


def _resources_by_type(parsed_doc, resource_type: str) -> dict:
    """Flatten hcl2's [{type: {name: {...}}}] resource list into {name: body}
    for one type. This installed python-hcl2 version keeps the literal
    surrounding quotes on both the resource-type and resource-name keys
    (e.g. '"google_monitoring_alert_policy"' -> '"stuck_claimed_message_rate"'),
    so strip them on both sides rather than assuming bare identifiers."""
    out = {}
    for block in parsed_doc.get("resource", []):
        for raw_type, names in block.items():
            if raw_type.strip('"') != resource_type:
                continue
            for raw_name, body in names.items():
                out[raw_name.strip('"')] = body
    return out


def test_monitoring_tf_is_valid_hcl(parsed):
    assert "resource" in parsed
    assert len(parsed["resource"]) > 0


def test_every_alert_policy_wires_the_shared_notification_channels_variable(parsed):
    """Regression guard: an alert policy that forgets `notification_channels =
    var.alert_notification_channels` fires nowhere, silently — the whole
    point of that shared variable (empty by default, see variables.tf) is
    that every policy honors it uniformly."""
    policies = _resources_by_type(parsed, "google_monitoring_alert_policy")
    assert len(policies) >= 10, "expected the Phase 2f baseline plus the Phase 5 additions"
    for name, body in policies.items():
        # hcl2 wraps each named block's body in a one-element list.
        block = body[0] if isinstance(body, list) else body
        assert block.get("notification_channels") == "${var.alert_notification_channels}", (
            f"google_monitoring_alert_policy.{name} does not reference "
            "var.alert_notification_channels"
        )


def test_expensive_resource_alerts_are_gated_behind_the_same_flag_as_the_resource(parsed):
    """Cloud SQL and data-plane-job alert policies must not be created
    before the resources they monitor exist — same flag, same reasoning
    as variables.tf's deploy_cloud_run_services/enable_cloud_sql/
    enable_data_plane_job gating."""
    policies = _resources_by_type(parsed, "google_monitoring_alert_policy")
    expected_gates = {
        "cloud_sql_high_cpu": "enable_cloud_sql",
        "cloud_sql_low_disk": "enable_cloud_sql",
        "cloud_sql_high_connections": "enable_cloud_sql",
        "cloud_run_job_failure_rate": "enable_data_plane_job",
        "cloud_run_5xx_rate": "deploy_cloud_run_services",
        "cloud_run_p95_latency": "deploy_cloud_run_services",
    }
    for name, expected_var in expected_gates.items():
        assert name in policies, f"expected alert policy {name} to exist"
        block = policies[name][0] if isinstance(policies[name], list) else policies[name]
        count_expr = block.get("count")
        assert count_expr is not None, f"{name} has no count gate at all"
        assert expected_var in count_expr, (
            f"{name}'s count gate ({count_expr!r}) does not reference var.{expected_var}"
        )


def test_log_based_metrics_are_referenced_by_a_matching_alert_policy(parsed):
    """Every google_logging_metric defined here exists to feed exactly one
    alert policy — catch a metric left orphaned (defined, never alerted
    on) or an alert policy whose filter string drifted from the metric's
    actual `name`."""
    metrics = _resources_by_type(parsed, "google_logging_metric")
    policies = _resources_by_type(parsed, "google_monitoring_alert_policy")
    assert len(metrics) >= 4

    all_policy_text = str(policies)
    for metric_name in metrics:
        assert f"google_logging_metric.{metric_name}.name" in all_policy_text, (
            f"google_logging_metric.{metric_name} is not referenced by any "
            "alert policy's filter — orphaned metric"
        )


def test_log_based_metric_filters_quote_a_real_log_line_from_the_codebase(parsed):
    """Each log-based metric's filter string is documented (in its own
    comment) as matching an exact log line already emitted somewhere in
    the codebase — assert the quoted substring actually still appears in
    the source file the comment names, so a future refactor that changes
    the wording is caught here instead of silently breaking the alert."""
    checks = [
        ("has a stale claim", REPO_ROOT / "agents" / "orchestrator" / "orchestrator.py"),
        ("Gemini narrative unavailable", REPO_ROOT / "agents" / "orchestrator" / "recovery.py"),
        ("Report generation failed", REPO_ROOT / "frontend" / "report_service.py"),
        ("using Rung-2 direct tool-call fallback", REPO_ROOT / "agents" / "discovery" / "agent.py"),
    ]
    for phrase, source_file in checks:
        assert source_file.is_file(), f"{source_file} does not exist"
        content = source_file.read_text(encoding="utf-8")
        assert phrase in content, (
            f"log-based-metric filter phrase {phrase!r} no longer appears in "
            f"{source_file} — the alert policy keyed on it would now fire never, "
            "silently"
        )
