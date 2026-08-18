"""Clean-estate onboarding — the release gate (master doc §32.11).

The reproducibility claim this project makes is not "the WideWorldImporters
demo replays". §32.11 states it plainly: the strongest test is onboarding a
*second* estate without modifying the core fleet.

This file is that test. The assertion that carries the whole claim is
`test_no_agent_module_mentions_the_second_estate` — a mechanical grep over
`agents/`. Every other test here shows the second estate working; only that
one proves the fleet did not have to change for it, and it is the only form
of the claim a test can actually enforce. Review cannot: a single
`if source == "postgres"` inside an agent would pass every other test in
this file while quietly falsifying the headline.

Tests needing the fixture container skip when it is not running:

    cd simulator/source_setup/postgres && docker compose up -d
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.conftest import REPO_ROOT

ESTATE_ID = "retail-postgres-estate"
SOURCE_ID = "retail-postgres"
PACK_ID = "postgres_retail_v1"

AGENTS_DIR = REPO_ROOT / "agents"


@pytest.fixture(scope="module")
def binding():
    from tools.connection_context import binding_for

    return binding_for(ESTATE_ID, SOURCE_ID)


@pytest.fixture(scope="module")
def adapter(binding):
    from tools.adapters import build_adapter_for_binding

    return build_adapter_for_binding(binding)


@pytest.fixture(scope="module")
def pack():
    from tools.pack_loader import get_pack

    return get_pack(PACK_ID)


# ---------------------------------------------------------------------------
# THE GATE — the fleet must not know this estate exists
# ---------------------------------------------------------------------------

#: Terms that only appear if an agent was taught about the second estate.
#: "postgres" is included deliberately: the whole claim is that the agent
#: fleet reasons over a normalized estate model, so no agent should name a
#: database engine at all.
_FORBIDDEN_IN_AGENTS = [
    ESTATE_ID,
    SOURCE_ID,
    PACK_ID,
    "retaildb",
    "postgres",
    "psycopg",
    "order_items",
]


def _agent_sources() -> list[Path]:
    return sorted(AGENTS_DIR.rglob("*.py"))


def test_agent_modules_exist_to_be_checked():
    """Guards the gate: an empty file list would make the next test pass
    while proving nothing."""
    assert len(_agent_sources()) > 10


@pytest.mark.parametrize("term", _FORBIDDEN_IN_AGENTS)
def test_no_agent_module_mentions_the_second_estate(term):
    """§32.11's actual claim, mechanically enforced.

    A second estate was onboarded by adding one adapter, one pack, one
    estate YAML and one line in ADAPTER_TYPES. If this fails, something
    under agents/ was taught about a specific source — which is exactly
    the coupling the adapter contract exists to prevent, and it would make
    the portability claim false no matter how green the rest of the suite is.
    """
    pattern = re.compile(re.escape(term), re.IGNORECASE)
    offenders = []
    for path in _agent_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()[:100]}")

    assert not offenders, (
        f"agent code mentions {term!r}, so the second estate is NOT onboarded by "
        f"configuration alone:\n  " + "\n  ".join(offenders)
    )


def test_registering_the_adapter_is_one_line():
    """R3 (§32.1): a new source technology is added behind a stable
    contract, not by editing call sites."""
    from tools.adapters import ADAPTER_TYPES
    from tools.adapters.postgres_adapter import PostgresAdapter

    assert ADAPTER_TYPES["postgres"] is PostgresAdapter

    registry_source = (REPO_ROOT / "tools" / "adapters" / "__init__.py").read_text(encoding="utf-8")
    postgres_lines = [
        line for line in registry_source.splitlines()
        if "postgres" in line.lower() and not line.strip().startswith("#")
    ]
    # One import, one registry entry. Nothing else.
    assert len(postgres_lines) == 2, postgres_lines


# ---------------------------------------------------------------------------
# The second estate satisfies the same contracts as the first
# ---------------------------------------------------------------------------


def test_the_estate_and_pack_validate_against_the_shared_contracts():
    import json

    import jsonschema
    import yaml

    definitions = json.loads(
        (REPO_ROOT / "contracts" / "metadata_model.json").read_text(encoding="utf-8")
    )["definitions"]

    estate = yaml.safe_load(
        (REPO_ROOT / "config" / "estates" / "retail-postgres.yaml").read_text(encoding="utf-8")
    )
    jsonschema.validate(instance=estate, schema=definitions["Estate"])

    pack = yaml.safe_load(
        (REPO_ROOT / "packs" / PACK_ID / "pack.yaml").read_text(encoding="utf-8")
    )
    jsonschema.validate(instance=pack, schema=definitions["MigrationPack"])


def test_the_estate_is_discoverable_without_the_registry():
    """A clean clone with no Firestore must still resolve committed
    estates, or `make assess` cannot be the first thing a new team runs."""
    from tools.connection_context import binding_for

    resolved = binding_for(ESTATE_ID, SOURCE_ID)
    assert resolved.adapter == "postgres"
    assert resolved.requires_connection


def test_the_pack_is_assessment_only_until_someone_decides_otherwise(pack):
    """§32.5: production-write access must never be a prerequisite for
    discovering an estate."""
    from tools.pack_loader import default_mode, supports_execution

    assert default_mode(pack) == "assessment"
    assert supports_execution(pack) is False


# ---------------------------------------------------------------------------
# Live fixture: discovery, derivation and reconciliation facts
# ---------------------------------------------------------------------------


@pytest.mark.requires_postgres
def test_health_check_reports_without_leaking_the_credential(adapter):
    import os

    result = adapter.health_check()
    assert result["status"] == "HEALTHY"
    assert result["object_count"] == 4
    password = os.environ.get("POSTGRES_PASSWORD")
    if password:
        assert password not in str(result)
    # It reports HOW it authenticated, which is what makes an unnoticed
    # local-dev fallback visible in a real deployment.
    assert "credential via" in result["detail"]


@pytest.mark.requires_postgres
def test_discovery_emits_contract_valid_records(adapter):
    import json

    import jsonschema

    table_schema = json.loads(
        (REPO_ROOT / "contracts" / "metadata_model.json").read_text(encoding="utf-8")
    )["definitions"]["Table"]

    tables = adapter.discover_tables()
    assert {t["table"] for t in tables} == {"customers", "orders", "order_items", "tags"}
    for table in tables:
        jsonschema.validate(instance=table, schema=table_schema)
        assert table["classification"] == "UNCLASSIFIED"
        assert table["discovered_by"] == "discovery-agent"
        # Nullability is what makes null_check_column derivable at all.
        assert all("is_nullable" in column for column in table["columns"])


@pytest.mark.requires_postgres
def test_targets_are_derived_from_metadata_not_declared(adapter, pack):
    """This pack declares no scheduled_tables — the path a real customer
    takes. Everything below comes from the discovered catalog plus the
    pack's type rules."""
    from tools.plan_builder import build_steps, build_targets

    tables = adapter.discover_tables()
    steps = build_steps(tables, [], set(), {})
    targets = {
        t["source_table"]: t
        for t in build_targets(tables, steps, pack=pack,
                               estate_id=ESTATE_ID, source_id=SOURCE_ID)
    }

    customers = targets["customers"]
    assert customers["scheduled"] is True
    assert customers["key_column"] == "customer_id"
    assert customers["numeric_column"] == "credit_limit"
    assert customers["null_check_column"] == "email_address"
    assert customers["aggregate_check"] == "applicable"


