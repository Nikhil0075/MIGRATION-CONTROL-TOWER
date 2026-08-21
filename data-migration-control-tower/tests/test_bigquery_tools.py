"""Tests for tools/bigquery_tools.py's dry-run -> reserve -> cap cost
controls (Deploy & Harden Phase 1c).

The per-query cap needs no live GCP call — a fake BigQuery client stands
in, following the same monkeypatch-get_client pattern already used by
tests/test_cost_evidence.py's billing_export tests. The per-run
cumulative reservation genuinely needs Firestore (it's a real
transaction against migration_runs/{run_id}/budget/bigquery), so those
tests skip when it's unreachable, same as every other live-Firestore
test in this suite.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from google.api_core.exceptions import Forbidden
from google.cloud import bigquery

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import tools.bigquery_tools as bq  # noqa: E402
from tools.usage_meter import attributed_to  # noqa: E402


def _firestore_reachable() -> bool:
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


class _FakeJob:
    def __init__(self, *, dry_run_bytes: int = 0, billed_bytes: int = 0, processed_bytes: int = 0, raise_forbidden: bool = False):
        self.total_bytes_processed = dry_run_bytes or processed_bytes
        self.total_bytes_billed = billed_bytes
        self._raise_forbidden = raise_forbidden

    def result(self):
        if self._raise_forbidden:
            raise Forbidden("Query exceeded limit for bytes billed")
        return [{"n": 1}]


class _FakeClient:
    """query() branches on job_config.dry_run, like the real client does —
    a dry run never executes and returns instantly with an estimate; a
    real run may raise Forbidden if told to."""

    def __init__(self, *, dry_run_bytes: int, billed_bytes: int | None = None, raise_forbidden: bool = False):
        self.dry_run_bytes = dry_run_bytes
        self.billed_bytes = billed_bytes if billed_bytes is not None else dry_run_bytes
        self.raise_forbidden = raise_forbidden
        self.real_query_config: bigquery.QueryJobConfig | None = None
        self.real_query_called = False

    def query(self, query, job_config=None):
        if job_config is not None and job_config.dry_run:
            return _FakeJob(dry_run_bytes=self.dry_run_bytes)
        self.real_query_called = True
        self.real_query_config = job_config
        return _FakeJob(
            billed_bytes=self.billed_bytes,
            processed_bytes=self.billed_bytes,
            raise_forbidden=self.raise_forbidden,
        )


@pytest.fixture()
def fake_bq(monkeypatch):
    def _install(**kwargs) -> _FakeClient:
        client = _FakeClient(**kwargs)
        monkeypatch.setattr(bq, "get_client", lambda: client)
        return client

    return _install


# -- Per-query cap (no Firestore/run_id needed) ------------------------------


def test_a_query_under_the_per_query_cap_runs_and_sets_maximum_bytes_billed(fake_bq, monkeypatch):
    monkeypatch.setenv("BQ_MAX_BYTES_BILLED_PER_QUERY", str(1024**3))  # 1 GiB
    client = fake_bq(dry_run_bytes=10 * 1024**2)  # 10 MiB estimate

    result = bq._metered_query("SELECT 1", purpose="test.small_query")

    assert list(result) == [{"n": 1}]
    assert client.real_query_called
    assert client.real_query_config.maximum_bytes_billed is not None
    assert client.real_query_config.maximum_bytes_billed <= 1024**3


def test_a_query_whose_dry_run_estimate_exceeds_the_cap_is_refused_before_running(fake_bq, monkeypatch):
    monkeypatch.setenv("BQ_MAX_BYTES_BILLED_PER_QUERY", str(100 * 1024**2))  # 100 MiB cap
    client = fake_bq(dry_run_bytes=5 * 1024**3)  # 5 GiB estimate — way over

    with pytest.raises(bq.QueryBudgetExceeded, match="per-query cap"):
        bq._metered_query("SELECT * FROM huge_table", purpose="test.huge_query")

    assert client.real_query_called is False  # refused before spending anything


def test_bigquerys_own_refusal_is_wrapped_with_a_clear_message(fake_bq, monkeypatch):
    monkeypatch.setenv("BQ_MAX_BYTES_BILLED_PER_QUERY", str(1024**3))
    fake_bq(dry_run_bytes=10 * 1024**2, billed_bytes=2 * 1024**3, raise_forbidden=True)

    with pytest.raises(bq.QueryBudgetExceeded, match="maximum_bytes_billed cap"):
        bq._metered_query("SELECT 1", purpose="test.underestimated_query")


def test_maximum_bytes_billed_never_exceeds_the_per_query_cap_even_with_margin(fake_bq, monkeypatch):
    cap = 50 * 1024**2  # 50 MiB
    monkeypatch.setenv("BQ_MAX_BYTES_BILLED_PER_QUERY", str(cap))
    # Estimate close enough to the cap that estimate + margin would exceed
    # it, if the cap weren't enforced as a hard ceiling on top of the margin.
    client = fake_bq(dry_run_bytes=cap - 1024)

    bq._metered_query("SELECT 1", purpose="test.near_cap_query")

    assert client.real_query_config.maximum_bytes_billed <= cap


# -- Per-run cumulative reservation (needs live Firestore) -------------------


@skip_if_no_firestore
def test_reservations_accumulate_and_refuse_once_the_run_cap_is_exceeded(fake_bq, monkeypatch):
    from tools.firestore_client import get_client
    from tools.usage_meter import RunBudgetExceeded, reserve_bigquery_budget

    run_id = f"test-budget-{uuid.uuid4().hex[:8]}"
    doc_ref = get_client().collection("migration_runs").document(run_id).collection("budget").document("bigquery")
    try:
        cap = 10 * 1024**2  # 10 MiB cap for this test
        first = reserve_bigquery_budget(run_id, 6 * 1024**2, cap)
        assert first == 6 * 1024**2

        with pytest.raises(RunBudgetExceeded) as excinfo:
            reserve_bigquery_budget(run_id, 6 * 1024**2, cap)  # 6 + 6 > 10
        assert excinfo.value.reserved_bytes == 6 * 1024**2
    finally:
        doc_ref.delete()


@skip_if_no_firestore
def test_metered_query_refuses_once_the_attributed_runs_cumulative_cap_is_hit(fake_bq, monkeypatch):
    from tools.firestore_client import get_client

    monkeypatch.setenv("BQ_MAX_BYTES_BILLED_PER_QUERY", str(1024**3))  # generous per-query cap
    monkeypatch.setenv("BQ_MAX_BYTES_BILLED_PER_RUN", str(10 * 1024**2))  # tight 10 MiB run cap
    fake_bq(dry_run_bytes=6 * 1024**2)  # each query estimates 6 MiB

    run_id = f"test-budget-{uuid.uuid4().hex[:8]}"
    doc_ref = get_client().collection("migration_runs").document(run_id).collection("budget").document("bigquery")
    try:
        with attributed_to(run_id):
            bq._metered_query("SELECT 1", purpose="test.first_query")  # 6 MiB reserved, under 10 MiB cap
            with pytest.raises(bq.QueryBudgetExceeded, match="per-run"):
                bq._metered_query("SELECT 1", purpose="test.second_query")  # 6+6=12 > 10 MiB cap
    finally:
        doc_ref.delete()


def test_unattributed_usage_skips_reservation_but_still_enforces_the_per_query_cap(fake_bq, monkeypatch):
    # current_run_id() is None outside any attributed_to() block — the
    # per-run reservation is a no-op (nothing to reserve against), but the
    # flat per-query cap still applies independently.
    monkeypatch.setenv("BQ_MAX_BYTES_BILLED_PER_QUERY", str(100 * 1024**2))
    fake_bq(dry_run_bytes=5 * 1024**3)

    with pytest.raises(bq.QueryBudgetExceeded, match="per-query cap"):
        bq._metered_query("SELECT 1", purpose="test.unattributed")
