"""The Migration Pack registry (Day 11 Phase 3, master doc §32.4).

Pure functions over committed YAML — no live services needed.

The tests that matter most are the ones covering the three fields that
were declared in every pack.yaml and read by nothing before this phase:
classification_rules, dialect_notes and default_mode. `default_mode` in
particular *claimed* to stop the Oracle pack from executing, while what
actually stopped it was a hardcoded source_id comparison in the API.
"""

from __future__ import annotations

import pytest
import yaml

from tools.pack_loader import (
    MODE_ASSESSMENT,
    MODE_EXECUTION,
    PackNotFound,
    PackValidationError,
    classification_rules,
    default_mode,
    dialect_note,
    get_pack,
    list_packs,
    load_pack,
    scheduled_tables,
    supports_execution,
    type_rules,
    validate_pack,
)

WWI = "wwi_sqlserver_v1"
ORACLE = "oracle_corpus_v1"
POSTGRES_ASSESSMENT = "postgres_retail_v1"
POSTGRES_EXEC = "postgres_retail_exec_v1"


def _minimal_pack(**overrides) -> dict:
    pack = {
        "pack_id": "p", "version": "1.0.0",
        "estate_file": "simulator/source_setup/estate.yaml",
        "source_id": "wwi-sqlserver", "default_mode": "assessment",
    }
    pack.update(overrides)
    return pack


# ---------------------------------------------------------------------------
# Discovery and validation
# ---------------------------------------------------------------------------


def test_every_committed_pack_loads_and_validates():
    """Subset, not exact set: adding a Migration Pack is configuration, and
    should not require editing a test. list_packs() validates each one, so
    this failing means a committed pack is malformed."""
    packs = list_packs()
    assert {WWI, ORACLE} <= {p["pack_id"] for p in packs}
    assert all(p.get("version") for p in packs)


def test_get_pack_by_id():
    assert get_pack(WWI)["pack_id"] == WWI


def test_unknown_pack_lists_the_known_ones():
    with pytest.raises(PackNotFound, match="Known packs"):
        get_pack("no_such_pack")


def test_missing_pack_file_is_reported_clearly(tmp_path):
    with pytest.raises(PackNotFound, match="No pack.yaml"):
        load_pack(tmp_path / "absent")


def test_a_pack_missing_a_required_field_is_rejected():
    """Previously a typo surfaced as a KeyError several frames later,
    usually inside an agent."""
    bad = _minimal_pack()
    del bad["source_id"]
    with pytest.raises(PackValidationError, match="source_id"):
        validate_pack(bad)


def test_an_invalid_default_mode_is_rejected():
    with pytest.raises(PackValidationError):
        validate_pack(_minimal_pack(default_mode="whenever"))


def test_a_scheduled_table_missing_its_target_is_rejected(tmp_path):
    bad = _minimal_pack(scheduled_tables=[{"source_schema": "S", "source_table": "T"}])
    with pytest.raises(PackValidationError, match="target_table"):
        validate_pack(bad)


def test_load_pack_reports_which_file_failed(tmp_path):
    path = tmp_path / "pack.yaml"
    path.write_text(yaml.safe_dump(_minimal_pack(default_mode="nonsense")), encoding="utf-8")
    with pytest.raises(PackValidationError, match=str(path.name)):
        load_pack(path)


# ---------------------------------------------------------------------------
# default_mode — now load-bearing
# ---------------------------------------------------------------------------


def test_default_mode_is_read_from_the_pack():
    assert default_mode(get_pack(WWI)) == MODE_EXECUTION
    assert default_mode(get_pack(ORACLE)) == MODE_ASSESSMENT


def test_execution_support_no_longer_depends_on_a_hardcoded_source_id():
    """Replaces frontend/api_v1.py's `source_id == "wwi-sqlserver"`."""
    assert supports_execution(get_pack(WWI)) is True
    assert supports_execution(get_pack(ORACLE)) is False


def test_assessment_only_pack_is_not_executable_even_with_a_capable_adapter():
    assert supports_execution(_minimal_pack(default_mode=MODE_ASSESSMENT)) is False


