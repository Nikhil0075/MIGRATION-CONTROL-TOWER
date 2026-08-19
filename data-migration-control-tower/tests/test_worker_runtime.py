"""The supervisor's attachment to the web server, and its console API.

Two things are worth pinning here that the supervisor's own tests cannot
see, because both are properties of the process rather than of the loop:

  - the test suite must NEVER start real consumers (conftest.py forces
    CONTROL_TOWER_WORKERS=0). A test session that pulled live
    subscriptions would consume the developer's own console messages;
  - a process with workers disabled must still ANSWER /workers. "Nothing
    is happening" with an empty table is the exact confusion the whole
    change exists to remove.
"""

from __future__ import annotations

import firebase_admin
import pytest
from fastapi.testclient import TestClient

from frontend import worker_runtime
from frontend.app import app


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
        lambda _t: {"uid": "worker-user", "email": "worker-user@example.internal", **claims},
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer verified"}


@pytest.fixture(autouse=True)
def _no_leaked_supervisor():
    """No test here may leave a supervisor running for the next module."""
    yield
    worker_runtime._supervisor = None
    worker_runtime._disabled_reason = None


# ---------------------------------------------------------------------------
# Startup policy
# ---------------------------------------------------------------------------


def test_the_test_suite_never_starts_workers(monkeypatch):
    """conftest.py sets this. If it ever stops doing so, a full test run
    starts eight threads against live Pub/Sub."""
    import os

    assert os.environ.get("CONTROL_TOWER_WORKERS") == "0"
    assert worker_runtime.start_supervisor() is None


def test_a_disabled_process_says_why_rather_than_showing_nothing(monkeypatch):
    monkeypatch.setenv("CONTROL_TOWER_WORKERS", "0")
    worker_runtime.start_supervisor()
    status = worker_runtime.status()
    assert status["enabled"] is False
    assert status["consumers"] == []
    assert "CONTROL_TOWER_WORKERS" in status["reason"]


def test_an_empty_consumer_selection_is_reported_not_silently_idle(monkeypatch):
    """CONTROL_TOWER_WORKER_CONSUMERS=typo would otherwise start a
    supervisor with zero consumers that looks healthy and does nothing."""
    monkeypatch.setenv("CONTROL_TOWER_WORKERS", "1")
    monkeypatch.setenv("CONTROL_TOWER_WORKER_CONSUMERS", "not-a-consumer")
    assert worker_runtime.start_supervisor() is None
    assert "selected no consumers" in worker_runtime.status()["reason"]


def test_a_failing_start_does_not_take_the_console_down(monkeypatch):
    """A console that will not boot is worse than one whose workers did
    not start: the operator loses the page that would have told them."""
    monkeypatch.setenv("CONTROL_TOWER_WORKERS", "1")
    monkeypatch.delenv("CONTROL_TOWER_WORKER_CONSUMERS", raising=False)

    class Boom:
        def start(self):
            raise RuntimeError("no credentials")

    monkeypatch.setattr(worker_runtime, "WorkerSupervisor", lambda *a, **k: Boom())
    assert worker_runtime.start_supervisor() is None
    assert "failed to start" in worker_runtime.status()["reason"]


def test_stop_is_safe_when_nothing_started():
    worker_runtime.stop_supervisor()  # must not raise


# ---------------------------------------------------------------------------
# The console API
# ---------------------------------------------------------------------------


def test_workers_status_is_readable_by_a_viewer(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})
    response = client.get("/api/v1/workers", headers=_headers())
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["enabled"] is False
    assert "reason" in body


def test_workers_status_requires_authentication(client):
    assert client.get("/api/v1/workers").status_code in (401, 403)


def test_pause_requires_the_operator_role(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"*": ["viewer"]}})
    response = client.post(
        "/api/v1/workers/plan/pause",
        headers=_headers(),
        json={"justification": "A viewer must not be able to halt the fleet."},
    )
    assert response.status_code == 403


def test_pause_reports_503_when_no_supervisor_runs_here(client, monkeypatch):
    """Not a 200. Reporting success for a pause that paused nothing is
    how an operator ends up believing work is stopped when it is not."""
    _token(monkeypatch, {"estate_roles": {"*": ["operator"]}})
    response = client.post(
        "/api/v1/workers/plan/pause",
        headers=_headers(),
        json={"justification": "Checking the disabled-process response."},
    )
    assert response.status_code == 503


def test_an_unknown_action_is_not_treated_as_a_pause(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"*": ["operator"]}})
    response = client.post(
        "/api/v1/workers/plan/halt",
        headers=_headers(),
        json={"justification": "Only pause and resume are real actions."},
    )
    assert response.status_code == 404


def test_pause_requires_a_justification(client, monkeypatch):
    _token(monkeypatch, {"estate_roles": {"*": ["operator"]}})
    response = client.post(
        "/api/v1/workers/plan/pause", headers=_headers(), json={"justification": "no"}
    )
    assert response.status_code == 422


def test_an_unknown_consumer_is_a_404_not_a_silent_success(client, monkeypatch):
    """Only reachable with a supervisor present, so one is installed by
    hand rather than started."""

    class FakeSupervisor:
        def set_paused(self, name, paused, *, actor, justification=""):
            raise KeyError(name)

    _token(monkeypatch, {"estate_roles": {"*": ["operator"]}})
    monkeypatch.setattr(worker_runtime, "_supervisor", FakeSupervisor())
    response = client.post(
        "/api/v1/workers/nonexistent/pause",
        headers=_headers(),
        json={"justification": "A typo must not look like it worked."},
    )
    assert response.status_code == 404
