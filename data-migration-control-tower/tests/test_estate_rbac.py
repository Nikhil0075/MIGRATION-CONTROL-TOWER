"""Per-estate authorization (Day 11 Phase 5).

A global `operator` role was defensible while the platform ran one
estate. Once a deployment hosts several, it means someone onboarded for
one customer's estate can start runs against another's.

The test that matters most here is the last one:
test_every_mutating_route_calls_authorize_estate. Estate authorization
cannot be a FastAPI dependency, because a dependency cannot cleanly read
an arbitrary request-body field — so it is an explicit call inside each
handler, and an explicit call is exactly the kind of thing a future
endpoint forgets. That test enumerates the router and fails if any
mutating route omits it.
"""

from __future__ import annotations

import inspect
import re

import firebase_admin
import pytest
from fastapi.testclient import TestClient

from frontend import api_v1
from frontend.app import app
from frontend.security import (
    ESTATE_GRANT_SOFT_LIMIT,
    WILDCARD_ESTATE,
    UserContext,
    _estate_roles_from_claims,
    _roles_from_claims,
    authorize_estate,
    get_user_context,
)
from fastapi import HTTPException


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _token(monkeypatch: pytest.MonkeyPatch, claims: dict) -> None:
    from firebase_admin import auth

    monkeypatch.delenv("APPROVER_ALLOWLIST", raising=False)
    monkeypatch.setattr(firebase_admin, "_apps", {"test": object()})
    monkeypatch.setattr(
        auth,
        "verify_id_token",
        lambda _t: {"uid": "rbac-user", "email": "rbac-user@example.internal", **claims},
    )


def _headers(**extra: str) -> dict[str, str]:
    return {"Authorization": "Bearer verified", **extra}


def _context(**claims) -> UserContext:
    grants = _estate_roles_from_claims(claims)
    union = frozenset().union(*grants.values()) if grants else frozenset()
    return UserContext(uid="u", email="u@example.internal", roles=union, estate_roles=grants)


# ---------------------------------------------------------------------------
# Claim parsing — backward compatibility first
# ---------------------------------------------------------------------------


def test_legacy_roles_list_is_treated_as_a_global_grant():
    """Every token minted before Phase 5 uses this shape and must keep
    working — a claim migration that logs everyone out is not acceptable
    for an authorization refactor."""
    user = _context(roles=["operator"])
    assert user.has_role("operator", "any-estate")
    assert user.has_role("viewer", "another-estate")


def test_legacy_roles_string_is_accepted():
    assert _context(roles="approver").has_role("approver", "any-estate")


def test_legacy_roles_mapping_is_accepted():
    assert _context(roles={"operator": True, "approver": False}).has_role("operator", "e")
    assert not _context(roles={"operator": True, "approver": False}).has_role("approver", "e")


def test_estate_scoped_grant_applies_only_to_that_estate():
    user = _context(estate_roles={"acme": ["operator"]})
    assert user.has_role("operator", "acme")
    assert not user.has_role("operator", "other-estate")


def test_wildcard_grant_applies_everywhere():
    user = _context(estate_roles={WILDCARD_ESTATE: ["viewer"], "acme": ["operator"]})
    assert user.has_role("viewer", "anything")
    assert user.has_role("operator", "acme")
    assert not user.has_role("operator", "anything")


def test_both_claim_shapes_can_coexist():
    user = _context(roles=["viewer"], estate_roles={"acme": ["operator"]})
    assert user.has_role("viewer", "other")
    assert user.has_role("operator", "acme")
    assert not user.has_role("operator", "other")


def test_elevated_roles_still_inherit_viewer_per_estate():
    user = _context(estate_roles={"acme": ["operator"]})
    assert user.has_role("viewer", "acme")


def test_unknown_roles_are_discarded():
    user = _context(estate_roles={"acme": ["superuser", "operator"]})
    assert user.roles_for("acme") == frozenset({"operator", "viewer"})


def test_roles_union_reports_every_role_held_anywhere():
    """GET /api/v1/session reports this flat list; the console's coarse
    "can this user act at all?" checks depend on it."""
    assert _roles_from_claims({"estate_roles": {"a": ["operator"], "b": ["approver"]}}) == {
        "operator", "approver", "viewer",
    }


def test_roles_for_none_returns_only_wildcard_grants():
    user = _context(estate_roles={WILDCARD_ESTATE: ["viewer"], "acme": ["operator"]})
    assert user.roles_for(None) == frozenset({"viewer"})


def test_scoped_estates_excludes_the_wildcard():
    user = _context(estate_roles={WILDCARD_ESTATE: ["viewer"], "acme": ["operator"]})
    assert user.scoped_estates == ["acme"]


