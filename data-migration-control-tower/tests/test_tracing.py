"""Tests for tools/tracing.py — Day 10 hardening Phase 5.

No live Cloud Trace calls: the real-exporter path is exercised instead
by a live run_full_migration.py run (see the Phase 5 section of
README.md) and by manually querying Cloud Trace afterward — a
unit-testable "did the span get created and exported" assertion isn't
meaningful against a real async batch exporter. These tests cover the
one thing that must always hold regardless of environment: tracing
degrades to a safe no-op, it never raises.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools import tracing  # noqa: E402


def test_span_is_a_no_op_without_gcp_project_id(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    tracing._tracer.cache_clear()

    with tracing.span("some-stage", run_id="run-123") as current_span:
        assert current_span is None  # no-op tracer, never raises


def test_span_never_raises_even_with_attributes(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    tracing._tracer.cache_clear()

    # Should complete cleanly whether or not tracing is actually configured.
    with tracing.span("another-stage", run_id="run-456", agent_id="risk-agent", version="1.0.0", none_value=None):
        pass


def test_span_propagates_exceptions_from_the_with_block(monkeypatch):
    """A span must never swallow a real error from the code it wraps —
    tracing is observability, not error handling."""
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    tracing._tracer.cache_clear()

    raised = False
    try:
        with tracing.span("failing-stage", run_id="run-789"):
            raise ValueError("boom")
    except ValueError:
        raised = True
    assert raised


def test_tracer_is_cached(monkeypatch):
    monkeypatch.delenv("GCP_PROJECT_ID", raising=False)
    tracing._tracer.cache_clear()
    first = tracing._tracer()
    second = tracing._tracer()
    assert first is second  # lru_cache(maxsize=1) — one construction attempt per process
