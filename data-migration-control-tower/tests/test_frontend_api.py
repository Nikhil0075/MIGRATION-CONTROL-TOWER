"""Tests for frontend/app.py — the Control Tower API is a thin read-only
wrapper over the exact Firestore data every other script reads/writes,
so these are integration-style: real Firestore, real registry/run data.
Skips automatically when Firestore isn't reachable, same pattern as the
rest of this suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _firestore_reachable() -> bool:
    # Delegates to the shared probe, which performs a real round trip.
    # This used to call `get_client()` and return True — but the Firestore
    # client is lazy and does no I/O when constructed, so it answered True
    # whenever the import worked, and the skipif below never skipped.
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from frontend.app import app

    return TestClient(app)


@pytest.fixture()
def authed_client():
    """Day 10 hardening: approve_run() now requires a verified Firebase ID
    token via the get_approver_identity dependency. Overriding that
    dependency (rather than needing a real Firebase project + ID token in
    tests) is the standard FastAPI pattern and exercises the exact same
    endpoint logic after the identity is established.
    """
    from fastapi.testclient import TestClient

    from frontend.app import app, get_approver_identity

    app.dependency_overrides[get_approver_identity] = lambda: "test-approver@example.internal"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_approver_identity, None)


@skip_if_no_firestore
def test_dashboard_returns_expected_shape(client):
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    body = response.json()
    for key in ("fleet_health", "total_runs", "completed_runs", "at_risk_runs", "blocked_runs"):
        assert key in body


@skip_if_no_firestore
def test_list_runs_returns_a_list(client):
    response = client.get("/api/runs")
    assert response.status_code == 200
    runs = response.json()
    assert isinstance(runs, list)
    if runs:
        assert "run_id" in runs[0]
        assert "state" in runs[0]


@skip_if_no_firestore
def test_run_detail_404_for_unknown_run(client):
    response = client.get("/api/runs/no-such-run-id")
    assert response.status_code == 404


@skip_if_no_firestore
def test_run_detail_and_lineage_for_a_real_run(client):
    runs = client.get("/api/runs").json()
    if not runs:
        pytest.skip("no runs exist yet to inspect")
    run_id = runs[0]["run_id"]

    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["run_id"] == run_id
    assert "state_history" in detail

    lineage = client.get(f"/api/runs/{run_id}/lineage").json()
    assert "nodes" in lineage and "edges" in lineage


@skip_if_no_firestore
def test_registry_endpoint_returns_seeded_agents(client):
    response = client.get("/api/registry")
    assert response.status_code == 200
    registry = response.json()
    # infrastructure/seed_registry.py publishes these — if this fails,
    # the seed script hasn't been run yet, not necessarily an API bug.
    if registry:
        assert isinstance(next(iter(registry.values())), list)


@skip_if_no_firestore
def test_approve_rejects_a_run_not_ready_for_approval(authed_client):
    from agents.orchestrator.run_lifecycle import create_run, delete_run

    run_id = create_run("test.frontend.approve_guard")  # fresh run, state=REQUESTED
    try:
        response = authed_client.post(f"/api/runs/{run_id}/approve", json={"justification": "test justification"})
        assert response.status_code == 409
    finally:
        delete_run(run_id)  # a leftover run here becomes the dashboard's "active run" — see test_state_machine.py


@skip_if_no_firestore
def test_approve_rejects_unauthenticated_request(client):
    """Day 10 hardening: no Authorization header at all -> 401, not a
    silent client-supplied-identity approval. Uses the plain `client`
    fixture (no dependency override) specifically so the real
    get_approver_identity dependency runs."""
    from agents.orchestrator.run_lifecycle import create_run, delete_run

    run_id = create_run("test.frontend.approve_auth_guard")
    try:
        response = client.post(f"/api/runs/{run_id}/approve", json={"justification": "test justification"})
        assert response.status_code == 401
    finally:
        delete_run(run_id)


@skip_if_no_firestore
def test_approve_rejects_missing_justification(authed_client):
    from agents.orchestrator.run_lifecycle import create_run, delete_run

    run_id = create_run("test.frontend.approve_justification_guard")
    try:
        response = authed_client.post(f"/api/runs/{run_id}/approve", json={})
        assert response.status_code == 422
    finally:
        delete_run(run_id)


@pytest.mark.requires_firestore
def test_response_has_security_headers(client):
    """Day 10 hardening: CSP + hardening headers on every response, not
    just the approve endpoint."""
    response = client.get("/api/runs")
    assert "Content-Security-Policy" in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.requires_firestore
def test_csp_allows_the_hosts_firebase_signin_popup_actually_needs(client):
    """Regression test for a real bug found live: signInWithPopup()'s
    gapi-based CORS-relay iframe is served from apis.google.com, and the
    OAuth handshake itself touches accounts.google.com — NEITHER is
    covered by the `*.googleapis.com` wildcard (apis.google.com and
    googleapis.com are unrelated domains). Without both allowlisted, the
    SDK's own network calls get silently CSP-blocked and Google Sign-In
    fails with an opaque `auth/internal-error` — reproduced and fixed
    live, see frontend/app.py's `_add_security_headers` docstring."""
    csp = client.get("/api/runs").headers["Content-Security-Policy"]
    for host in ("https://apis.google.com", "https://accounts.google.com"):
        assert host in csp, f"{host!r} missing from CSP — will break Google Sign-In's popup flow"
    script_directive = next(
        directive.strip()
        for directive in csp.split(";")
        if directive.strip().startswith("script-src")
    )
    assert "https://apis.google.com" in script_directive


