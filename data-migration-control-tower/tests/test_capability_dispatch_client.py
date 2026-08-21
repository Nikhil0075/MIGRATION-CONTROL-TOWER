"""Tests for tools/capability_dispatch_client.py (Deploy & Harden
Phase 2c) — the calling side of typed HTTP capability dispatch. Mocks
`requests.post` and the OIDC token fetch, so these need neither a live
Cloud Run service nor live Google credentials — matching the "no live
GCP call needed" split the module's own design intends.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import tools.capability_dispatch_client as client_module  # noqa: E402
from tools.capability_dispatch import CapabilityResponse  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch):
    from google.oauth2 import id_token as id_token_module

    monkeypatch.setattr(id_token_module, "fetch_id_token", lambda *a, **k: "fake-oidc-token")


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body
        self.text = text or str(json_body)

    def json(self):
        return self._json_body


def test_a_successful_call_returns_the_unwrapped_result(monkeypatch):
    def fake_post(url, data, headers, timeout):
        assert url == "https://discovery-agent.example.run.app/invoke"
        assert headers["Authorization"] == "Bearer fake-oidc-token"
        return _FakeResponse(200, CapabilityResponse(invocation_id="inv-1", result={"tables": 3}).model_dump())

    monkeypatch.setattr("requests.post", fake_post)

    result = client_module.invoke_remote_capability(
        service_url="https://discovery-agent.example.run.app",
        capability="discovery.catalog.estate",
        args=[],
        kwargs={"estate_id": "wwi-demo-estate"},
    )
    assert result == {"tables": 3}


def test_a_handler_side_failure_raises_remote_capability_error(monkeypatch):
    def fake_post(url, data, headers, timeout):
        return _FakeResponse(
            500,
            {"detail": {"invocation_id": "inv-2", "error_type": "ValueError", "error_message": "bad table name"}},
        )

    monkeypatch.setattr("requests.post", fake_post)

    with pytest.raises(client_module.RemoteCapabilityError) as excinfo:
        client_module.invoke_remote_capability(
            service_url="https://discovery-agent.example.run.app",
            capability="discovery.catalog.estate",
            args=[],
            kwargs={},
        )
    assert excinfo.value.error_type == "ValueError"
    assert "bad table name" in excinfo.value.error_message


def test_a_protocol_level_refusal_raises_unreachable_without_retrying(monkeypatch):
    calls = []

    def fake_post(url, data, headers, timeout):
        calls.append(1)
        return _FakeResponse(403, {}, text="caller not on allowlist")

    monkeypatch.setattr("requests.post", fake_post)

    with pytest.raises(client_module.RemoteCapabilityUnreachable):
        client_module.invoke_remote_capability(
            service_url="https://discovery-agent.example.run.app",
            capability="discovery.catalog.estate",
            args=[],
            kwargs={},
        )
    assert len(calls) == 1  # a 403 is not retried — retrying the same call won't change the outcome


def test_transport_failures_are_retried_up_to_max_retries(monkeypatch):
    import requests as requests_module

    calls = []

    def fake_post(url, data, headers, timeout):
        calls.append(1)
        raise requests_module.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr("requests.post", fake_post)

    with pytest.raises(client_module.RemoteCapabilityUnreachable):
        client_module.invoke_remote_capability(
            service_url="https://discovery-agent.example.run.app",
            capability="discovery.catalog.estate",
            args=[],
            kwargs={},
            max_retries=2,
        )
    assert len(calls) == 3  # initial attempt + 2 retries


def test_a_transport_failure_that_recovers_on_retry_succeeds(monkeypatch):
    import requests as requests_module

    calls = []

    def fake_post(url, data, headers, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise requests_module.exceptions.Timeout("timed out")
        return _FakeResponse(200, CapabilityResponse(invocation_id="inv-3", result={"ok": True}).model_dump())

    monkeypatch.setattr("requests.post", fake_post)

    result = client_module.invoke_remote_capability(
        service_url="https://discovery-agent.example.run.app",
        capability="discovery.catalog.estate",
        args=[],
        kwargs={},
        max_retries=2,
    )
    assert result == {"ok": True}
    assert len(calls) == 2  # same invocation_id both times, dedupe handled server-side
