"""Guards on contracts/metadata_model.json itself (Day 11 Phase 0).

The schema file is the model of record for every entity in this project,
and two of its properties are load-bearing in ways that are easy to break
silently by editing JSON:

  1. **No definition may contain an internal $ref.** Five modules load a
     single definition node and validate against it standalone with no
     RefResolver (tools/source_catalog.py::_load_schema, plus the same
     pattern in lineage_graph, registry, reconciliation). An internal
     $ref parses fine, passes review, and then fails at runtime with an
     unresolvable-reference error the first time that entity is
     validated. MigrationPlan's own description already documents the
     convention; this test enforces it.

  2. **ConnectionProfile may never gain a credential-value field.** The
     whole point of the onboarding wizard collecting *references* is that
     no secret ever reaches Firestore, the API layer, or a log line. That
     guarantee is one careless property away from being false.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from tests.conftest import REPO_ROOT

SCHEMA_PATH = REPO_ROOT / "contracts" / "metadata_model.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
DEFINITIONS = SCHEMA["definitions"]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


@pytest.mark.parametrize("name", sorted(DEFINITIONS))
def test_definition_has_no_internal_ref(name):
    """Every consumer validates one definition in isolation — see module docstring."""
    refs = [n["$ref"] for n in _walk(DEFINITIONS[name]) if "$ref" in n]
    assert refs == [], (
        f"{name} contains $ref {refs}, which will not resolve: consumers extract "
        f"a single definition node and validate it standalone with no RefResolver. "
        f"Inline the shape (as MigrationPlan.steps does) or validate the nested "
        f"entity separately against its own definition."
    )


@pytest.mark.parametrize("name", sorted(DEFINITIONS))
def test_definition_is_a_valid_schema(name):
    jsonschema.Draft7Validator.check_schema(DEFINITIONS[name])


# ---------------------------------------------------------------------------
# ConnectionProfile: references only, never values
# ---------------------------------------------------------------------------

_CREDENTIAL_VALUE_NAMES = {
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "credential", "credentials", "private_key", "connection_string", "dsn",
}


def test_connection_profile_is_closed():
    """additionalProperties: false is what stops a credential field being
    added by a caller rather than by a schema edit."""
    assert DEFINITIONS["ConnectionProfile"]["additionalProperties"] is False


def test_connection_profile_declares_no_credential_value_field():
    properties = DEFINITIONS["ConnectionProfile"]["properties"]
    offenders = sorted(
        name
        for name in properties
        if name.lower() in _CREDENTIAL_VALUE_NAMES
    )
    assert offenders == [], (
        f"ConnectionProfile declares credential-value field(s) {offenders}. This "
        f"definition may only carry REFERENCES (password_secret_ref, password_env). "
        f"A value here would be persisted to Firestore and returned by /api/v1/estates."
    )


def test_connection_profile_reference_fields_are_named_as_references():
    """password_secret_ref / password_env hold a secret NAME, not a secret.
    Their names encode that; a bare 'password' would not."""
    properties = DEFINITIONS["ConnectionProfile"]["properties"]
    assert "password_secret_ref" in properties
    assert "password_env" in properties


def test_a_credential_bearing_profile_is_rejected():
    profile = {"host": "localhost", "port": 1433, "password": "hunter2"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=profile, schema=DEFINITIONS["ConnectionProfile"])


def test_a_reference_only_profile_validates():
    profile = {
        "host_env": "SQLSERVER_HOST",
        "port_env": "SQLSERVER_PORT",
        "user_env": "SQLSERVER_USER",
        "password_secret_ref": "sqlserver-wwi-password",
        "password_env": "SQLSERVER_PASSWORD",
    }
    jsonschema.validate(instance=profile, schema=DEFINITIONS["ConnectionProfile"])


# ---------------------------------------------------------------------------
# Nullability — the prerequisite for deriving null_check_column
# ---------------------------------------------------------------------------


def test_table_columns_carry_nullability():
    column_props = DEFINITIONS["Table"]["properties"]["columns"]["items"]["properties"]
    assert "is_nullable" in column_props, (
        "tools/plan_builder.py derives a MigrationTarget's null_check_column from "
        "this field; without it the null-profile check would have to be guessed."
    )
    assert column_props["is_nullable"]["type"] == ["boolean", "null"], (
        "null means 'this adapter cannot determine nullability' (e.g. the static "
        "Oracle DDL corpus) — distinct from False, and never treated as nullable."
    )


def test_table_record_with_nullability_validates():
    record = {
        "table_id": "wwi-sqlserver.WideWorldImporters.Sales.Customers",
        "system": "wwi-sqlserver",
        "database": "WideWorldImporters",
        "schema": "Sales",
        "table": "Customers",
        "classification": "UNCLASSIFIED",
        "primary_key": ["CustomerID"],
        "columns": [
            {"name": "CustomerID", "data_type": "int", "is_nullable": False},
            {"name": "PhoneNumber", "data_type": "nvarchar", "is_nullable": True},
            {"name": "LEGACY_COL", "data_type": "VARCHAR2", "is_nullable": None},
        ],
    }
    jsonschema.validate(instance=record, schema=DEFINITIONS["Table"])


# ---------------------------------------------------------------------------
# The new multi-estate entities
# ---------------------------------------------------------------------------


def test_migration_target_validates_a_fully_resolved_target():
    target = {
        "target_id": "wwi-sqlserver:Sales.Customers",
        "table_id": "wwi-sqlserver.WideWorldImporters.Sales.Customers",
        "source_database": "WideWorldImporters",
        "source_schema": "Sales",
        "source_table": "Customers",
        "target_table": "customers_dim",
        "key_column": "CustomerID",
        "order_by": "CustomerID",
        "numeric_column": "CreditLimit",
        "null_check_column": "PhoneNumber",
        "aggregate_check": "applicable",
        "execution_order": 0,
        "scheduled": True,
        "blocked": False,
        "blocked_reason": None,
        "sql_translation_notes": None,
    }
    jsonschema.validate(instance=target, schema=DEFINITIONS["MigrationTarget"])


def test_migration_target_allows_a_blocked_composite_key_table():
    """A composite PK yields a blocked target with a stated reason rather
    than a silently-chosen first key column."""
    target = {
        "target_id": "postgres-retail:retail.order_items",
        "table_id": "postgres-retail.retail.retail.order_items",
        "source_schema": "retail",
        "source_table": "order_items",
        "target_table": "order_items",
        "key_column": None,
        "numeric_column": None,
        "null_check_column": None,
        "aggregate_check": "not_applicable",
        "execution_order": 3,
        "scheduled": False,
        "blocked": True,
        "blocked_reason": "composite primary key not supported by the executor",
    }
    jsonschema.validate(instance=target, schema=DEFINITIONS["MigrationTarget"])


def test_estate_requires_at_least_one_source():
    estate = {"estate_id": "empty", "display_name": "Empty", "sources": []}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=estate, schema=DEFINITIONS["Estate"])


def test_committed_demo_estate_yaml_validates_against_the_estate_schema():
    """The estate.yaml this project ships must satisfy the schema that the
    onboarding wizard writes against — otherwise config-as-code and in-app
    authoring have quietly diverged before the registry is even built."""
    import yaml

    raw = yaml.safe_load(
        (REPO_ROOT / "simulator" / "source_setup" / "estate.yaml").read_text(encoding="utf-8")
    )
    jsonschema.validate(instance=raw, schema=DEFINITIONS["Estate"])
    for source in raw["sources"]:
        jsonschema.validate(instance=source, schema=DEFINITIONS["EstateSource"])
        if source.get("connection_profile") is not None:
            jsonschema.validate(
                instance=source["connection_profile"],
                schema=DEFINITIONS["ConnectionProfile"],
            )


def test_committed_packs_validate_against_the_pack_schema():
    import yaml

    packs = sorted((REPO_ROOT / "packs").glob("*/pack.yaml"))
    assert packs, "no committed packs found"
    for pack_path in packs:
        pack = yaml.safe_load(pack_path.read_text(encoding="utf-8"))
        jsonschema.validate(instance=pack, schema=DEFINITIONS["MigrationPack"])
