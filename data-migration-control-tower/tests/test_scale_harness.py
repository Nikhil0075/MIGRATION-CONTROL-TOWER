"""Tests for evaluation/scale_harness.py (Deploy & Harden Phase 4b) —
the throughput field, model_calls note, and tier-keyed Firestore writes
added on top of the existing schema-validation/wave-scheduling/policy-
decision measurements.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from evaluation.scale_harness import (  # noqa: E402
    APPROVED_TIERS,
    _percentile,
    run_scale_demo,
    write_report,
)


def _firestore_reachable() -> bool:
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


def test_percentile_of_a_single_value_is_itself():
    assert _percentile([5.0], 50) == 5.0
    assert _percentile([5.0], 95) == 5.0


def test_percentile_interpolates_between_neighbors():
    values = [1.0, 2.0, 3.0, 4.0]
    assert _percentile(values, 50) == pytest.approx(2.5)


def test_percentile_of_empty_is_zero():
    assert _percentile([], 50) == 0.0


@pytest.mark.parametrize("tier", sorted(APPROVED_TIERS))
def test_approved_tiers_are_the_deploy_and_harden_phase_4_set(tier):
    """Regression guard: 1k/5k/20k specifically, not some other set —
    these are the exact tiers the plan approved, not a range."""
    assert tier in {1000, 5000, 20000}


@skip_if_no_firestore
def test_run_scale_demo_reports_zero_model_calls_explicitly():
    """This harness must never silently omit model_calls — it's always
    0, stated as a fact, not left absent (Deploy & Harden Phase 4b)."""
    metrics = run_scale_demo(100)
    assert metrics["model_calls"] == 0
    assert "control-plane-only" in metrics["model_calls_note"]


@skip_if_no_firestore
def test_run_scale_demo_computes_throughput_from_existing_latency_data():
    metrics = run_scale_demo(100)
    assert metrics["schema_validation"]["throughput_per_sec"] > 0
    assert metrics["wave_scheduling"]["throughput_per_sec"] > 0


#: write_report() calls path.relative_to(REPO_ROOT) internally, so
#: reports_dir must be inside the repo tree — pytest's tmp_path (system
#: temp) fails that unconditionally, unrelated to anything this phase
#: changed. Tests below use the real reports dir and delete what they
#: wrote in a finally block.
REAL_REPORTS_DIR = REPO_ROOT / "evaluation" / "reports"


@skip_if_no_firestore
def test_write_report_writes_a_tier_keyed_doc_and_a_current_alias():
    from tools.firestore_client import get_client

    # A count value unlikely to collide with a real tier someone else is
    # using concurrently.
    count = 100_000 + uuid.uuid4().int % 1000
    metrics = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "pipeline_count": count,
        "model_calls": 0,
        "model_calls_note": "control-plane-only",
        "schema_validation": {"p50_ms": 1.0, "p95_ms": 2.0, "total_ms": 10.0, "throughput_per_sec": 100.0},
        "wave_scheduling": {
            "total_duration_ms": 5.0, "avg_per_item_ms": 0.05, "throughput_per_sec": 200.0,
            "admitted": count, "held": 0, "backlog_escalated": 0,
        },
        "policy_decisions": {"sample_size": 10, "p50_ms": 1.0, "p95_ms": 2.0},
    }
    reports_col = get_client().collection("evaluation_scale_reports")
    tier_file = REAL_REPORTS_DIR / f"scale_metrics_{count}.md"
    current_file = REAL_REPORTS_DIR / "scale_metrics.md"
    # "current" (the Firestore doc) stays deliberately un-deleted — see
    # below — but the on-disk alias is a real committed repo artifact
    # (the latest real scale run's evidence), not a shared resource
    # other concurrent *processes* read from disk. Snapshot it so this
    # test's write gets undone locally, same as the tier-suffixed file.
    pre_existing_current = current_file.read_text(encoding="utf-8") if current_file.exists() else None
    try:
        write_report(metrics, reports_dir=REAL_REPORTS_DIR)

        tier_doc = reports_col.document(str(count)).get()
        assert tier_doc.exists
        assert tier_doc.to_dict()["pipeline_count"] == count

        current_doc = reports_col.document("current").get()
        assert current_doc.exists
        assert current_doc.to_dict()["pipeline_count"] == count

        # Both files written: the tier-suffixed one and the fixed-name alias.
        assert tier_file.exists()
        assert current_file.exists()
    finally:
        reports_col.document(str(count)).delete()
        tier_file.unlink(missing_ok=True)
        # The Firestore "current" doc is shared/global — deliberately NOT
        # deleted here, to avoid a test run clobbering another process's
        # view of "the latest tier"; this test only asserts current was
        # set to ITS value at the time it ran, not that it stays that way
        # after. The on-disk file, though, only this checkout's test
        # process writes to — restore it so a full test-suite run doesn't
        # leave fake data sitting in a real, committed evidence file.
        if pre_existing_current is None:
            current_file.unlink(missing_ok=True)
        else:
            current_file.write_text(pre_existing_current, encoding="utf-8")


@skip_if_no_firestore
def test_write_report_does_not_clobber_a_different_tiers_doc():
    """The bug this phase fixed: before tier-keying, a second write to
    "current" would silently overwrite the first tier's own record —
    this proves two different counts now coexist."""
    from tools.firestore_client import get_client

    base = 200_000 + uuid.uuid4().int % 1000
    reports_col = get_client().collection("evaluation_scale_reports")
    counts = [base, base + 1]
    tier_files = [REAL_REPORTS_DIR / f"scale_metrics_{count}.md" for count in counts]
    current_file = REAL_REPORTS_DIR / "scale_metrics.md"
    pre_existing_current = current_file.read_text(encoding="utf-8") if current_file.exists() else None
    try:
        for count in counts:
            metrics = {
                "generated_at": "2026-01-01T00:00:00+00:00",
                "pipeline_count": count,
                "model_calls": 0,
                "model_calls_note": "control-plane-only",
                "schema_validation": {"p50_ms": 1.0, "p95_ms": 2.0, "total_ms": 10.0, "throughput_per_sec": 100.0},
                "wave_scheduling": {
                    "total_duration_ms": 5.0, "avg_per_item_ms": 0.05, "throughput_per_sec": 200.0,
                    "admitted": count, "held": 0, "backlog_escalated": 0,
                },
                "policy_decisions": {"sample_size": 10, "p50_ms": 1.0, "p95_ms": 2.0},
            }
            write_report(metrics, reports_dir=REAL_REPORTS_DIR)

        for count in counts:
            doc = reports_col.document(str(count)).get()
            assert doc.exists
            assert doc.to_dict()["pipeline_count"] == count  # neither overwrote the other
    finally:
        for count in counts:
            reports_col.document(str(count)).delete()
        for tier_file in tier_files:
            tier_file.unlink(missing_ok=True)
        if pre_existing_current is None:
            current_file.unlink(missing_ok=True)
        else:
            current_file.write_text(pre_existing_current, encoding="utf-8")
        for tier_file in tier_files:
            tier_file.unlink(missing_ok=True)
