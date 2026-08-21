"""Tests for frontend/report_service.py's rate limiting (Deploy & Harden
Phase 5) — report generation had idempotency/audit/estate-authorization
already, but no per-user quota at all before this. Mirrors
frontend/assistant_service.py::_consume_quota()'s proven pattern.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from frontend.report_service import ReportQuotaExceeded, consume_report_quota  # noqa: E402


def _firestore_reachable() -> bool:
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


@pytest.fixture()
def cleanup_usage_doc():
    from tools.firestore_client import get_client

    keys: list[str] = []
    yield keys
    for key in keys:
        get_client().collection("report_daily_usage").document(key).delete()


@skip_if_no_firestore
def test_consume_report_quota_allows_calls_under_the_limit(monkeypatch, cleanup_usage_doc):
    import datetime as dt

    monkeypatch.setenv("REPORT_DAILY_LIMIT", "5")
    uid = f"test-uid-{uuid.uuid4().hex[:8]}"
    cleanup_usage_doc.append(f"{uid}_{dt.datetime.now(dt.timezone.utc).date().isoformat()}")

    for _ in range(5):
        consume_report_quota(uid)  # must not raise


@skip_if_no_firestore
def test_consume_report_quota_refuses_once_the_limit_is_hit(monkeypatch, cleanup_usage_doc):
    import datetime as dt

    monkeypatch.setenv("REPORT_DAILY_LIMIT", "2")
    uid = f"test-uid-{uuid.uuid4().hex[:8]}"
    cleanup_usage_doc.append(f"{uid}_{dt.datetime.now(dt.timezone.utc).date().isoformat()}")

    consume_report_quota(uid)
    consume_report_quota(uid)
    with pytest.raises(ReportQuotaExceeded, match="Daily report generation limit"):
        consume_report_quota(uid)


@skip_if_no_firestore
def test_consume_report_quota_is_scoped_per_user(monkeypatch, cleanup_usage_doc):
    """One user hitting their limit must not affect a different user's
    quota — a naive shared counter would be a real cross-user bug."""
    import datetime as dt

    monkeypatch.setenv("REPORT_DAILY_LIMIT", "1")
    uid_a = f"test-uid-a-{uuid.uuid4().hex[:8]}"
    uid_b = f"test-uid-b-{uuid.uuid4().hex[:8]}"
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    cleanup_usage_doc.extend([f"{uid_a}_{today}", f"{uid_b}_{today}"])

    consume_report_quota(uid_a)
    with pytest.raises(ReportQuotaExceeded):
        consume_report_quota(uid_a)
    consume_report_quota(uid_b)  # a fresh user, unaffected by uid_a's exhausted quota
