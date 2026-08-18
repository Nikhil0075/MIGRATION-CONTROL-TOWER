"""SourceAdapter interface (master doc §32.3's portability model).

Before this, every introspection function in tools/source_catalog.py
took source-specific arguments (a raw pyodbc.Connection, a hardcoded
local path) with no common shape a caller could program against without
knowing which source it was talking to. Onboarding a second real source
family meant writing new one-off code at every call site, not adding one
class.

Every source family this project can discover from implements this
interface. discover_tables()/discover_pipelines() must return the exact
same Table/Pipeline record shape (contracts/metadata_model.json)
regardless of source — tests/test_adapters.py's contract tests assert
this directly against every registered adapter, not just the one this
project happens to demo with. fetch_rows() is a generator, not a
pre-built list, so a streaming DataPlaneExecutor can consume it without
every adapter needing to change.

Day 11 Phase 3b widened the contract beyond discovery. The Validation
agent used to issue raw INFORMATION_SCHEMA and SELECT COUNT(*)/SUM()
statements against pyodbc from inside the agent, with the schema, table
and column names as module constants. That single function was the
hardest blocker to reconciling a non-SQL-Server estate: nothing about it
could be reused, and nothing about it could be configured.

**Capabilities, not assumptions.** Not every source can do everything —
the Oracle DDL corpus has no live database at all. Each adapter declares
what it supports; unsupported operations raise
AdapterCapabilityNotSupported rather than AttributeError, so the console
can disable an action before an operator triggers it and the failure,
when it happens, names the adapter and the operation.

**On `system` vs `source_id`.** These are deliberately still distinct.
An adapter's `system` tag (e.g. "sqlserver-wwi") is the prefix of every
table_id it has ever emitted; the estate's `source_id` for that same
source is "wwi-sqlserver". They were never equal, so rewriting table_id
to use source_id would change the identity of every already-catalogued
table and invalidate historical plan hashes for no benefit. Estate
scoping is carried by the run's estate_id and by health_key below, not
by rewriting identifiers. `legacy_system_alias` exists for the reverse
lookup where a consumer needs to match old records to a source.
"""

from __future__ import annotations

import abc
import datetime as dt
from typing import TYPE_CHECKING, Iterator

from tools.firestore_client import get_client

if TYPE_CHECKING:
    from tools.connection_context import SourceBinding

#: discover_tables / discover_pipelines
CAPABILITY_DISCOVER = "discover"
#: fetch_rows — the source can be read for bulk transfer
CAPABILITY_TRANSFER = "transfer"
#: count_rows / source_facts — the source can be reconciled against a target
CAPABILITY_RECONCILE = "reconcile"
#: health_check against a live server
CAPABILITY_HEALTH = "health"

ALL_CAPABILITIES = frozenset(
    {CAPABILITY_DISCOVER, CAPABILITY_TRANSFER, CAPABILITY_RECONCILE, CAPABILITY_HEALTH}
)


class AdapterCapabilityNotSupported(NotImplementedError):
    """This adapter cannot perform this operation, and says so specifically.

    Subclasses NotImplementedError deliberately: adapters already raised
    bare NotImplementedError from fetch_rows, and tests/test_adapters.py
    asserts that. Narrowing the type must not break the existing contract.
    """

    def __init__(self, adapter: str, operation: str, reason: str = ""):
        self.adapter = adapter
        self.operation = operation
        message = f"{adapter} does not support {operation!r}"
        if reason:
            message = f"{message}: {reason}"
        super().__init__(message)


