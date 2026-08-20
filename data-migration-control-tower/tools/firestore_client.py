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


#: Firestore's own name for the unnamed database every project starts
#: with. Passing it explicitly and passing nothing are equivalent.
DEFAULT_DATABASE = "(default)"


def database_id() -> str:
    """Which Firestore database this process talks to.

    Exists so that "which database" is a runtime question with one
    answer, rather than an assumption baked into every caller. The test
    suite sets FIRESTORE_DATABASE to keep itself out of the production
    data — before this, `pytest` wrote to the same database the console
    reads, which is how 9,891 orphaned documents and hundreds of
    `test_run_*` records ended up in a live project.
    """
    return os.environ.get("FIRESTORE_DATABASE") or DEFAULT_DATABASE


@lru_cache(maxsize=4)
def _client(project_id: str | None, database: str) -> firestore.Client:
    """Cached per (project, database).

    Keyed rather than a single cached client because the database is read
    from the environment: a process that changes it and gets the old
    client back would be writing to a database it believes it left.
    """
    kwargs: dict = {}
    if project_id:
        kwargs["project"] = project_id
    if database != DEFAULT_DATABASE:
        kwargs["database"] = database
    return firestore.Client(**kwargs)


def get_client() -> firestore.Client:
    """Returns a cached Firestore client for the configured project.

    Reads GCP_PROJECT_ID from the environment. On Cloud Run this also
    works with no explicit project (falls back to the runtime's default
    project via Application Default Credentials). FIRESTORE_DATABASE
    selects a non-default database; unset means `(default)`, which is
    what production uses.
    """
    return _client(os.environ.get("GCP_PROJECT_ID"), database_id())


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
