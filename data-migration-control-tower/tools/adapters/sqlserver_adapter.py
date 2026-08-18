"""SQL Server SourceAdapter.

Day 10 introduced this as a thin wrapper over
tools/source_catalog.py::catalog_sql_server_tables and
tools/migration_executor.py::fetch_source_rows — a pure refactor with the
execution path still calling those functions directly.

Day 11 Phase 3b made it load-bearing. The reconciliation queries that
used to live inside agents/validation/agent.py as raw SQL over a
process-global connection, with the schema/table/column names as module
constants, are now source_facts() here — driven by a MigrationTarget from
the plan. That is what lets a different source family be reconciled
without editing the Validation agent.
"""

from __future__ import annotations

import time
from typing import Iterator

from tools.adapters.base import (
    CAPABILITY_DISCOVER,
    CAPABILITY_HEALTH,
    CAPABILITY_RECONCILE,
    CAPABILITY_TRANSFER,
    SourceAdapter,
)
from tools.source_catalog import catalog_sql_server_tables
from tools.sqlserver_client import get_connection


class SqlServerAdapter(SourceAdapter):
    system = "sqlserver-wwi"
    capabilities = frozenset(
        {CAPABILITY_DISCOVER, CAPABILITY_TRANSFER, CAPABILITY_RECONCILE, CAPABILITY_HEALTH}
    )

    def __init__(self, database: str = "WideWorldImporters", *, binding=None):
        self.database = database
        self.binding = binding

    def _connect(self):
        """Per-estate when bound, process-global env otherwise.

        The unbound path is what keeps every existing caller and the local
        CLI scripts working; the bound path is what allows two SQL Server
        estates in one process (tools/connection_context.py).
        """
        if self.binding is not None and self.binding.requires_connection:
            return get_connection(database=self.database, profile=self.binding.resolve())
        return get_connection(database=self.database)

    @staticmethod
    def _split(table_ref: str) -> tuple[str, str]:
        """Accepts 'Schema.Table' or a full 'system.db.Schema.Table' table_id."""
        parts = [p for p in table_ref.split(".") if p]
        if len(parts) < 2:
            raise ValueError(
                f"Cannot resolve {table_ref!r} to a schema and table — expected "
                f"'Schema.Table' or 'system.database.Schema.Table'."
            )
        return parts[-2], parts[-1]

    # -- Discovery ----------------------------------------------------------

    def discover_tables(self) -> list[dict]:
        conn = self._connect()
        try:
            return catalog_sql_server_tables(conn, database=self.database)
        finally:
            conn.close()

    def discover_pipelines(self) -> list[dict]:
        # SQL Server itself carries no job/DAG metadata in this estate —
        # scheduling info lives in the separate DAG artifact corpus
        # (DagArtifactAdapter), not here.
        return []

    def fetch_rows(self, table_id: str, order_by: str) -> Iterator[dict]:
        from tools.migration_executor import fetch_source_rows

        schema_name, table_name = self._split(table_id)
        _columns, rows, _excluded = fetch_source_rows(
            schema_name, table_name, order_by, binding=self.binding
        )
        yield from rows

    # -- Health -------------------------------------------------------------

    def health_check(self) -> dict:
        """Live probe. Records the observation and returns it.

        `detail` names the server version and how the credential was
        resolved — never the credential, and never the connection string.
        Surfacing the resolution backend matters: a source silently running
        on the local-dev environment fallback instead of Secret Manager is
        a real deployment defect that otherwise looks like success.
        """
        started = time.monotonic()
        try:
            conn = self._connect()
        except Exception as exc:  # noqa: BLE001
            record = self.record_connection_health(
                "UNREACHABLE", detail=f"connection failed: {type(exc).__name__}: {exc}"
            )
            return {**record, "latency_ms": int((time.monotonic() - started) * 1000)}

        try:
            cursor = conn.cursor()
            cursor.execute("SELECT @@VERSION")
            version = str(cursor.fetchone()[0]).splitlines()[0].strip()
            cursor.execute(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'"
            )
            object_count = int(cursor.fetchone()[0])
        except Exception as exc:  # noqa: BLE001
            record = self.record_connection_health(
                "DEGRADED", detail=f"connected, but introspection failed: {exc}"
            )
            return {**record, "latency_ms": int((time.monotonic() - started) * 1000)}
        finally:
            conn.close()

        detail = f"{version}; {object_count} base tables"
        if self.binding is not None and self.binding.requires_connection:
            from tools.secret_resolver import describe_resolution

            profile = self.binding.connection_profile or {}
            detail += f"; credential via {describe_resolution(profile.get('password_secret_ref'), env_fallback=profile.get('password_env'))}"

        record = self.record_connection_health("HEALTHY", detail=detail, object_count=object_count)
        return {**record, "latency_ms": int((time.monotonic() - started) * 1000)}

    # -- Reconciliation -----------------------------------------------------

    def get_table_schema(self, table_ref: str) -> list[dict]:
        schema_name, table_name = self._split(table_ref)
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                schema_name,
                table_name,
            )
            return [
                {"name": n, "data_type": t, "is_nullable": nullable == "YES"}
                for n, t, nullable in cursor.fetchall()
            ]
        finally:
            conn.close()

    def count_rows(self, table_ref: str) -> int:
        schema_name, table_name = self._split(table_ref)
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) FROM [{schema_name}].[{table_name}]")
            return int(cursor.fetchone()[0])
        finally:
            conn.close()

    def source_facts(self, target: dict) -> dict:
        """Source side of all five reconciliation checks, in one connection.

        Column names come from the MigrationTarget, so this method contains
        no knowledge of which estate it is reconciling. numeric_sum and
        null_count are None when the target declares no such column — the
        caller omits that check rather than comparing against a fabricated
        zero.
        """
        self._require("reconcile", "source_facts")
        schema_name = target["source_schema"]
        table_name = target["source_table"]
        key_column = target.get("key_column")
        numeric_column = target.get("numeric_column")
        null_check_column = target.get("null_check_column")

        if not key_column:
            raise ValueError(
                f"Target {target.get('target_id')!r} declares no key_column; it should have "
                f"been blocked by tools/plan_builder.py rather than reaching reconciliation."
            )

        qualified = f"[{schema_name}].[{table_name}]"
        conn = self._connect()
        try:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
                schema_name,
                table_name,
            )
            columns = [r[0] for r in cursor.fetchall()]

            cursor.execute(f"SELECT COUNT(*) FROM {qualified}")
            row_count = int(cursor.fetchone()[0])

            numeric_sum = None
            if numeric_column:
                cursor.execute(f"SELECT SUM([{numeric_column}]) FROM {qualified}")
                numeric_sum = float(cursor.fetchone()[0] or 0)

            null_count = None
            if null_check_column:
                cursor.execute(
                    f"SELECT COUNT(*) FROM {qualified} WHERE [{null_check_column}] IS NULL"
                )
                null_count = int(cursor.fetchone()[0])

            cursor.execute(
                f"SELECT CAST([{key_column}] AS NVARCHAR(50)) FROM {qualified} "
                f"ORDER BY [{key_column}]"
            )
            keys = [r[0] for r in cursor.fetchall()]
        finally:
            conn.close()

        return {
            "columns": columns,
            "row_count": row_count,
            "numeric_sum": numeric_sum,
            "null_count": null_count,
            "keys": keys,
        }

    # -- Transfer -----------------------------------------------------------

    def column_plan(self, table_ref: str, type_rules: dict) -> tuple[list[str], list[str], list[str]]:
        """Which columns can be carried, and how.

        geography/geometry/hierarchyid are carried via SQL Server's own
        .ToString() rather than dropped: excluding them used to make
        post-remediation schema checks fail permanently, because the target
        could never match the source's full column set.
        """
        schema_name, table_name = self._split(table_ref)
        scalar_safe = {t.lower() for t in type_rules.get("scalar_safe", set())}
        stringify = {t.lower() for t in type_rules.get("stringify_via_method", set())}

        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ? ORDER BY ORDINAL_POSITION",
                schema_name,
                table_name,
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        select_exprs: list[str] = []
        carried: list[str] = []
        for name, data_type in rows:
            lowered = str(data_type).lower()
            if lowered in scalar_safe:
                select_exprs.append(f"[{name}]")
                carried.append(name)
            elif lowered in stringify:
                select_exprs.append(f"[{name}].ToString() AS [{name}]")
                carried.append(name)
            # else: excluded, or a type no rule mentions — skipped, and
            # reported below so the gap is visible in the manifest.
        excluded = [name for name, _ in rows if name not in carried]
        return select_exprs, carried, excluded