# ---------------------------------------------------------------------------
# postgres_retail_exec_v1 — the new immutable execution-capable pack
# (Deploy & Harden Phase 3a, docs/adr/0003)
# ---------------------------------------------------------------------------


def test_postgres_exec_pack_loads_and_validates():
    packs = list_packs()
    assert POSTGRES_EXEC in {p["pack_id"] for p in packs}


def test_postgres_exec_pack_is_execution_capable():
    """PostgresAdapter already declares transfer+reconcile — this pack's
    default_mode=execution is what actually turns that on
    (tools/pack_loader.py::supports_execution())."""
    assert supports_execution(get_pack(POSTGRES_EXEC)) is True


def test_original_postgres_assessment_pack_is_unaffected():
    """The whole point of a NEW pack rather than an in-place edit — this
    must still read exactly as it did before postgres_retail_exec_v1
    existed."""
    assert supports_execution(get_pack(POSTGRES_ASSESSMENT)) is False
    assert default_mode(get_pack(POSTGRES_ASSESSMENT)) == MODE_ASSESSMENT


def test_postgres_exec_pack_targets_the_same_estate_and_source_as_the_assessment_pack():
    """An execution-capable pack does not get to invent a new connection
    story — it inherits the one already onboarded."""
    exec_pack = get_pack(POSTGRES_EXEC)
    assessment_pack = get_pack(POSTGRES_ASSESSMENT)
    assert exec_pack["estate_file"] == assessment_pack["estate_file"]
    assert exec_pack["source_id"] == assessment_pack["source_id"]


def test_postgres_exec_pack_derives_targets_from_the_catalog_not_hardcoded():
    assert scheduled_tables(get_pack(POSTGRES_EXEC)) == []


def test_postgres_exec_pack_declares_its_upgrade_provenance():
    pack = get_pack(POSTGRES_EXEC)
    assert pack.get("derived_from") == POSTGRES_ASSESSMENT
    assert pack.get("upgrade_rationale")


# ---------------------------------------------------------------------------
# dialect_notes — now load-bearing
# ---------------------------------------------------------------------------


def test_dialect_note_is_returned_for_a_pack_that_declares_one():
    note = dialect_note(get_pack(ORACLE))
    assert note and "NVL" in note


def test_dialect_note_is_none_for_a_pack_that_declares_none():
    """The WWI pack is a SQL Server source needing no translation; it must
    not inherit the Oracle sentence that used to be hardcoded."""
    assert dialect_note(get_pack(WWI)) is None


def test_blank_dialect_notes_are_treated_as_absent():
    assert dialect_note(_minimal_pack(dialect_notes="   ")) is None


# ---------------------------------------------------------------------------
# classification_rules — now load-bearing
# ---------------------------------------------------------------------------


def test_classification_rules_resolve_to_real_content():
    rules = classification_rules(get_pack(WWI))
    assert rules is not None and "rules" in rules


def test_classification_rules_are_none_when_undeclared():
    assert classification_rules(_minimal_pack()) is None


# ---------------------------------------------------------------------------
# type_rules — moved out of migration_executor's module constants
# ---------------------------------------------------------------------------


def test_wwi_type_rules_match_the_executor_constants_they_replaced():
    rules = type_rules(get_pack(WWI))
    assert rules["stringify_via_method"] == {"geography", "geometry", "hierarchyid"}
    assert rules["excluded"] == {"xml", "varbinary", "image"}
    assert {"int", "nvarchar", "decimal", "datetime2"} <= rules["scalar_safe"]


def test_type_rules_fall_back_to_defaults_for_a_pack_without_them():
    rules = type_rules(_minimal_pack())
    assert "geography" in rules["stringify_via_method"]
    assert "int" in rules["numeric"]


def test_numeric_rules_exist_for_aggregate_column_derivation():
    assert "decimal" in type_rules(get_pack(WWI))["numeric"]


