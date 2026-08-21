"""Tests for tools/idempotency.py (Deploy & Harden Phase 2c) — the
generic claim/complete pattern factored out of orchestrator.py's
_dedup_claim/_dedup_complete for use outside Pub/Sub message handling
(specifically, HTTP capability-invocation dedup in
tools/capability_http_server.py).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools import idempotency  # noqa: E402


def _firestore_reachable() -> bool:
    from tests.probes import firestore_reachable

    return firestore_reachable()


skip_if_no_firestore = pytest.mark.skipif(not _firestore_reachable(), reason="Firestore not reachable")

COLLECTION = "test_idempotency_claims"


@pytest.fixture()
def cleanup_key():
    from tools.firestore_client import get_client

    keys: list[str] = []
    yield keys
    for key in keys:
        get_client().collection(COLLECTION).document(key).delete()


@skip_if_no_firestore
def test_a_fresh_key_is_claimed(cleanup_key):
    key = f"key-{uuid.uuid4().hex[:8]}"
    cleanup_key.append(key)
    status, cached = idempotency.claim(COLLECTION, key, actor="test-actor")
    assert status == "claimed"
    assert cached is None


@skip_if_no_firestore
def test_a_completed_key_returns_the_cached_result_and_never_reclaims(cleanup_key):
    key = f"key-{uuid.uuid4().hex[:8]}"
    cleanup_key.append(key)
    idempotency.claim(COLLECTION, key, actor="test-actor")
    idempotency.complete(COLLECTION, key, actor="test-actor", result={"value": 42})

    status, cached = idempotency.claim(COLLECTION, key, actor="test-actor")
    assert status == "done"
    assert cached == {"value": 42, "deduped": True}


@skip_if_no_firestore
def test_a_claimed_but_never_completed_key_is_a_stale_claim(cleanup_key):
    key = f"key-{uuid.uuid4().hex[:8]}"
    cleanup_key.append(key)
    idempotency.claim(COLLECTION, key, actor="test-actor")  # claimed, never completed

    status, cached = idempotency.claim(COLLECTION, key, actor="test-actor")
    assert status == "stale_claim"
    assert cached is None


@skip_if_no_firestore
def test_different_keys_do_not_interfere(cleanup_key):
    key_a, key_b = f"key-{uuid.uuid4().hex[:8]}", f"key-{uuid.uuid4().hex[:8]}"
    cleanup_key.extend([key_a, key_b])
    idempotency.claim(COLLECTION, key_a, actor="test-actor")
    idempotency.complete(COLLECTION, key_a, actor="test-actor", result={"value": 1})

    status, _ = idempotency.claim(COLLECTION, key_b, actor="test-actor")
    assert status == "claimed"  # key_a's completion doesn't leak into key_b
