"""Tests for GET /api/v1/evaluations (Deploy & Harden Phase 4d) — the
three-panel scale-evidence surfacing (control-plane tiers, data-plane
rows-moved, operational load), extending the existing endpoint rather
than replacing it (scale_metrics/scale_report_* keep their original
shape so nothing already reading them breaks).

No existing test in this suite exercises a get_user_context-gated
endpoint yet — this establishes that pattern (a fake wildcard-viewer
UserContext via dependency_overrides), the same approach
tests/test_frontend_api.py already uses for get_approver_identity.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _firestore_reachable() -> bool:
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")


@pytest.fixture()
def viewer_client(monkeypatch):
    """No existing test in this suite exercises a get_user_context-gated
    endpoint, so this establishes the real pattern needed —
    frontend/api_v1.py's router applies `Depends(require_role("viewer"))`
    at the ROUTER level (frontend/security.py::require_role()), and that
    factory's returned closure calls `get_user_context(authorization)` as
    a plain Python function call, NOT as a nested `Depends()` — so
    `app.dependency_overrides[get_user_context]` alone (which only
    intercepts FastAPI-DI-resolved Depends() sites, e.g. `_scope_reads`
    and the endpoint's own `user: UserContext = Depends(get_user_context)`
    parameter) never reaches it, and the router-level gate keeps 401ing
    even with that override in place. Fixed by ALSO monkeypatching the
    module-level function `frontend.security.get_user_context` refers
    to — `require_role()`'s closure looks that name up dynamically in
    `security.py`'s own module namespace at call time, so reassigning
    the attribute (not just overriding the Depends() target) reaches it.
    """
    from fastapi.testclient import TestClient

    from frontend import security as security_module
    from frontend.app import app
    from frontend.security import UserContext, WILDCARD_ESTATE

    fake_user = UserContext(
        uid="test-uid", email="viewer@example.internal",
        roles=frozenset({"viewer"}), estate_roles={WILDCARD_ESTATE: frozenset({"viewer"})},
    )
    original = security_module.get_user_context
    app.dependency_overrides[original] = lambda: fake_user
    monkeypatch.setattr(security_module, "get_user_context", lambda authorization=None: fake_user)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(original, None)


@skip_if_no_firestore
def test_evaluations_endpoint_returns_the_three_panel_shape(viewer_client):
    response = viewer_client.get("/api/v1/evaluations")
    assert response.status_code == 200
    data = response.json()["data"]
    # Original keys still present — nothing that already reads this
    # endpoint should need to change.
    assert "scale_metrics" in data
    assert "scale_report_status" in data
    # New Phase 4d keys.
    assert "scale_metrics_by_tier" in data
    assert "data_plane_metrics" in data
    assert "data_plane_status" in data
    assert "operational_load_metrics" in data
    assert "operational_load_status" in data


@skip_if_no_firestore
def test_evaluations_endpoint_surfaces_a_real_control_plane_tier(viewer_client):
    from tools.firestore_client import get_client

    tier = 300_000 + uuid.uuid4().int % 1000
    reports = get_client().collection("evaluation_scale_reports")
    reports.document(str(tier)).set({"pipeline_count": tier, "model_calls": 0})
    try:
        response = viewer_client.get("/api/v1/evaluations")
        data = response.json()["data"]
        assert str(tier) in data["scale_metrics_by_tier"]
        assert data["scale_metrics_by_tier"][str(tier)]["pipeline_count"] == tier
    finally:
        reports.document(str(tier)).delete()


@skip_if_no_firestore
def test_evaluations_endpoint_reports_a_reason_when_no_data_plane_report_is_present(viewer_client):
    """Whichever status this dev environment's evaluation_data_plane_reports
    collection is actually in (empty until evaluation/data_plane_scale_test.py
    is run for real), the two fields must be internally consistent — a
    reason is given if and only if the status says not_configured."""
    response = viewer_client.get("/api/v1/evaluations")
    data = response.json()["data"]
    if data["data_plane_status"] == "not_configured":
        assert data["data_plane_metrics"] is None
        assert "data_plane_scale_test.py" in data["data_plane_reason"]
    else:
        assert data["data_plane_status"] == "available"
        assert data["data_plane_metrics"] is not None
        assert data["data_plane_reason"] is None
