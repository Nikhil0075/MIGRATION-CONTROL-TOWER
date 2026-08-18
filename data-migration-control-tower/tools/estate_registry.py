"""The estate registry (Day 11 Phase 2, master doc §32.2).

An estate is the unit of onboarding, isolation and authorization. Before
this module there was exactly one, declared in a single YAML file at a
path hardcoded into frontend/api_v1.py, and `GET /api/v1/estates`
returned a hardcoded list of one. Onboarding a second estate meant
editing the repository.

Firestore layout:
    estates/{estate_id}                  the estate document
    estates/{estate_id}/revisions/{ts}   the document as it was before each write

Revision history is a subcollection rather than a separate audit store
because the question it answers — "who changed this estate's connection
binding, and to what?" — is only ever asked about one estate at a time.

**Layering note (deliberate deviation from the phase plan).** The plan
said estate writes should go through frontend/operations.py's audit path.
They do not: `tools/` is the deterministic, framework-free layer that
`frontend/` builds on (CLAUDE.md), and importing frontend from tools
would invert that. Durable estate state and its revision history belong
here; the operator-request bookkeeping (operation_requests /
operation_audit, Idempotency-Key, justification) stays in the API layer
and wraps these calls in Phase 6. Both records get written for a
console-initiated change — they answer different questions.

Credentials never reach this module. Estate documents carry connection
*references* only, enforced by ConnectionProfile being a closed schema
(contracts/metadata_model.json, asserted in tests/test_contracts.py).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path

import jsonschema
import yaml

from tools.firestore_client import get_client

logger = logging.getLogger("estate_registry")

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "metadata_model.json"

COLLECTION = "estates"
REVISIONS = "revisions"

DEFAULT_ESTATE_ID = "wwi-demo-estate"
DEMO_ESTATE_YAML = REPO_ROOT / "simulator" / "source_setup" / "estate.yaml"

STATUS_ACTIVE = "ACTIVE"
STATUS_DISABLED = "DISABLED"

ORIGIN_YAML = "yaml_import"
ORIGIN_WIZARD = "wizard"

SCHEMA_VERSION = 1

#: Keys the loader adds for its own bookkeeping; never persisted.
_TRANSIENT_KEYS = {"_source_path"}


class EstateValidationError(ValueError):
    """The document does not satisfy contracts/metadata_model.json."""


class EstateNotFound(KeyError):
    pass


class EstateConflict(RuntimeError):
    """The write would overwrite something the caller did not intend to."""


def _definitions() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["definitions"]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_estate(doc: dict) -> dict:
    """Validates an estate and every nested source/profile.

    Nested entities are validated explicitly rather than through a $ref:
    every schema consumer in this project extracts one definition node and
    validates it standalone with no RefResolver, so an internal $ref would
    not resolve (see tests/test_contracts.py, which enforces this).
    """
    definitions = _definitions()
    cleaned = {k: v for k, v in doc.items() if k not in _TRANSIENT_KEYS}

    try:
        jsonschema.validate(instance=cleaned, schema=definitions["Estate"])
        for source in cleaned.get("sources", []):
            jsonschema.validate(instance=source, schema=definitions["EstateSource"])
            profile = source.get("connection_profile")
            if profile is not None:
                jsonschema.validate(instance=profile, schema=definitions["ConnectionProfile"])
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "(root)"
        raise EstateValidationError(f"{path}: {exc.message}") from exc

    source_ids = [s["source_id"] for s in cleaned.get("sources", [])]
    duplicates = sorted({s for s in source_ids if source_ids.count(s) > 1})
    if duplicates:
        raise EstateValidationError(
            f"duplicate source_id(s) {duplicates}: source_id is the table_id prefix and "
            f"the Wave Manager concurrency key, so it must be unique within an estate."
        )

    from tools.adapters import ADAPTER_TYPES

    unknown = sorted(
        {s["adapter"] for s in cleaned.get("sources", []) if s["adapter"] not in ADAPTER_TYPES}
    )
    if unknown:
        raise EstateValidationError(
            f"unknown adapter type(s) {unknown}. Registered: {sorted(ADAPTER_TYPES)}. "
            f"Add an implementation to tools/adapters/ and register it before "
            f"declaring an estate source that uses it."
        )
    return cleaned


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_estate(estate_id: str) -> dict:
    snapshot = get_client().collection(COLLECTION).document(estate_id).get()
    if not snapshot.exists:
        raise EstateNotFound(
            f"No estate {estate_id!r} in the registry. Seed the committed estates with "
            f"`python infrastructure/seed_estates.py`, or create one through the console."
        )
    return snapshot.to_dict() or {}


def list_estates(*, status: str | None = None) -> list[dict]:
    """Filtered in Python, not with .where().

    A `.where()` here is legal on its own, but the moment it is combined
    with an order_by it needs a composite index this project does not
    create — the documented Firestore gotcha in CLAUDE.md. Estate counts
    are small (one per onboarded customer), so this costs nothing.
    """
    docs = [d.to_dict() or {} for d in get_client().collection(COLLECTION).stream()]
    if status is not None:
        docs = [d for d in docs if d.get("status", STATUS_ACTIVE) == status]
    return sorted(docs, key=lambda d: d.get("estate_id", ""))


def estate_exists(estate_id: str) -> bool:
    return get_client().collection(COLLECTION).document(estate_id).get().exists


def get_source(estate_id: str, source_id: str) -> dict:
    estate = get_estate(estate_id)
    for source in estate.get("sources", []):
        if source.get("source_id") == source_id:
            return source
    raise EstateNotFound(
        f"No source {source_id!r} in estate {estate_id!r}. Declared sources: "
        f"{[s.get('source_id') for s in estate.get('sources', [])]}."
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _record_revision(estate_id: str, previous: dict, *, actor: str, reason: str) -> None:
    if not previous:
        return
    get_client().collection(COLLECTION).document(estate_id).collection(REVISIONS).document(
        _now()
    ).set({"previous": previous, "replaced_by": actor, "reason": reason, "recorded_at": _now()})


def create_estate(doc: dict, *, actor: str, origin: str = ORIGIN_WIZARD) -> dict:
    cleaned = validate_estate(doc)
    estate_id = cleaned["estate_id"]
    if estate_exists(estate_id):
        raise EstateConflict(
            f"Estate {estate_id!r} already exists. Use update_estate() to change it, or "
            f"import_from_yaml(force=True) to replace it deliberately."
        )
    now = _now()
    record = {
        **cleaned,
        "status": cleaned.get("status", STATUS_ACTIVE),
        "origin": origin,
        "schema_version": SCHEMA_VERSION,
        "created_at": now,
        "created_by": actor,
        "updated_at": now,
        "updated_by": actor,
    }
    get_client().collection(COLLECTION).document(estate_id).set(record)
    logger.info("created estate %s (origin=%s, by=%s)", estate_id, origin, actor)
    return record


def update_estate(estate_id: str, patch: dict, *, actor: str, reason: str = "") -> dict:
    previous = get_estate(estate_id)
    merged = {**previous, **patch, "estate_id": estate_id}
    cleaned = validate_estate(merged)
    record = {
        **cleaned,
        "origin": previous.get("origin", ORIGIN_WIZARD),
        "schema_version": SCHEMA_VERSION,
        "created_at": previous.get("created_at"),
        "created_by": previous.get("created_by"),
        "updated_at": _now(),
        "updated_by": actor,
    }
    _record_revision(estate_id, previous, actor=actor, reason=reason or "update_estate")
    get_client().collection(COLLECTION).document(estate_id).set(record)
    return record


def set_status(estate_id: str, status: str, *, actor: str, reason: str = "") -> dict:
    if status not in (STATUS_ACTIVE, STATUS_DISABLED):
        raise EstateValidationError(f"status must be ACTIVE or DISABLED, got {status!r}")
    return update_estate(estate_id, {"status": status}, actor=actor, reason=reason or f"set_status:{status}")


def delete_estate(estate_id: str) -> None:
    """Hard-delete, for test/dev hygiene only — the same reasoning as
    tools/registry.py::delete_card() and run_lifecycle.delete_run().

    Operational removal is set_status(DISABLED): run history references
    estate_id, and deleting the estate a completed run points at makes
    that history uninterpretable. A leaked test estate is worse than a
    leaked test run — it shows up in the console's estate switcher.
    """
    client = get_client()
    doc_ref = client.collection(COLLECTION).document(estate_id)
    for revision in doc_ref.collection(REVISIONS).stream():
        revision.reference.delete()
    doc_ref.delete()


# ---------------------------------------------------------------------------
# YAML import / export — config-as-code alongside in-app authoring
# ---------------------------------------------------------------------------


def import_from_yaml(path: str | Path, *, actor: str = "seed", force: bool = False) -> dict:
    """Idempotent upsert of a committed estate YAML into the registry.

    Refuses to overwrite an estate whose `origin` is 'wizard' unless
    forced. That refusal is the entire reason config-as-code and in-app
    authoring can coexist: without it, re-running `make setup` after an
    operator edited an estate in the console would silently revert their
    change, and the revert would look like data loss with no cause.
    """
    path = Path(path)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or "estate_id" not in doc:
        raise EstateValidationError(f"{path} declares no estate_id.")

    cleaned = validate_estate(doc)
    estate_id = cleaned["estate_id"]

    try:
        previous = get_estate(estate_id)
    except EstateNotFound:
        return create_estate(cleaned, actor=actor, origin=ORIGIN_YAML)

    if previous.get("origin") == ORIGIN_WIZARD and not force:
        raise EstateConflict(
            f"Estate {estate_id!r} was authored in the console (origin=wizard); refusing to "
            f"overwrite it from {path}. Re-run with force=True to replace it deliberately, "
            f"or export the console version with export_to_yaml() first."
        )

    record = update_estate(
        estate_id, cleaned, actor=actor, reason=f"yaml_import:{path.name}"
    )
    logger.info("re-imported estate %s from %s", estate_id, path)
    return record


def export_to_yaml(estate_id: str) -> str:
    """Renders a registry estate back to committable YAML.

    The round-trip is what makes the console safe to use on an estate that
    started as config-as-code: edit in the app, export, commit.
    """
    estate = get_estate(estate_id)
    exportable = {
        k: v
        for k, v in estate.items()
        if k not in _TRANSIENT_KEYS
        and k not in {"created_at", "created_by", "updated_at", "updated_by", "origin", "schema_version"}
    }
    ordered = {}
    for key in ("estate_id", "display_name", "status", "owner", "sources", "target"):
        if key in exportable:
            ordered[key] = exportable.pop(key)
    ordered.update(exportable)
    return yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True)


def seed_committed_estates(*, actor: str = "seed", force: bool = False) -> list[dict]:
    """Imports every committed estate YAML. Used by infrastructure/seed_estates.py."""
    from tools.connection_context import ESTATE_SEARCH_PATHS

    imported = []
    for entry in ESTATE_SEARCH_PATHS:
        paths = sorted(entry.glob("*.yaml")) if entry.is_dir() else ([entry] if entry.is_file() else [])
        for path in paths:
            try:
                imported.append(import_from_yaml(path, actor=actor, force=force))
            except EstateConflict as exc:
                logger.warning("skipped %s: %s", path, exc)
    return imported