@pytest.mark.requires_postgres
def test_a_composite_primary_key_is_blocked_with_a_reason(adapter, pack):
    """WideWorldImporters contains no composite primary keys at all, so
    without this fixture the blocked path would ship untested."""
    from tools.plan_builder import BLOCKED_COMPOSITE_PRIMARY_KEY, build_steps, build_targets

    tables = adapter.discover_tables()
    steps = build_steps(tables, [], set(), {})
    targets = {
        t["source_table"]: t
        for t in build_targets(tables, steps, pack=pack, source_id=SOURCE_ID)
    }

    blocked = targets["order_items"]
    assert blocked["blocked"] is True
    assert blocked["scheduled"] is False
    assert blocked["key_column"] is None
    assert blocked["blocked_reason"] == BLOCKED_COMPOSITE_PRIMARY_KEY


@pytest.mark.requires_postgres
def test_a_table_without_a_numeric_column_omits_the_aggregate_check(adapter, pack):
    from tools.plan_builder import build_steps, build_targets

    tables = adapter.discover_tables()
    steps = build_steps(tables, [], set(), {})
    tags = next(
        t for t in build_targets(tables, steps, pack=pack, source_id=SOURCE_ID)
        if t["source_table"] == "tags"
    )
    assert tags["scheduled"] is True, "still migratable — just one fewer check"
    assert tags["numeric_column"] is None
    assert tags["aggregate_check"] == "not_applicable"


@pytest.mark.requires_postgres
def test_source_facts_returns_the_same_shape_as_the_sql_server_adapter(adapter, pack):
    """The reason agents/validation/agent.py can reconcile this estate
    without knowing it exists."""
    from tools.plan_builder import build_steps, build_targets

    tables = adapter.discover_tables()
    steps = build_steps(tables, [], set(), {})
    customers = next(
        t for t in build_targets(tables, steps, pack=pack, source_id=SOURCE_ID)
        if t["source_table"] == "customers"
    )

    facts = adapter.source_facts(customers)
    assert set(facts) == {"columns", "row_count", "numeric_sum", "null_count", "keys"}
    assert facts["row_count"] == 5
    assert facts["null_count"] == 2, "two seeded customers have no email address"
    assert facts["numeric_sum"] == pytest.approx(110251.50)
    assert facts["keys"] == ["1", "2", "3", "4", "5"]


@pytest.mark.requires_postgres
def test_rows_stream_json_safe_values(adapter):
    rows = list(adapter.fetch_rows("retail.customers", "customer_id"))
    assert len(rows) == 5
    import json

    json.dumps(rows)  # Decimal/datetime would raise here if not normalized


@pytest.mark.requires_postgres
def test_unsupported_capabilities_raise_a_typed_error():
    """An adapter that cannot do something says so specifically, so the
    console can disable the action instead of failing at run time."""
    from tools.adapters import build_adapter
    from tools.adapters.base import AdapterCapabilityNotSupported

    corpus = build_adapter("oracle_corpus")
    with pytest.raises(AdapterCapabilityNotSupported, match="source_facts"):
        corpus.source_facts({"source_schema": "s", "source_table": "t", "key_column": "k"})
