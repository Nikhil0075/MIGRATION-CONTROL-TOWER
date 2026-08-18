"""Estate onboarding endpoints (Day 11 Phase 6).

These are the API the onboarding wizard drives: describe the estate,
bind credential references, validate the connection, save. The tests that
matter most are the ones proving a credential can neither be submitted
nor returned — everything else here is ordinary CRUD.

Live Firestore, like the rest of this suite; every test that writes uses a
unique estate_id and deletes it in teardown.
"""

from __future__ import annotations

import uuid

import firebase_admin
import pytest
from fastapi.testclient import TestClient

from frontend.app import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _operator(monkeypatch: pytest.MonkeyPatch):
    from firebase_admin import auth

    monkeypatch.delenv("APPROVER_ALLOWLIST", raising=False)
    monkeypatch.setattr(firebase_admin, "_apps", {"test": object()})
    monkeypatch.setattr(
        auth,
        "verify_id_token",
        lambda _t: {"uid": "op", "email": "op@example.internal", "roles": ["operator"]},
    )


@pytest.fixture()
def estate_cleanup():
    created: list[str] = []
    yield created
    from tools.estate_registry import delete_estate

    for estate_id in created:
        try:
            delete_estate(estate_id)
        except Exception:  # noqa: BLE001
            pass


def _headers(key: str = "estate-api-test-key-0001") -> dict[str, str]:
    return {"Authorization": "Bearer t", "Idempotency-Key": key}