def test_a_token_with_no_recognized_role_gets_nothing():
    user = _context(roles=["nonsense"])
    assert user.roles == frozenset()
    assert not user.has_role("viewer", "acme")


# ---------------------------------------------------------------------------
# authorize_estate
# ---------------------------------------------------------------------------


def test_authorize_estate_allows_a_scoped_operator():
    authorize_estate(_context(estate_roles={"acme": ["operator"]}), "acme", "operator")


def test_authorize_estate_denies_another_estate():
    user = _context(estate_roles={"acme": ["operator"]})
    with pytest.raises(HTTPException) as exc:
        authorize_estate(user, "other", "operator")
    assert exc.value.status_code == 403


def test_authorization_failure_does_not_enumerate_other_grants():
    """Knowing which estates exist is itself information a caller may not
    be entitled to."""
    user = _context(estate_roles={"secret-customer": ["operator"]})
    with pytest.raises(HTTPException) as exc:
        authorize_estate(user, "other", "operator")
    assert "secret-customer" not in str(exc.value.detail)


def test_approver_allowlist_remains_a_global_grant(monkeypatch):
    """It predates estate scoping and has no estate to attach to; silently
    narrowing it would lock out the existing approval flow."""
    monkeypatch.setenv("APPROVER_ALLOWLIST", "ops-lead@example.internal")
    monkeypatch.setattr(firebase_admin, "_apps", {"test": object()})
    from firebase_admin import auth

    monkeypatch.setattr(
        auth, "verify_id_token",
        lambda _t: {"uid": "u", "email": "ops-lead@example.internal"},
    )
    user = get_user_context("Bearer t")
    assert user.has_role("approver", "any-estate-at-all")


# ---------------------------------------------------------------------------
# End-to-end through the API
# ---------------------------------------------------------------------------


def test_operator_on_one_estate_cannot_start_a_run_on_another(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"acme-legacy": ["operator"]}})
    response = client.post(
        "/api/v1/runs",
        headers=_headers(**{"Idempotency-Key": "rbac-cross-estate-001"}),
        json={
            "pipeline_id": "wwi.sales.customers",
            "estate_id": "wwi-demo-estate",
            "justification": "Cross-estate authorization check",
        },
    )
    assert response.status_code == 403
    assert "wwi-demo-estate" in response.json()["detail"]


def test_operator_can_start_a_run_on_their_own_estate(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"wwi-demo-estate": ["operator"]}})
    monkeypatch.setattr(
        "frontend.api_v1.queue_operation",
        lambda **kwargs: {"operation_id": "op_rbac", "status": "published", "event": kwargs["event"]},
    )
    response = client.post(
        "/api/v1/runs",
        headers=_headers(**{"Idempotency-Key": "rbac-same-estate-001"}),
        json={
            "pipeline_id": "wwi.sales.customers",
            "estate_id": "wwi-demo-estate",
            "justification": "Same-estate authorization check",
        },
    )
    assert response.status_code == 202
    # The estate must ride along on the event so create_run records it.
    assert response.json()["data"]["event"]["estate_id"] == "wwi-demo-estate"


def test_assessment_is_estate_scoped(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"acme-legacy": ["operator"]}})
    response = client.post(
        "/api/v1/assessments",
        headers=_headers(**{"Idempotency-Key": "rbac-assessment-001"}),
        json={
            "pack_id": "wwi_sqlserver_v1",
            "estate_id": "wwi-demo-estate",
            "justification": "Cross-estate assessment check",
        },
    )
    assert response.status_code == 403


def test_wave_override_authorizes_the_estate_in_the_wave_key(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"acme-legacy": ["operator"]}})
    response = client.put(
        "/api/v1/waves/wwi-demo-estate:wwi-sqlserver/override",
        headers=_headers(**{"Idempotency-Key": "rbac-wave-001"}),
        json={"state": "HOLD", "justification": "Cross-estate wave override check"},
    )
    assert response.status_code == 403


def test_a_legacy_global_operator_token_still_works(client, monkeypatch):
    _token(monkeypatch, {"roles": ["operator"]})
    monkeypatch.setattr(
        "frontend.api_v1.queue_operation",
        lambda **kwargs: {"operation_id": "op_legacy", "status": "published"},
    )
    response = client.post(
        "/api/v1/runs",
        headers=_headers(**{"Idempotency-Key": "rbac-legacy-token-001"}),
        json={"pipeline_id": "wwi.sales.customers", "justification": "Legacy claim shape check"},
    )
    assert response.status_code == 202


# ---------------------------------------------------------------------------
# The guard that keeps this true as routes are added
# ---------------------------------------------------------------------------