class SourceAdapter(abc.ABC):
    #: Matches the `system` / `source_system` value this adapter stamps
    #: onto every Table/Pipeline record it produces (e.g. "sqlserver-wwi").
    system: str

    #: Historical `system` value, when an adapter's tag has changed. Lets a
    #: consumer match records catalogued before the rename without
    #: rewriting them.
    legacy_system_alias: str | None = None

    #: What this adapter can actually do. Declared, then verified —
    #: tests/test_adapters.py asserts every declared capability resolves to
    #: a working method and every undeclared one raises, so a new adapter
    #: cannot over-claim.
    capabilities: frozenset[str] = frozenset({CAPABILITY_DISCOVER})

    #: Which estate/source this instance is bound to, when it was built
    #: from one. None for the direct-construction path used by local CLI
    #: scripts and the existing tests.
    binding: "SourceBinding | None" = None

    @classmethod
    def supports(cls, capability: str) -> bool:
        return capability in cls.capabilities

    def _require(self, capability: str, operation: str) -> None:
        if capability not in self.capabilities:
            raise AdapterCapabilityNotSupported(
                type(self).__name__,
                operation,
                f"it does not declare the {capability!r} capability",
            )

    @property
    def health_key(self) -> str:
        """Document id for connection_health.

        Estate-scoped when bound, so two estates using the same adapter
        type do not overwrite each other's freshness snapshot. Falls back
        to the bare system tag for unbound instances, which is what every
        record written before Phase 2 used.
        """
        if self.binding is not None:
            return self.binding.health_key
        return self.system

    def record_connection_health(
        self, status: str, *, detail: str, object_count: int | None = None
    ) -> dict:
        """Persist a credential-free source freshness snapshot.

        The adapter owns the observation because it is the component that
        actually touched the source. Only source identity, outcome,
        freshness, and counts cross this boundary; connection strings and
        credentials do not enter Firestore or the UI service.
        """
        record = {
            "source_system": self.system,
            "estate_id": self.binding.estate_id if self.binding else None,
            "source_id": self.binding.source_id if self.binding else None,
            "status": status,
            "detail": detail,
            "object_count": object_count,
            "last_observed_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        get_client().collection("connection_health").document(self.health_key).set(record)
        return record

    # -- Discovery (required of every adapter) ------------------------------

    @abc.abstractmethod
    def discover_tables(self) -> list[dict]:
        """Returns Table records (contracts/metadata_model.json),
        classification='UNCLASSIFIED' — sensitivity judgment belongs to
        the Risk agent, never to a discovery-time adapter (§4.2)."""

    @abc.abstractmethod
    def discover_pipelines(self) -> list[dict]:
        """Returns Pipeline records. Empty for adapters with no
        associated scheduling/job metadata of their own (e.g. a source
        whose jobs are declared in a separate DAG artifact source)."""

    @abc.abstractmethod
    def fetch_rows(self, table_id: str, order_by: str) -> Iterator[dict]:
        """Yields row dicts for table_id in order_by order. Raises
        NotImplementedError for a discovery/assessment-only adapter that
        has no live database to move data from (e.g. OracleCorpusAdapter
        — see its docstring)."""

    # -- Optional, capability-gated ----------------------------------------

    def health_check(self) -> dict:
        """Credential-free liveness probe for one source.

        Returns {status, detail, object_count, latency_ms}. This is what
        the onboarding wizard's "Validate connection" step calls, so it
        must never echo a connection string or credential — `detail` is
        operator-facing text, not a dump of the connection parameters.
        """
        raise AdapterCapabilityNotSupported(type(self).__name__, "health_check")

    def get_table_schema(self, table_ref: str) -> list[dict]:
        """Returns [{name, data_type, is_nullable}] for 'Schema.Table'."""
        raise AdapterCapabilityNotSupported(type(self).__name__, "get_table_schema")

    def count_rows(self, table_ref: str) -> int:
        raise AdapterCapabilityNotSupported(type(self).__name__, "count_rows")

    def source_facts(self, target: dict) -> dict:
        """Every source-side fact reconciliation needs, in one round trip.

        `target` is a MigrationTarget (contracts/metadata_model.json), so
        the column names come from the plan rather than from constants in
        the calling agent — that indirection is the entire point.

        Returns {columns, row_count, numeric_sum, null_count, keys}, with
        numeric_sum/null_count None when the target declares no
        numeric_column/null_check_column. Deliberately one method rather
        than four: three or four separate methods is three or four
        opportunities for a new adapter to be half-implemented while still
        passing a contract test.
        """
        raise AdapterCapabilityNotSupported(type(self).__name__, "source_facts")

    def column_plan(self, table_ref: str, type_rules: dict) -> tuple[list[str], list[str], list[str]]:
        """Returns (select_expressions, carried_columns, excluded_columns).

        `type_rules` comes from the Migration Pack — which types convert
        cleanly, which need a dialect-specific conversion, which have no
        honest conversion at all. The rules are configuration; the
        mechanism that applies them is dialect-specific and therefore
        lives on the adapter.
        """
        raise AdapterCapabilityNotSupported(type(self).__name__, "column_plan")
