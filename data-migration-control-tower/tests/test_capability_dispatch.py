"""Tests for tools/capability_dispatch.py's envelope shape (Deploy &
Harden Phase 2c) — pure Pydantic model behavior, no live services."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.capability_dispatch import CapabilityRequest, build_payload  # noqa: E402


def test_build_payload_and_args_kwargs_round_trip():
    payload = build_payload("path/a", "path/b", run_id="run-1", estate_id="estate-1")
    req = CapabilityRequest(capability="discovery.catalog.estate", payload=payload)
    assert req.args() == ["path/a", "path/b"]
    assert req.kwargs() == {"run_id": "run-1", "estate_id": "estate-1"}


def test_an_empty_payload_yields_empty_args_and_kwargs():
    req = CapabilityRequest(capability="lineage.graph.build")
    assert req.args() == []
    assert req.kwargs() == {}


def test_invocation_id_defaults_to_a_fresh_uuid_each_time():
    a = CapabilityRequest(capability="test.cap")
    b = CapabilityRequest(capability="test.cap")
    assert a.invocation_id != b.invocation_id


def test_an_explicit_invocation_id_is_kept_as_given():
    req = CapabilityRequest(capability="test.cap", invocation_id="fixed-id-123")
    assert req.invocation_id == "fixed-id-123"


def test_the_envelope_serializes_to_json_and_back():
    req = CapabilityRequest(
        capability="discovery.catalog.estate",
        run_id="run-1",
        estate_id="estate-1",
        trace_id="trace-1",
        payload=build_payload(estate_id="estate-1"),
    )
    restored = CapabilityRequest.model_validate_json(req.model_dump_json())
    assert restored == req