_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _mutating_routes():
    for route in api_v1.router.routes:
        methods = getattr(route, "methods", set()) & _MUTATING_METHODS
        if methods:
            yield route


def test_there_are_mutating_routes_to_check():
    """Guards the guard: if the enumeration below ever finds nothing, the
    next test would pass vacuously."""
    assert list(_mutating_routes()), "expected the v1 router to expose mutating routes"


def test_every_mutating_route_calls_authorize_estate():
    """Estate authorization is an explicit in-handler call, not a
    dependency — a FastAPI dependency cannot read an arbitrary
    request-body field. Explicit calls are exactly what a new endpoint
    forgets, so this enumerates the router rather than trusting review.
    """
    missing = []
    for route in _mutating_routes():
        source = inspect.getsource(route.endpoint)
        if not re.search(r"\bauthorize_estate\s*\(", source):
            missing.append(f"{sorted(route.methods)} {route.path} -> {route.endpoint.__name__}")

    assert not missing, (
        "these mutating routes never call authorize_estate(), so a user holding a role "
        "on ANY estate could act on EVERY estate through them:\n  " + "\n  ".join(missing)
    )


def test_every_mutating_route_still_requires_a_coarse_role():
    """authorize_estate alone is not enough — an unauthenticated caller
    must be rejected before any handler body runs."""
    unguarded = []
    for route in _mutating_routes():
        params = inspect.signature(route.endpoint).parameters
        if not any(
            "require_role" in str(param.default) for param in params.values()
        ):
            unguarded.append(f"{sorted(route.methods)} {route.path}")
    assert not unguarded, f"mutating routes without a require_role dependency: {unguarded}"


def test_estate_grant_soft_limit_is_documented():
    """Firebase custom claims cap near 1000 bytes; past this many grants a
    token stops minting. Recorded as a constant so the ceiling is visible
    rather than discovered in production."""
    assert 5 <= ESTATE_GRANT_SOFT_LIMIT <= 50


# --- Execution profiles are estate-scoped (Day 11 Phase 9) ----------------


def test_a_profile_from_another_estate_is_rejected(client, monkeypatch):
    """`execution_profile` used to default to "wwi-default" for every
    estate, so a run started against a newly onboarded estate silently
    carried the demo estate's profile. Nothing rejected it — the mismatch
    only surfaced later, inside the run."""
    _token(monkeypatch, {"roles": ["operator"]})
    monkeypatch.setattr(
        "frontend.api_v1._execution_profiles_for",
        lambda _estate: ["postgres_retail_v1"],
    )
    response = client.post(
        "/api/v1/runs",
        headers=_headers(**{"Idempotency-Key": "profile-mismatch-0001"}),
        json={
            "pipeline_id": "retail.customers",
            "estate_id": "retail-postgres-estate",
            "execution_profile": "wwi-default",
            "justification": "Profile belongs to a different estate",
        },
    )
    assert response.status_code == 422
    assert "not offered by estate" in response.json()["detail"]


def test_an_omitted_profile_resolves_to_the_estates_own(client, monkeypatch):
    _token(monkeypatch, {"roles": ["operator"]})
    monkeypatch.setattr(
        "frontend.api_v1._execution_profiles_for", lambda _estate: ["postgres_retail_v1"]
    )
    captured = {}
    monkeypatch.setattr(
        "frontend.api_v1.queue_operation",
        lambda **kwargs: captured.update(kwargs) or {"operation_id": "op", "status": "published"},
    )
    response = client.post(
        "/api/v1/runs",
        headers=_headers(**{"Idempotency-Key": "profile-resolve-0002"}),
        json={
            "pipeline_id": "retail.customers",
            "estate_id": "retail-postgres-estate",
            "justification": "Server resolves the estate's own profile",
        },
    )
    assert response.status_code == 202
    assert captured["event"]["execution_profile"] == "postgres_retail_v1"


def test_an_ambiguous_profile_must_be_chosen_not_guessed(client, monkeypatch):
    """With several profiles the server names them rather than picking
    one arbitrarily."""
    _token(monkeypatch, {"roles": ["operator"]})
    monkeypatch.setattr(
        "frontend.api_v1._execution_profiles_for", lambda _estate: ["a_v1", "b_v1"]
    )
    response = client.post(
        "/api/v1/runs",
        headers=_headers(**{"Idempotency-Key": "profile-ambiguous-0003"}),
        json={
            "pipeline_id": "x.y",
            "estate_id": "multi-profile-estate",
            "justification": "Ambiguous execution profile",
        },
    )
    assert response.status_code == 422
    assert "name the one to use" in response.json()["detail"]
