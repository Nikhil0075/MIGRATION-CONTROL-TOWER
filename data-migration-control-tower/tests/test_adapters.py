"""Contract tests for tools/adapters/ (Day 10 Phase 3, master doc §32).

Every registered adapter must satisfy the same shape regardless of
source family — these tests run against ADAPTER_TYPES generically
(parametrized), not hand-written per adapter, so a future adapter that
forgets a piece of the contract fails here rather than silently
producing malformed Table/Pipeline records downstream.

SqlServerAdapter's discover_tables()/fetch_rows() need the live WWI
container (skip automatically when unreachable — same pattern as
tests/test_source_catalog.py). OracleCorpusAdapter and DagArtifactAdapter
are static-file-based and always run.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from tools.adapters import ADAPTER_TYPES, build_adapter  # noqa: E402
from tools.adapters.dag_artifact_adapter import DagArtifactAdapter  # noqa: E402
from tools.adapters.oracle_corpus_adapter import OracleCorpusAdapter  # noqa: E402
from tools.adapters.sqlserver_adapter import SqlServerAdapter  # noqa: E402

ORACLE_CORPUS_PATH = REPO_ROOT / "simulator" / "source_setup" / "oracle_dialect_corpus"
DAG_ARTIFACTS_PATH = REPO_ROOT / "simulator" / "source_setup" / "dags"


def _sql_server_reachable() -> bool:
    try:
        from tools.sqlserver_client import get_connection

        get_connection().close()
        return True
    except Exception:  # noqa: BLE001
        return False


skip_if_no_sql_server = pytest.mark.skipif(not _sql_server_reachable(), reason="SQL Server container not reachable")

# (adapter instance, expects_tables, expects_pipelines, can_fetch_rows)
_STATIC_ADAPTERS = [
    (OracleCorpusAdapter(ORACLE_CORPUS_PATH), True, False, False),
    (DagArtifactAdapter(DAG_ARTIFACTS_PATH), False, True, False),
]


def test_adapter_types_registry_contains_every_shipped_adapter():
    """Asserts a subset, not an exact set, and deliberately so.

    Registering a new source family should be one line in ADAPTER_TYPES
    (§32.1's R3 claim). An exact-equality assertion here would make every
    new adapter also require a test edit — a small contradiction of the
    thing this suite exists to prove. What matters is that the adapters
    this project ships stay registered; that a fourth appeared is the
    feature working.
    """
    assert {"sqlserver", "oracle_corpus", "dag_artifacts", "postgres"} <= set(ADAPTER_TYPES)


def test_every_registered_adapter_implements_the_contract():
    """The real guard: whatever is registered must satisfy SourceAdapter,
    so a half-finished adapter cannot be reachable from an estate."""
    from tools.adapters.base import SourceAdapter as _Base

    for name, cls in ADAPTER_TYPES.items():
        assert issubclass(cls, _Base), name
        assert getattr(cls, "system", None), f"{name} declares no system tag"
        assert getattr(cls, "capabilities", None), f"{name} declares no capabilities"


def test_build_adapter_rejects_unknown_type():
    with pytest.raises(ValueError, match="Unknown adapter type"):
        build_adapter("not_a_real_adapter")


def test_build_adapter_passes_config_through():
    adapter = build_adapter("oracle_corpus", corpus_path=str(ORACLE_CORPUS_PATH))
    assert isinstance(adapter, OracleCorpusAdapter)
    assert adapter.corpus_path == str(ORACLE_CORPUS_PATH)


@pytest.mark.parametrize(
    "adapter,expects_tables,expects_pipelines,can_fetch_rows",
    _STATIC_ADAPTERS,
    ids=["oracle_corpus", "dag_artifacts"],
)
def test_adapter_contract_shape(adapter, expects_tables, expects_pipelines, can_fetch_rows):
    """The contract every adapter must satisfy, regardless of source."""
    tables = adapter.discover_tables()
    pipelines = adapter.discover_pipelines()

    assert isinstance(tables, list)
    assert isinstance(pipelines, list)
    if expects_tables:
        assert tables, f"{type(adapter).__name__} should discover at least one table"
        for t in tables:
            assert t["classification"] == "UNCLASSIFIED"  # sensitivity is Risk's job, not discovery's
            assert t["discovered_by"] == "discovery-agent"
            assert t["table_id"]
    else:
        assert tables == []

    if expects_pipelines:
        assert pipelines, f"{type(adapter).__name__} should discover at least one pipeline"
        for p in pipelines:
            assert p["pipeline_id"]
    else:
        assert pipelines == []

    if not can_fetch_rows:
        with pytest.raises(NotImplementedError):
            next(adapter.fetch_rows("whatever.table.id", order_by="id"))


def _without_timestamp(records: list[dict], key: str) -> list[dict]:
    """discovered_at is a fresh dt.datetime.now() on every call — strip it
    before comparing two independent invocations for structural equality."""
    return [{k: v for k, v in r.items() if k != key} for r in records]


def test_oracle_corpus_adapter_matches_source_catalog_directly():
    """Pure-refactor guarantee: the adapter must return exactly what the
    underlying tools/source_catalog.py function already returns."""
    from tools.source_catalog import catalog_oracle_corpus

    direct = catalog_oracle_corpus(ORACLE_CORPUS_PATH)
    via_adapter = OracleCorpusAdapter(ORACLE_CORPUS_PATH).discover_tables()
    assert _without_timestamp(direct, "discovered_at") == _without_timestamp(via_adapter, "discovered_at")


def test_dag_artifact_adapter_matches_source_catalog_directly():
    from tools.source_catalog import catalog_dag_artifacts

    direct = catalog_dag_artifacts(DAG_ARTIFACTS_PATH)
    via_adapter = DagArtifactAdapter(DAG_ARTIFACTS_PATH).discover_pipelines()
    assert _without_timestamp(direct, "discovered_at") == _without_timestamp(via_adapter, "discovered_at")


def test_registered_estate_discovery_builds_every_declared_binding(monkeypatch):
    from agents.discovery import agent as discovery

    estate = {
        "estate_id": "registered-estate",
        "sources": [
            {"source_id": "sql-one", "adapter": "sqlserver"},
            {"source_id": "dag-one", "adapter": "dag_artifacts"},
        ],
    }
    built = []

    class FakeAdapter:
        def __init__(self, source_id):
            self.source_id = source_id

        def discover_tables(self):
            return [{"table_id": self.source_id}] if self.source_id == "sql-one" else []

        def discover_pipelines(self):
            return [{"pipeline_id": self.source_id}] if self.source_id == "dag-one" else []

        def record_connection_health(self, *_args, **_kwargs):
            return None

    class Binding:
        def __init__(self, source_id):
            self.source_id = source_id
            self.requires_connection = source_id == "sql-one"

    monkeypatch.setattr("tools.connection_context.load_estate_document", lambda _id: estate)
    monkeypatch.setattr(
        "tools.connection_context.binding_from_estate",
        lambda _estate, source_id: Binding(source_id),
    )
    monkeypatch.setattr(
        "tools.adapters.build_adapter_for_binding",
        lambda binding: (built.append(binding.source_id), FakeAdapter(binding.source_id))[1],
    )

    tables, pipelines = discovery.discover_estate(estate_id="registered-estate")
    assert built == ["sql-one", "dag-one"]
    assert tables == [{"table_id": "sql-one"}]
    assert pipelines == [{"pipeline_id": "dag-one"}]


@skip_if_no_sql_server
def test_sqlserver_adapter_contract_shape():
    adapter = SqlServerAdapter()
    tables = adapter.discover_tables()
    assert tables, "SqlServerAdapter should discover WWI tables"
    for t in tables:
        assert t["classification"] == "UNCLASSIFIED"
        assert t["discovered_by"] == "discovery-agent"
    assert adapter.discover_pipelines() == []


@skip_if_no_sql_server
def test_sqlserver_adapter_matches_source_catalog_directly():
    from tools.source_catalog import catalog_sql_server_tables
    from tools.sqlserver_client import get_connection

    conn = get_connection()
    try:
        direct = catalog_sql_server_tables(conn)
    finally:
        conn.close()
    via_adapter = SqlServerAdapter().discover_tables()
    assert _without_timestamp(direct, "discovered_at") == _without_timestamp(via_adapter, "discovered_at")


@skip_if_no_sql_server
def test_sqlserver_adapter_fetch_rows_matches_migration_executor_directly():
    from tools.migration_executor import fetch_source_rows

    # fetch_source_rows() returns a generator (Day 10 Phase 4 streaming) —
    # materialize it to compare against the adapter's own generator output.
    _columns, direct_row_iter, _excluded = fetch_source_rows("Sales", "Customers", "CustomerID")
    direct_rows = list(direct_row_iter)
    via_adapter = list(SqlServerAdapter().fetch_rows("sqlserver-wwi.WideWorldImporters.Sales.Customers", "CustomerID"))
    assert direct_rows == via_adapter


# ---------------------------------------------------------------------------
# Capability coherence (Day 11 Phase 8)
# ---------------------------------------------------------------------------
#
# `capabilities` is a CLAIM an adapter makes about itself, and the console
# acts on it: GET /adapter-types drives which actions the onboarding wizard
# offers, and _packs() derives execution_supported from it. An adapter that
# over-claims produces a UI offering an action that then fails at run time;
# one that under-claims silently disables working functionality.
#
# These tests make the claim honest. They are parametrized over
# ADAPTER_TYPES rather than written per adapter, so a source family added
# in future is held to the same contract without anyone remembering to
# extend this file.

# capability -> (method name, arguments that reach the capability check
# before any connection is attempted)
_CAPABILITY_METHODS = {
    "reconcile": (
        "source_facts",
        ({"source_schema": "s", "source_table": "t", "key_column": "k",
          "target_id": "probe", "target_table": "tt"},),
    ),
}


@pytest.mark.parametrize("adapter_type", sorted(ADAPTER_TYPES))
def test_declared_capabilities_are_all_known(adapter_type):
    """A typo in a capability string would silently disable a feature —
    "transfr" is not an error anywhere, it just never matches."""
    from tools.adapters.base import ALL_CAPABILITIES

    declared = ADAPTER_TYPES[adapter_type].capabilities
    unknown = set(declared) - set(ALL_CAPABILITIES)
    assert not unknown, (
        f"{adapter_type} declares unknown capabilities {sorted(unknown)}; "
        f"known: {sorted(ALL_CAPABILITIES)}"
    )


@pytest.mark.parametrize("adapter_type", sorted(ADAPTER_TYPES))
def test_every_adapter_declares_discovery(adapter_type):
    """discover_tables/discover_pipelines are abstract, so every adapter
    implements them — the declaration must say so."""
    assert "discover" in ADAPTER_TYPES[adapter_type].capabilities


@pytest.mark.parametrize("adapter_type", sorted(ADAPTER_TYPES))
def test_undeclared_capabilities_raise_a_typed_error(adapter_type):
    """The important half: NOT declaring a capability must mean the method
    raises AdapterCapabilityNotSupported — not AttributeError, and not a
    confusing failure deep inside a connection attempt. The console shows
    this message to an operator.
    """
    from tools.adapters.base import AdapterCapabilityNotSupported

    cls = ADAPTER_TYPES[adapter_type]
    instance = object.__new__(cls)  # no __init__: this must not need config
    instance.binding = None

    for capability, (method_name, args) in _CAPABILITY_METHODS.items():
        if capability in cls.capabilities:
            continue
        with pytest.raises(AdapterCapabilityNotSupported) as excinfo:
            getattr(instance, method_name)(*args)
        message = str(excinfo.value)
        assert cls.__name__ in message, "the error must name the adapter"
        assert method_name in message, "the error must name the operation"


@pytest.mark.parametrize("adapter_type", sorted(ADAPTER_TYPES))
def test_capability_gated_methods_are_not_left_at_the_base_stub(adapter_type):
    """A DECLARED capability must be backed by a real override.

    Inheriting the base stub while claiming the capability is the exact
    failure this catches: the adapter would pass every shape test, appear
    fully featured in the console, and then raise the moment an operator
    used it.
    """
    from tools.adapters.base import SourceAdapter

    cls = ADAPTER_TYPES[adapter_type]
    required_overrides = {
        "health": ["health_check"],
        "reconcile": ["source_facts", "get_table_schema", "count_rows"],
        "transfer": ["fetch_rows", "column_plan"],
    }
    for capability, methods in required_overrides.items():
        if capability not in cls.capabilities:
            continue
        for method_name in methods:
            assert getattr(cls, method_name) is not getattr(SourceAdapter, method_name, None), (
                f"{cls.__name__} declares the {capability!r} capability but inherits the "
                f"base {method_name}() stub, so the console would offer an action that "
                f"raises as soon as it is used."
            )


@pytest.mark.parametrize("adapter_type", sorted(ADAPTER_TYPES))
def test_health_key_is_estate_scoped_when_bound(adapter_type):
    """Two estates using the same adapter type must not overwrite each
    other's connection_health snapshot."""
    from tools.connection_context import SourceBinding

    cls = ADAPTER_TYPES[adapter_type]
    instance = object.__new__(cls)
    instance.binding = SourceBinding(
        estate_id="estate-a", source_id="src", adapter=adapter_type
    )
    assert instance.health_key == "estate-a__src"

    instance.binding = None
    assert instance.health_key == cls.system, "unbound falls back to the system tag"


def test_describe_adapters_reports_what_the_console_renders():
    """GET /adapter-types is generated from this; the wizard's adapter
    picker and its disabled actions both come from these fields."""
    from tools.adapters import describe_adapters

    described = {item["adapter_type"]: item for item in describe_adapters()}
    assert set(described) == set(ADAPTER_TYPES)
    for adapter_type, item in described.items():
        assert item["capabilities"] == sorted(ADAPTER_TYPES[adapter_type].capabilities)
        assert item["system"], f"{adapter_type} reports no system tag"
