"""Tests for tools/capability_http_server.py (Deploy & Harden Phase 2c) —
the receiving side of typed HTTP capability dispatch.

OIDC verification is overridden via app.dependency_overrides, the same
pattern tests/test_frontend_api.py already uses for its own auth
dependency — a real deployment sets audience/allowed_caller_service_accounts
and never overrides verify_caller_identity; these tests exercise the
route logic (allowlist, idempotency, error shape) independent of live
OIDC infrastructure, which the client-side tests
(tests/test_capability_dispatch_client.py) don't need either.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.capability_dispatch import CapabilityRequest  # noqa: E402
from tools.capability_http_server import build_capability_app, verify_caller_identity  # noqa: E402


def _firestore_reachable() -> bool:
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


def _echo(**kwargs):
    return {"echoed": kwargs}


def _boom(**kwargs):
    raise ValueError("deliberate failure")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    app = build_capability_app(
        service_name="test-service",
        handlers={"test.echo": _echo, "test.boom": _boom},
    )
    app.dependency_overrides[verify_caller_identity] = lambda: "caller@example.internal"
    yield TestClient(app)
    app.dependency_overrides.pop(verify_caller_identity, None)


def test_status_lists_served_capabilities(client):
    response = client.get("/status")
    assert response.status_code == 200
    assert response.json()["capabilities"] == ["test.boom", "test.echo"]


@skip_if_no_firestore
def test_a_known_capability_runs_and_returns_the_handlers_result(client):
    req = CapabilityRequest(
        capability="test.echo",
        invocation_id=f"inv-{uuid.uuid4().hex[:8]}",
        payload={"kwargs": {"table": "customers"}},
    )
    response = client.post("/invoke", json=req.model_dump())
    assert response.status_code == 200
    body = response.json()
    assert body["result"] == {"echoed": {"table": "customers"}}
    assert body["invocation_id"] == req.invocation_id


def test_an_unserved_capability_is_a_404_not_a_dynamic_import_attempt(client):
    req = CapabilityRequest(capability="not.served.here", invocation_id=f"inv-{uuid.uuid4().hex[:8]}")
    response = client.post("/invoke", json=req.model_dump())
    assert response.status_code == 404
    assert "not.served.here" in response.json()["detail"]


@skip_if_no_firestore
def test_a_handler_exception_becomes_a_structured_500_not_a_bare_one(client):
    req = CapabilityRequest(capability="test.boom", invocation_id=f"inv-{uuid.uuid4().hex[:8]}")
    response = client.post("/invoke", json=req.model_dump())
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["error_type"] == "ValueError"
    assert "deliberate failure" in detail["error_message"]


@skip_if_no_firestore
def test_a_repeated_invocation_id_returns_the_cached_result_without_rerunning_the_handler(client):
    calls = []

    def _counted(**kwargs):
        calls.append(1)
        return {"n": len(calls)}

    from fastapi.testclient import TestClient

    from tools.capability_http_server import build_capability_app as _build

    app = _build(service_name="counted-service", handlers={"test.counted": _counted})
    app.dependency_overrides[verify_caller_identity] = lambda: "caller@example.internal"
    counted_client = TestClient(app)

    invocation_id = f"inv-{uuid.uuid4().hex[:8]}"
    req = CapabilityRequest(capability="test.counted", invocation_id=invocation_id)

    first = counted_client.post("/invoke", json=req.model_dump())
    second = counted_client.post("/invoke", json=req.model_dump())

    assert first.json()["result"] == {"n": 1}
    assert second.json()["result"] == {"n": 1}  # NOT {"n": 2} — the handler ran only once
    assert len(calls) == 1


def test_caller_verification_is_actually_enforced_when_not_overridden():
    from fastapi.testclient import TestClient

    app = build_capability_app(service_name="locked-service", handlers={"test.echo": _echo})
    client_no_override = TestClient(app)

    req = CapabilityRequest(capability="test.echo", invocation_id=f"inv-{uuid.uuid4().hex[:8]}")
    response = client_no_override.post("/invoke", json=req.model_dump())
    assert response.status_code == 401  # no Authorization header at all


def test_a_caller_not_on_the_allowlist_is_refused(monkeypatch):
    """Exercises verify_caller_identity's real allowlist logic (not the
    dependency-override bypass every other test in this file uses) by
    faking only the OIDC verification step, which needs no live Google
    infrastructure to fake, then letting the rest of the function
    (allowlist check) run for real."""
    from google.oauth2 import id_token as id_token_module

    monkeypatch.setattr(
        id_token_module, "verify_oauth2_token", lambda *a, **k: {"email": "someone-else@example.internal"}
    )

    from fastapi.testclient import TestClient

    app = build_capability_app(
        service_name="allowlisted-service",
        handlers={"test.echo": _echo},
        allowed_caller_service_accounts=["expected-caller@example.internal"],
    )
    strict_client = TestClient(app)

    req = CapabilityRequest(capability="test.echo", invocation_id=f"inv-{uuid.uuid4().hex[:8]}")
    response = strict_client.post(
        "/invoke", json=req.model_dump(), headers={"Authorization": "Bearer fake-token"}
    )
    assert response.status_code == 403


@skip_if_no_firestore
def test_a_caller_on_the_allowlist_is_accepted(monkeypatch):
    from google.oauth2 import id_token as id_token_module

    monkeypatch.setattr(
        id_token_module, "verify_oauth2_token", lambda *a, **k: {"email": "expected-caller@example.internal"}
    )

    from fastapi.testclient import TestClient

    app = build_capability_app(
        service_name="allowlisted-service",
        handlers={"test.echo": _echo},
        allowed_caller_service_accounts=["expected-caller@example.internal"],
    )
    strict_client = TestClient(app)

    req = CapabilityRequest(capability="test.echo", invocation_id=f"inv-{uuid.uuid4().hex[:8]}")
    response = strict_client.post(
        "/invoke", json=req.model_dump(), headers={"Authorization": "Bearer fake-token"}
    )
    assert response.status_code == 200


def test_request_body_over_the_size_limit_is_refused():
    from fastapi.testclient import TestClient

    import tools.capability_dispatch as dispatch_module

    app = build_capability_app(service_name="test-service", handlers={"test.echo": _echo})
    app.dependency_overrides[verify_caller_identity] = lambda: "caller@example.internal"
    size_client = TestClient(app)

    oversized_kwargs = {"blob": "x" * (dispatch_module.MAX_PAYLOAD_BYTES + 1)}
    req = CapabilityRequest(capability="test.echo", payload={"kwargs": oversized_kwargs})
    response = size_client.post("/invoke", json=req.model_dump())
    assert response.status_code == 413