def _payload(estate_id: str, **overrides) -> dict:
    body = {
        "estate_id": estate_id,
        "display_name": "Test estate",
        "sources": [
            {
                "source_id": "primary",
                "adapter": "sqlserver",
                "config": {"database": "WideWorldImporters"},
                "connection_profile": {
                    "host_env": "SQLSERVER_HOST",
                    "port_env": "SQLSERVER_PORT",
                    "user_env": "SQLSERVER_USER",
                    "password_env": "SQLSERVER_PASSWORD",
                },
            }
        ],
        "target": {"system": "bigquery", "dataset_env": "BQ_DATASET"},
        "justification": "Onboarding an estate for the contract tests",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Credential containment — the guarantee the whole wizard rests on
# ---------------------------------------------------------------------------


def test_a_connection_profile_carrying_a_password_is_rejected(client, estate_cleanup):
    """The request model forbids extra keys precisely so a caller cannot
    smuggle a credential value into a document that gets persisted and
    returned by GET /estates."""
    body = _payload(f"test-{uuid.uuid4().hex[:8]}")
    body["sources"][0]["connection_profile"]["password"] = "hunter2"
    response = client.post("/api/v1/estates", headers=_headers(), json=body)
    assert response.status_code == 422
    assert "hunter2" not in response.text


def test_created_estate_response_contains_no_credential(client, estate_cleanup):
    estate_id = f"test-{uuid.uuid4().hex[:8]}"
    estate_cleanup.append(estate_id)
    response = client.post("/api/v1/estates", headers=_headers(), json=_payload(estate_id))
    assert response.status_code == 201
    source = response.json()["data"]["sources"][0]
    assert "password" not in str(source).lower() or source["connection"]["credential_source"]
    assert "SQLSERVER_PASSWORD" not in str(source.get("connection"))


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@pytest.mark.requires_firestore
def test_create_then_fetch_an_estate(client, estate_cleanup):
    estate_id = f"test-{uuid.uuid4().hex[:8]}"
    estate_cleanup.append(estate_id)

    created = client.post("/api/v1/estates", headers=_headers(), json=_payload(estate_id))
    assert created.status_code == 201

    fetched = client.get(f"/api/v1/estates/{estate_id}", headers={"Authorization": "Bearer t"})
    assert fetched.status_code == 200
    assert fetched.json()["data"]["display_name"] == "Test estate"
    assert fetched.json()["data"]["origin"] == "wizard"


@pytest.mark.requires_firestore
def test_creating_a_duplicate_estate_conflicts(client, estate_cleanup):
    estate_id = f"test-{uuid.uuid4().hex[:8]}"
    estate_cleanup.append(estate_id)
    client.post("/api/v1/estates", headers=_headers(), json=_payload(estate_id))
    again = client.post("/api/v1/estates", headers=_headers("second-key-0002"), json=_payload(estate_id))
    assert again.status_code == 409


@pytest.mark.requires_firestore
def test_unknown_estate_returns_404(client):
    response = client.get(f"/api/v1/estates/absent-{uuid.uuid4().hex[:6]}", headers={"Authorization": "Bearer t"})
    assert response.status_code == 404


@pytest.mark.requires_firestore
def test_patch_updates_and_audits(client, estate_cleanup):
    estate_id = f"test-{uuid.uuid4().hex[:8]}"
    estate_cleanup.append(estate_id)
    client.post("/api/v1/estates", headers=_headers(), json=_payload(estate_id))

    patched = client.patch(
        f"/api/v1/estates/{estate_id}",
        headers=_headers("patch-key-0003"),
        json={"display_name": "Renamed estate", "justification": "Renaming for the contract test"},
    )
    assert patched.status_code == 200
    assert patched.json()["data"]["display_name"] == "Renamed estate"


@pytest.mark.requires_firestore
def test_patch_with_no_changes_is_rejected(client, estate_cleanup):
    estate_id = f"test-{uuid.uuid4().hex[:8]}"
    estate_cleanup.append(estate_id)
    client.post("/api/v1/estates", headers=_headers(), json=_payload(estate_id))
    response = client.patch(
        f"/api/v1/estates/{estate_id}",
        headers=_headers("empty-patch-0004"),
        json={"justification": "Nothing actually changes here"},
    )
    assert response.status_code == 422


@pytest.mark.requires_firestore
def test_delete_is_a_soft_disable(client, estate_cleanup):
    """Run history references estate_id; removing the document would make
    that history uninterpretable."""
    estate_id = f"test-{uuid.uuid4().hex[:8]}"
    estate_cleanup.append(estate_id)
    client.post("/api/v1/estates", headers=_headers(), json=_payload(estate_id))

    disabled = client.request(
        "DELETE",
        f"/api/v1/estates/{estate_id}",
        headers=_headers("disable-key-0005"),
        json={"justification": "Decommissioning for the contract test"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["data"]["status"] == "DISABLED"

    # Still fetchable — disabled, not gone.
    assert client.get(f"/api/v1/estates/{estate_id}", headers={"Authorization": "Bearer t"}).status_code == 200


@pytest.mark.requires_firestore
def test_disabling_an_estate_with_in_flight_runs_is_refused(client, estate_cleanup, monkeypatch):
    """Disabling an estate underneath work still in flight would strand
    that work with no operator-visible cause."""
    estate_id = f"test-{uuid.uuid4().hex[:8]}"
    estate_cleanup.append(estate_id)
    client.post("/api/v1/estates", headers=_headers(), json=_payload(estate_id))

    monkeypatch.setattr(
        "frontend.api_v1._all_runs",
        lambda _limit=500, estate_id=None: [{"run_id": "run-x", "state": "MIGRATING", "estate_id": estate_id}],
    )
    response = client.request(
        "DELETE",
        f"/api/v1/estates/{estate_id}",
        headers=_headers("blocked-disable-0006"),
        json={"justification": "Should be refused while a run is in flight"},
    )
    assert response.status_code == 409
    assert "in flight" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Validation and discovery
# ---------------------------------------------------------------------------


@pytest.mark.requires_firestore
def test_validate_unknown_source_returns_404(client, estate_cleanup):
    estate_id = f"test-{uuid.uuid4().hex[:8]}"
    estate_cleanup.append(estate_id)
    client.post("/api/v1/estates", headers=_headers(), json=_payload(estate_id))

    response = client.post(
        f"/api/v1/estates/{estate_id}/sources/nope/validate", headers={"Authorization": "Bearer t"}
    )
    assert response.status_code == 404


@pytest.mark.requires_firestore
def test_validating_a_static_file_source_is_not_applicable(client, estate_cleanup):
    """The Oracle DDL corpus has no server to reach. Reporting that
    honestly beats a fabricated success or a confusing failure."""
    estate_id = f"test-{uuid.uuid4().hex[:8]}"
    estate_cleanup.append(estate_id)
    body = _payload(estate_id)
    body["sources"] = [{
        "source_id": "corpus", "adapter": "oracle_corpus",
        "config": {"corpus_path": "simulator/source_setup/oracle_dialect_corpus"},
        "connection_profile": None,
    }]
    client.post("/api/v1/estates", headers=_headers(), json=body)

    result = client.post(
        f"/api/v1/estates/{estate_id}/sources/corpus/validate",
        headers={"Authorization": "Bearer t"},
    ).json()["data"]
    assert result["status"] == "NOT_APPLICABLE"


@pytest.mark.requires_sqlserver
@pytest.mark.requires_firestore
def test_validate_reaches_the_live_source_without_leaking_the_credential(client, estate_cleanup):
    estate_id = f"test-{uuid.uuid4().hex[:8]}"
    estate_cleanup.append(estate_id)
    client.post("/api/v1/estates", headers=_headers(), json=_payload(estate_id))

    response = client.post(
        f"/api/v1/estates/{estate_id}/sources/primary/validate",
        headers={"Authorization": "Bearer t"},
    )
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["status"] == "HEALTHY"
    assert body["object_count"] > 0
    # The probe reports HOW it authenticated, never WITH WHAT.
    import os

    password = os.environ.get("SQLSERVER_PASSWORD")
    if password:
        assert password not in response.text


def test_adapter_types_are_discoverable(client):
    """The wizard renders its adapter picker from this, so registering a
    new adapter needs no frontend change."""
    types = client.get("/api/v1/adapter-types", headers={"Authorization": "Bearer t"}).json()["data"]
    by_name = {t["adapter_type"]: t for t in types}
    assert {"sqlserver", "oracle_corpus", "dag_artifacts"} <= set(by_name)
    assert "transfer" in by_name["sqlserver"]["capabilities"]
    assert "transfer" not in by_name["oracle_corpus"]["capabilities"]


def test_pack_execution_support_is_derived_not_hardcoded(client):
    packs = {p["pack_id"]: p for p in client.get("/api/v1/assessments", headers={"Authorization": "Bearer t"}).json()["data"]["packs"]}
    assert packs["wwi_sqlserver_v1"]["execution_supported"] is True
    assert packs["oracle_corpus_v1"]["execution_supported"] is False


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


def test_viewer_cannot_create_an_estate(client, monkeypatch):
    from firebase_admin import auth

    monkeypatch.setattr(
        auth, "verify_id_token",
        lambda _t: {"uid": "v", "email": "v@example.internal", "roles": ["viewer"]},
    )
    response = client.post("/api/v1/estates", headers=_headers(), json=_payload("viewer-denied"))
    assert response.status_code == 403


def test_create_requires_an_idempotency_key(client):
    response = client.post(
        "/api/v1/estates",
        headers={"Authorization": "Bearer t"},
        json=_payload("missing-key-estate"),
    )
    assert response.status_code == 422


def test_validation_errors_do_not_echo_the_rejected_value(client):
    """FastAPI's default 422 handler includes the rejected input. Helpful
    for a malformed page number; actively harmful for a credential, which
    would land in the caller's console and any log recording response
    bodies. The rejection was already correct — this stops the rejection
    itself from becoming the disclosure."""
    body = _payload("echo-check-estate")
    body["sources"][0]["connection_profile"]["password"] = "super-secret-value"
    response = client.post("/api/v1/estates", headers=_headers(), json=body)

    assert response.status_code == 422
    assert "super-secret-value" not in response.text
    # ...but the caller still learns which field failed and why.
    detail = response.json()["detail"]
    assert any("password" in str(item.get("loc", "")) for item in detail)
    assert all("msg" in item for item in detail)
