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
