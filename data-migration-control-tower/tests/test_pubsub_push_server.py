"""Tests for tools/pubsub_push_server.py (Deploy & Harden Phase 2b) —
the Cloud Run push-delivery wrapper for tools/worker_supervisor.py's
event consumers. No live Pub/Sub needed: a push delivery is just an
HTTP POST with a specific JSON envelope, which these tests construct by
hand — the same skipping-real-GCP approach
tests/test_capability_http_server.py uses for OIDC.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.capability_http_server import verify_caller_identity  # noqa: E402
from tools.pubsub_push_server import build_push_app  # noqa: E402


def _push_envelope(payload: dict, message_id: str = "msg-1") -> dict:
    data = base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")
    return {"message": {"data": data, "messageId": message_id}, "subscription": "projects/p/subscriptions/s"}


def _client(handler) -> TestClient:
    app = build_push_app(consumer_name="test-consumer", handler=handler)
    app.dependency_overrides[verify_caller_identity] = lambda: "pubsub-invoker@example.internal"
    return TestClient(app)


def test_a_successful_delivery_returns_200_which_is_the_ack():
    received = []

    def handler(payload):
        received.append(payload)
        return {"processed": True}

    client = _client(handler)
    response = client.post("/", json=_push_envelope({"run_id": "run-1"}, message_id="msg-42"))

    assert response.status_code == 200
    assert received == [{"run_id": "run-1", "_pubsub_message_id": "msg-42"}]


def test_the_pubsub_message_id_is_attached_the_same_way_pull_does():
    """This is what makes _dedup_claim() work unchanged regardless of
    whether a message arrived via pull() or push delivery."""
    seen = {}

    def handler(payload):
        seen["message_id"] = payload.get("_pubsub_message_id")

    client = _client(handler)
    client.post("/", json=_push_envelope({}, message_id="msg-abc-123"))
    assert seen["message_id"] == "msg-abc-123"


def test_a_handler_exception_returns_a_non_2xx_which_is_the_nack():
    def handler(payload):
        raise RuntimeError("wave capacity HOLD")

    client = _client(handler)
    response = client.post("/", json=_push_envelope({"run_id": "run-1"}))

    assert response.status_code == 500
    assert "wave capacity HOLD" in response.json()["detail"]


def test_a_push_body_with_no_message_data_is_a_400_not_a_crash():
    client = _client(lambda payload: None)
    response = client.post("/", json={"message": {}, "subscription": "s"})
    assert response.status_code == 400


def test_malformed_base64_is_a_400():
    client = _client(lambda payload: None)
    response = client.post(
        "/", json={"message": {"data": "not-valid-base64!!!", "messageId": "m1"}, "subscription": "s"}
    )
    assert response.status_code == 400


def test_caller_verification_is_enforced_when_not_overridden():
    app = build_push_app(consumer_name="locked-consumer", handler=lambda payload: None)
    client = TestClient(app)
    response = client.post("/", json=_push_envelope({}))
    assert response.status_code == 401


def test_status_endpoint_names_the_consumer():
    client = _client(lambda payload: None)
    response = client.get("/status")
    assert response.json() == {"consumer": "test-consumer"}
