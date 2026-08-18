"""OpenTelemetry span helper, exported to Cloud Trace (Day 10 hardening,
Phase 5 — master doc Appendix E: "Traces carry run ID and agent identity
and are viewable in Cloud Trace.").

A thin wrapper, mirroring tools/evaluation_harness.py's style: one small
module other code calls into directly, not a framework. Every span this
project creates carries run_id and (where resolved) agent_id/version as
attributes, so a judge can filter Cloud Trace's UI to exactly one run
and see the real sequence and duration of every stage that touched it —
even though this project runs the fleet as one orchestrator process
today, not as separate Cloud Run services per agent (see
infrastructure/README.md's Rung-2 substitution note). The span tree
still shows genuine causality and timing; it just doesn't cross a real
service boundary yet.

Tracing is strictly best-effort and never fatal: a missing
GCP_PROJECT_ID, an unreachable Cloud Trace API, or any other exporter
failure degrades to "no traces recorded," never a broken migration run
— the same "explanatory layer can fail safely, the deterministic core
never does" discipline this project already applies to Gemini
narratives (agents/orchestrator/recovery.py) and the Gemma
substitution (tools/fast_pii_screen.py).
"""

from __future__ import annotations

import contextlib
import logging
import os
from functools import lru_cache

logger = logging.getLogger("tracing")

_SERVICE_NAME = "data-migration-control-tower"


@lru_cache(maxsize=1)
def _tracer():
    """Lazy, cached tracer wired to Cloud Trace. Returns None (never
    raises) if GCP_PROJECT_ID isn't set or the exporter can't be
    constructed — span() below treats that as "tracing disabled."
    """
    project_id = os.environ.get("GCP_PROJECT_ID")
    if not project_id:
        logger.info("GCP_PROJECT_ID not set — tracing disabled.")
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": _SERVICE_NAME}))
        provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter(project_id=project_id)))
        trace.set_tracer_provider(provider)
        return trace.get_tracer(_SERVICE_NAME)
    except Exception as exc:  # noqa: BLE001 — tracing must never break a real run
        logger.info("Cloud Trace exporter unavailable (%s); tracing disabled.", exc)
        return None


def flush(timeout_millis: int = 5000) -> None:
    """Forces any pending spans out to Cloud Trace before the process
    exits. The OTel SDK registers an atexit shutdown hook on its own,
    but that's timing-dependent — a short-lived CLI script (this
    project's usual shape: run, print a summary, exit) can outrace it
    and lose the last batch. Call this explicitly at the end of any
    script whose spans matter for the demo, e.g.
    agents/orchestrator/run_full_migration.py's main(). A no-op if
    tracing was never configured.
    """
    tracer = _tracer()
    if tracer is None:
        return
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis)
    except Exception as exc:  # noqa: BLE001 — flushing must never break a real run either
        logger.info("Cloud Trace flush failed (%s).", exc)


@contextlib.contextmanager
def span(name: str, **attributes):
    """Opens a span named `name` for the duration of the with-block,
    tagged with `attributes` (run_id, agent_id, capability, version,
    ...). A no-op context manager (yields None) when tracing isn't
    configured, so every caller can use this unconditionally without a
    feature-flag check of its own.

    Usage:
        with tracing.span("handle_risk_assessed", run_id=run_id, agent_id=agent_id, version=version):
            ...
    """
    tracer = _tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as current_span:
        for key, value in attributes.items():
            if value is not None:
                current_span.set_attribute(key, str(value))
        yield current_span