@pytest.mark.requires_firestore
def test_run_plan_404_for_run_without_a_plan(client):
    from agents.orchestrator.run_lifecycle import create_run, delete_run

    run_id = create_run("test.frontend.plan_guard")
    try:
        response = client.get(f"/api/runs/{run_id}/plan")
        assert response.status_code == 404
    finally:
        delete_run(run_id)


def test_fleet_health_unknown_with_no_runs():
    from frontend.app import _compute_fleet_health

    assert _compute_fleet_health([]) == "UNKNOWN"


def test_fleet_health_healthy_when_no_stuck_runs():
    from frontend.app import _compute_fleet_health

    runs = [{"state": "COMPLETE", "state_history": []}]
    assert _compute_fleet_health(runs) == "HEALTHY"


def test_fleet_health_degraded_when_a_run_is_stuck_failed():
    import datetime as dt

    from frontend.app import _compute_fleet_health

    stale_at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
    runs = [{"state": "FAILED", "state_history": [{"state": "FAILED", "at": stale_at}]}]
    assert _compute_fleet_health(runs) == "DEGRADED"


def test_fleet_health_healthy_when_failure_is_recent():
    """A run that just failed and is still actively being worked (recent
    transition) isn't 'degraded' yet — only a stale, stuck one is."""
    import datetime as dt

    from frontend.app import _compute_fleet_health

    recent_at = dt.datetime.now(dt.timezone.utc).isoformat()
    runs = [{"state": "FAILED", "state_history": [{"state": "FAILED", "at": recent_at}]}]
    assert _compute_fleet_health(runs) == "HEALTHY"


# --- Approver authorization allowlist (fix pass after the second audit) --


def test_is_allowlisted_exact_email_match():
    from frontend.app import _is_allowlisted

    assert _is_allowlisted("ops-lead@example.internal", ["ops-lead@example.internal"]) is True
    assert _is_allowlisted("someone-else@example.internal", ["ops-lead@example.internal"]) is False


def test_is_allowlisted_domain_match():
    from frontend.app import _is_allowlisted

    assert _is_allowlisted("anyone@example.internal", ["@example.internal"]) is True
    assert _is_allowlisted("anyone@other.internal", ["@example.internal"]) is False


def test_is_allowlisted_case_insensitive():
    from frontend.app import _is_allowlisted

    assert _is_allowlisted("Ops-Lead@Example.Internal", ["ops-lead@example.internal"]) is True


def test_is_allowlisted_empty_list_denies_everyone():
    """Fails closed: an unconfigured allowlist authorizes nobody, it
    doesn't default to allowing every authenticated user."""
    from frontend.app import _is_allowlisted

    assert _is_allowlisted("anyone@example.internal", []) is False


def test_load_approver_allowlist_parses_comma_separated_env(monkeypatch):
    from frontend.app import _load_approver_allowlist

    monkeypatch.setenv("APPROVER_ALLOWLIST", " ops-lead@example.internal , @example.internal ,")
    assert _load_approver_allowlist() == ["ops-lead@example.internal", "@example.internal"]


def test_load_approver_allowlist_empty_when_unset(monkeypatch):
    from frontend.app import _load_approver_allowlist

    monkeypatch.delenv("APPROVER_ALLOWLIST", raising=False)
    assert _load_approver_allowlist() == []


def test_get_approver_identity_denies_authenticated_but_unlisted_user(monkeypatch):
    """A real (simulated) valid Firebase token for someone NOT on the
    allowlist must be rejected with 403 — authentication proving identity
    is not the same as authorization to approve."""
    import firebase_admin
    import firebase_admin.auth as firebase_auth_module

    from frontend.app import get_approver_identity

    monkeypatch.setenv("APPROVER_ALLOWLIST", "ops-lead@example.internal")
    monkeypatch.setattr(
        firebase_auth_module, "verify_id_token", lambda token: {"email": "random-gmail-user@gmail.com"}
    )
    monkeypatch.setattr(firebase_admin, "_apps", {"[DEFAULT]": object()})  # skip initialize_app()

    with pytest.raises(HTTPException) as exc_info:
        get_approver_identity(authorization="Bearer fake-but-valid-looking-token")
    assert exc_info.value.status_code == 403


def test_get_approver_identity_allows_allowlisted_user(monkeypatch):
    import firebase_admin
    import firebase_admin.auth as firebase_auth_module

    from frontend.app import get_approver_identity

    monkeypatch.setenv("APPROVER_ALLOWLIST", "ops-lead@example.internal")
    monkeypatch.setattr(
        firebase_auth_module, "verify_id_token", lambda token: {"email": "ops-lead@example.internal"}
    )
    monkeypatch.setattr(firebase_admin, "_apps", {"[DEFAULT]": object()})

    assert get_approver_identity(authorization="Bearer fake-but-valid-looking-token") == "ops-lead@example.internal"
