"""Thin Firestore helper shared by every agent.

Deliberately small: this module owns *connection* concerns only
(client construction, project/database resolution from env). Migration
run lifecycle semantics (state transitions, catalog writes) live in
agents/orchestrator/run_lifecycle.py, per the architectural rule in
master doc §9 — deterministic state logic is not agent code.
"""

from __future__ import annotations

import os
from functools import lru_cache

from google.cloud import firestore


@lru_cache(maxsize=1)
def get_client() -> firestore.Client:
    """Returns a cached Firestore client for the configured project.

    Reads GCP_PROJECT_ID from the environment. On Cloud Run this also
    works with no explicit project (falls back to the runtime's default
    project via Application Default Credentials).
    """
    project_id = os.environ.get("GCP_PROJECT_ID")
    if project_id:
        return firestore.Client(project=project_id)
    return firestore.Client()


def write_document(collection: str, doc_id: str, data: dict) -> str:
    """Writes (merges) a document and returns its path, e.g. for logging."""
    client = get_client()
    ref = client.collection(collection).document(doc_id)
    ref.set(data, merge=True)
    return ref.path


def read_document(collection: str, doc_id: str) -> dict | None:
    client = get_client()
    snap = client.collection(collection).document(doc_id).get()
    return snap.to_dict() if snap.exists else None
