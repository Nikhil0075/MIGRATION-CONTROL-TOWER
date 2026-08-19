"""The Firestore estate registry (Day 11 Phase 2, master doc §32.2).

These run against live Firestore, like the rest of this suite. Every test
that writes uses a unique estate_id and deletes it in teardown — a leaked
estate is worse than a leaked run, because it appears in the console's
estate switcher rather than merely in a list of runs (CLAUDE.md's
test-hygiene rule).

Validation tests need no Firestore and always run.
"""

from __future__ import annotations

import uuid

import pytest

from tools.estate_registry import (
    DEFAULT_ESTATE_ID,
    ORIGIN_WIZARD,
    ORIGIN_YAML,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    EstateConflict,
    EstateNotFound,
    EstateValidationError,
    create_estate,
    delete_estate,
    export_to_yaml,
    get_estate,
    get_source,
    import_from_yaml,
    list_estates,
    set_status,
    update_estate,
    validate_estate,
)

DEMO_YAML = "simulator/source_setup/estate.yaml"


def _estate(estate_id: str, **overrides) -> dict:
    doc = {
        "estate_id": estate_id,
        "display_name": "Test estate",
        "sources": [
            {
                "source_id": "primary",
                "adapter": "sqlserver",
                "config": {"database": "TestDB"},
                "connection_profile": {
                    "host": "localhost",
                    "port": 1433,
                    "user": "sa",
                    "password_secret_ref": "test-password",
                    "password_env": "SQLSERVER_PASSWORD",
                },
            }
        ],
        "target": {"system": "bigquery", "dataset_env": "BQ_DATASET"},
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def temp_estate():
    """Yields a factory; deletes every estate it created, always."""
    created: list[str] = []

    def _make(**overrides) -> dict:
        estate_id = f"test-estate-{uuid.uuid4().hex[:8]}"
        doc = _estate(estate_id, **overrides)
        created.append(estate_id)
        return doc

    yield _make

    for estate_id in created:
        try:
            delete_estate(estate_id)
        except Exception:  # noqa: BLE001 — never mask the test's own failure
            pass


@pytest.fixture
def borrowed_demo_estate():
    """Borrows the shared demo estate: states its precondition, puts it back.

    `wwi-demo-estate` is the one estate this file touches that is not
    disposable — the console's default switcher entry, the packs and every
    caller that omits an estate_id all point at it. So the hygiene rule the
    rest of this file follows for temp estates ("delete what you created")
    becomes "restore exactly what you borrowed", snapshot and all.

    The precondition half matters just as much. `import_from_yaml` refuses
    to overwrite an estate whose origin is `wizard`, deliberately — that is
    what `test_yaml_import_refuses_to_revert_a_console_edit` pins down. A
    test that just calls `import_from_yaml(DEMO_YAML)` therefore inherits
    whatever the last writer left on the shared document: if anything (a
    test, or an operator using the console against this project) leaves it
    console-authored, the import correctly refuses and the test fails for a
    reason that has nothing to do with idempotency. Establishing the state
    out loud means a failure afterwards is this test's own defect.

    `force=True` re-imports the content but deliberately does *not* re-stamp
    `origin` — `update_estate` preserves provenance — so the origin the
    importer keys on is set here explicitly rather than as a side effect.
    """
    from tools.firestore_client import get_client

    doc_ref = get_client().collection("estates").document(DEFAULT_ESTATE_ID)
    snapshot = doc_ref.get().to_dict()
    revisions_before = {r.id for r in doc_ref.collection("revisions").stream()}

    import_from_yaml(DEMO_YAML, actor="test-precondition", force=True)
    doc_ref.update({"origin": ORIGIN_YAML})

    yield

    for revision in doc_ref.collection("revisions").stream():
        if revision.id not in revisions_before:
            revision.reference.delete()
    if snapshot is None:
        doc_ref.delete()
    else:
        doc_ref.set(snapshot)


# ---------------------------------------------------------------------------
# Validation — no Firestore needed
# ---------------------------------------------------------------------------


def test_valid_estate_passes():
    assert validate_estate(_estate("x"))["estate_id"] == "x"


def test_estate_needs_at_least_one_source():
    with pytest.raises(EstateValidationError):
        validate_estate(_estate("x", sources=[]))


def test_duplicate_source_ids_are_rejected():
    """source_id is the table_id prefix and the Wave Manager concurrency
    key; two sources sharing one would silently contend for slots."""
    doc = _estate("x")
    doc["sources"] = doc["sources"] + [dict(doc["sources"][0])]
    with pytest.raises(EstateValidationError, match="duplicate source_id"):
        validate_estate(doc)


def test_unknown_adapter_type_is_rejected_with_the_registered_list():
    doc = _estate("x")
    doc["sources"][0]["adapter"] = "cassandra"
    with pytest.raises(EstateValidationError, match="Registered:"):
        validate_estate(doc)


def test_a_credential_value_in_a_profile_is_rejected():
    """The guarantee that the registry never stores secrets is enforced by
    ConnectionProfile being a closed schema, not by convention."""
    doc = _estate("x")
    doc["sources"][0]["connection_profile"]["password"] = "hunter2"
    with pytest.raises(EstateValidationError):
        validate_estate(doc)


def test_transient_loader_keys_are_stripped():
    cleaned = validate_estate({**_estate("x"), "_source_path": "/tmp/estate.yaml"})
    assert "_source_path" not in cleaned


def test_validation_error_names_the_failing_path():
    doc = _estate("x")
    doc["sources"][0].pop("adapter")
    with pytest.raises(EstateValidationError, match="adapter"):
        validate_estate(doc)


# ---------------------------------------------------------------------------
# CRUD — live Firestore
# ---------------------------------------------------------------------------


@pytest.mark.requires_firestore
def test_create_then_get_roundtrips(temp_estate):
    doc = temp_estate()
    created = create_estate(doc, actor="tester")
    assert created["status"] == STATUS_ACTIVE
    assert created["origin"] == ORIGIN_WIZARD
    assert created["created_by"] == "tester"
    assert get_estate(doc["estate_id"])["display_name"] == "Test estate"


@pytest.mark.requires_firestore
def test_create_refuses_to_clobber_an_existing_estate(temp_estate):
    doc = temp_estate()
    create_estate(doc, actor="tester")
    with pytest.raises(EstateConflict, match="already exists"):
        create_estate(doc, actor="tester")


@pytest.mark.requires_firestore
def test_get_unknown_estate_points_at_the_seed_command():
    with pytest.raises(EstateNotFound, match="seed_estates"):
        get_estate(f"absent-{uuid.uuid4().hex[:8]}")


@pytest.mark.requires_firestore
def test_update_preserves_creation_provenance(temp_estate):
    doc = temp_estate()
    create_estate(doc, actor="creator")
    updated = update_estate(doc["estate_id"], {"display_name": "Renamed"}, actor="editor")
    assert updated["display_name"] == "Renamed"
    assert updated["created_by"] == "creator"
    assert updated["updated_by"] == "editor"


@pytest.mark.requires_firestore
def test_update_records_a_revision(temp_estate):
    from tools.firestore_client import get_client

    doc = temp_estate()
    create_estate(doc, actor="creator")
    update_estate(doc["estate_id"], {"display_name": "Renamed"}, actor="editor")

    revisions = list(
        get_client()
        .collection("estates")
        .document(doc["estate_id"])
        .collection("revisions")
        .stream()
    )
    assert len(revisions) == 1
    assert revisions[0].to_dict()["previous"]["display_name"] == "Test estate"


@pytest.mark.requires_firestore
def test_update_rejects_an_invalid_patch(temp_estate):
    doc = temp_estate()
    create_estate(doc, actor="tester")
    with pytest.raises(EstateValidationError):
        update_estate(doc["estate_id"], {"sources": []}, actor="tester")


@pytest.mark.requires_firestore
def test_set_status_disables_without_deleting(temp_estate):
    doc = temp_estate()
    create_estate(doc, actor="tester")
    set_status(doc["estate_id"], STATUS_DISABLED, actor="tester", reason="decommissioned")
    assert get_estate(doc["estate_id"])["status"] == STATUS_DISABLED


@pytest.mark.requires_firestore
def test_list_filters_by_status(temp_estate):
    doc = temp_estate()
    create_estate(doc, actor="tester")
    set_status(doc["estate_id"], STATUS_DISABLED, actor="tester")
    disabled_ids = {e["estate_id"] for e in list_estates(status=STATUS_DISABLED)}
    active_ids = {e["estate_id"] for e in list_estates(status=STATUS_ACTIVE)}
    assert doc["estate_id"] in disabled_ids
    assert doc["estate_id"] not in active_ids


@pytest.mark.requires_firestore
def test_get_source_names_what_is_declared(temp_estate):
    doc = temp_estate()
    create_estate(doc, actor="tester")
    assert get_source(doc["estate_id"], "primary")["adapter"] == "sqlserver"
    with pytest.raises(EstateNotFound, match="Declared sources"):
        get_source(doc["estate_id"], "nope")


# ---------------------------------------------------------------------------
# YAML import / export — config-as-code alongside in-app authoring
# ---------------------------------------------------------------------------


@pytest.mark.requires_firestore
def test_importing_the_committed_demo_estate_is_idempotent(borrowed_demo_estate):
    first = import_from_yaml(DEMO_YAML, actor="test")
    second = import_from_yaml(DEMO_YAML, actor="test")
    assert first["estate_id"] == DEFAULT_ESTATE_ID
    assert second["estate_id"] == DEFAULT_ESTATE_ID
    assert [s["source_id"] for s in second["sources"]] == [
        "wwi-sqlserver", "oracle-corpus", "dag-artifacts",
    ]


@pytest.mark.requires_firestore
def test_yaml_import_refuses_to_revert_a_console_edit(tmp_path, temp_estate):
    """The property that lets config-as-code and the wizard coexist:
    re-running `make setup` must not silently undo an operator's edit."""
    import yaml

    doc = temp_estate()
    create_estate(doc, actor="operator")  # origin=wizard
    update_estate(doc["estate_id"], {"display_name": "Edited in console"}, actor="operator")

    path = tmp_path / "estate.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    with pytest.raises(EstateConflict, match="authored in the console"):
        import_from_yaml(path, actor="seed")

    assert get_estate(doc["estate_id"])["display_name"] == "Edited in console"


@pytest.mark.requires_firestore
def test_forced_yaml_import_replaces_a_console_edit(tmp_path, temp_estate):
    import yaml

    doc = temp_estate()
    create_estate(doc, actor="operator")
    update_estate(doc["estate_id"], {"display_name": "Edited in console"}, actor="operator")

    path = tmp_path / "estate.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    import_from_yaml(path, actor="seed", force=True)
    assert get_estate(doc["estate_id"])["display_name"] == "Test estate"


@pytest.mark.requires_firestore
def test_yaml_import_of_a_new_estate_records_its_origin(tmp_path, temp_estate):
    import yaml

    doc = temp_estate()
    path = tmp_path / "estate.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    created = import_from_yaml(path, actor="seed")
    assert created["origin"] == ORIGIN_YAML


@pytest.mark.requires_firestore
def test_export_roundtrips_back_through_validation(temp_estate):
    """Edit in the app, export, commit — the round-trip must produce YAML
    the importer accepts, or the console is a one-way door."""
    import yaml

    doc = temp_estate()
    create_estate(doc, actor="tester")
    exported = yaml.safe_load(export_to_yaml(doc["estate_id"]))

    assert exported["estate_id"] == doc["estate_id"]
    assert "created_at" not in exported and "origin" not in exported
    validate_estate(exported)


@pytest.mark.requires_firestore
def test_import_rejects_yaml_without_an_estate_id(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("display_name: nameless\n", encoding="utf-8")
    with pytest.raises(EstateValidationError, match="declares no estate_id"):
        import_from_yaml(path, actor="test")


# ---------------------------------------------------------------------------
# Integration with connection_context and run documents
# ---------------------------------------------------------------------------


@pytest.mark.requires_firestore
def test_registry_estate_is_visible_to_connection_context(temp_estate):
    """binding_for() must see console-created estates, not just YAML —
    otherwise onboarding through the app produces an estate nothing can
    actually connect to."""
    from tools.connection_context import binding_for

    doc = temp_estate()
    create_estate(doc, actor="tester")

    binding = binding_for(doc["estate_id"], "primary")
    assert binding.adapter == "sqlserver"
    assert binding.config == {"database": "TestDB"}
    assert binding.health_key == f"{doc['estate_id']}__primary"


@pytest.mark.requires_firestore
def test_run_documents_record_their_estate(firestore_cleanup):
    from agents.orchestrator.run_lifecycle import create_run, get_run

    run_id = firestore_cleanup.run(
        create_run("test.estate.scoping", estate_id="acme-legacy",
                   source_id="acme-sqlserver", pack_id="acme_v1")
    )
    run = get_run(run_id)
    assert run["estate_id"] == "acme-legacy"
    assert run["source_id"] == "acme-sqlserver"
    assert run["pack_id"] == "acme_v1"


@pytest.mark.requires_firestore
def test_run_without_an_estate_defaults_to_the_demo_estate(firestore_cleanup):
    """The ~15 existing callers pass no estate_id and must keep working."""
    from agents.orchestrator.run_lifecycle import create_run, get_run

    run_id = firestore_cleanup.run(create_run("test.default.estate"))
    assert get_run(run_id)["estate_id"] == DEFAULT_ESTATE_ID