# ---------------------------------------------------------------------------
# scheduled_tables
# ---------------------------------------------------------------------------


def test_wwi_pack_declares_the_previously_hardcoded_table():
    declared = scheduled_tables(get_pack(WWI))
    assert len(declared) == 1
    assert declared[0] == {
        "source_schema": "Sales",
        "source_table": "Customers",
        "target_table": "customers_dim",
        "key_column": "CustomerID",
        "numeric_column": "CreditLimit",
        "null_check_column": "PhoneNumber",
    }


def test_a_pack_without_scheduled_tables_derives_from_the_catalog():
    assert scheduled_tables(get_pack(ORACLE)) == []


# ---------------------------------------------------------------------------
# Binding a pack's adapter to the estate actually being assessed
# ---------------------------------------------------------------------------


def _pack_with(source_id: str = "retail-postgres") -> dict:
    return {"pack_id": "test_pack", "source_id": source_id}


def _estate(*sources: dict) -> dict:
    return {"estate_id": "test-estate", "sources": list(sources)}


def _patch(monkeypatch, estate: dict, pack_adapter: str = "postgres"):
    from tools import pack_loader

    monkeypatch.setattr(pack_loader, "estate_for_pack", lambda pack: _estate(
        {"source_id": pack["source_id"], "adapter": pack_adapter}
    ))
    monkeypatch.setattr(
        "tools.connection_context.load_estate_document", lambda estate_id: estate
    )
    built = {}
    monkeypatch.setattr(
        "tools.adapters.build_adapter_for_binding",
        lambda binding: built.setdefault("binding", binding),
    )
    return built


def test_no_estate_id_keeps_the_pack_s_own_adapter(monkeypatch):
    """The CLI demo path must not change: there the pack and the estate are
    the same thing, and there is nothing to rebind."""
    from tools import pack_loader

    sentinel = object()
    monkeypatch.setattr(pack_loader, "build_adapter_for_pack", lambda pack: sentinel)
    assert pack_loader.build_adapter_for_estate(_pack_with(), None) is sentinel


def test_the_adapter_binds_to_the_named_estate_not_the_pack_s(monkeypatch):
    """Without this the adapter carries no binding at all: Postgres raises,
    and SQL Server quietly falls back to process env vars — connecting to
    whatever the machine points at rather than the estate requested."""
    from tools import pack_loader

    estate = _estate({"source_id": "finance-postgres", "adapter": "postgres"})
    built = _patch(monkeypatch, estate)
    pack_loader.build_adapter_for_estate(_pack_with(), "test-estate")
    assert built["binding"].source_id == "finance-postgres"
    assert built["binding"].estate_id == "test-estate"


def test_a_matching_source_id_wins_over_the_adapter_family(monkeypatch):
    from tools import pack_loader

    estate = _estate(
        {"source_id": "other-postgres", "adapter": "postgres"},
        {"source_id": "retail-postgres", "adapter": "postgres"},
    )
    built = _patch(monkeypatch, estate)
    pack_loader.build_adapter_for_estate(_pack_with(), "test-estate")
    assert built["binding"].source_id == "retail-postgres"


def test_an_estate_without_a_matching_source_says_so(monkeypatch):
    from tools import pack_loader

    estate = _estate({"source_id": "wwi-sqlserver", "adapter": "sqlserver"})
    _patch(monkeypatch, estate)
    with pytest.raises(RuntimeError, match="has no 'postgres' source"):
        pack_loader.build_adapter_for_estate(_pack_with(), "test-estate")


def test_an_ambiguous_estate_refuses_to_guess(monkeypatch):
    """Assessing one of two sources and labelling the result with the estate
    reports coverage that was never measured."""
    from tools import pack_loader

    estate = _estate(
        {"source_id": "a-postgres", "adapter": "postgres"},
        {"source_id": "b-postgres", "adapter": "postgres"},
    )
    _patch(monkeypatch, estate)
    with pytest.raises(RuntimeError, match="does not say"):
        pack_loader.build_adapter_for_estate(_pack_with(), "test-estate")
