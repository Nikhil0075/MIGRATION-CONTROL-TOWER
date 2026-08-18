"""PostgreSQL SourceAdapter — the second-estate proof (master doc §32.11).

This file is the R3 extensibility claim made concrete. Adding a new source
family should mean writing one adapter and registering it, not editing the
agent fleet: nothing in `agents/` mentions Postgres, this schema, or this
estate, and tests/test_clean_estate_onboarding.py asserts that
mechanically rather than by review.

Everything source-specific lives here or in the Migration Pack. The
catalog SQL differs from SQL Server's (information_schema quirks,
`reltuples` instead of `sys.partitions`, `%s` placeholders instead of
`?`), and the parameter style differs — and none of that reaches
Discovery, Lineage, Risk, the Planner or Validation, because all of them
consume the normalized Table/MigrationTarget shapes.

Deliberately NOT implemented: `column_plan`'s dialect conversion tricks.
SQL Server needs `.ToString()` for geography/geometry/hierarchyid;
Postgres's own types either convert cleanly to JSON or are excluded by the
pack's type rules. Inventing an equivalent would be speculative.
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
from tools.source_catalog import catalog_postgres_tables

DEFAULT_BATCH_SIZE = 500


def _json_safe(value):
    """Postgres returns date/datetime/Decimal objects; BigQuery's JSON load
    needs primitives. Same normalization tools/migration_executor.py applies
    to pyodbc output, kept local so this adapter has no dependency on the
    SQL Server transfer path."""
    import datetime as dt
    import decimal
    import uuid as _uuid

    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (_uuid.UUID, memoryview, bytes)):
        return str(value)
    return value


class PostgresAdapter(SourceAdapter):
    system = "postgres"
    capabilities = frozenset(
        {CAPABILITY_DISCOVER, CAPABILITY_TRANSFER, CAPABILITY_RECONCILE, CAPABILITY_HEALTH}
    )

    def __init__(
        self,
        database: str = "retaildb",
        *,
        system: str | None = None,
        binding=None,
    ):
        self.database = database
        self.binding = binding
        # A second Postgres estate needs its own table_id prefix, so the tag
        # is configurable. SqlServerAdapter's is a fixed literal only because
        # changing it would rewrite the identity of every table catalogued
        # before estates existed; a new adapter carries no such history.
        if system:
            self.system = system
        elif binding is not None:
            self.system = binding.source_id

    def _connect(self):
        import psycopg

        if self.binding is None or not self.binding.requires_connection:
            raise RuntimeError(
                "PostgresAdapter needs an estate binding to resolve its connection. "
                "Build it with tools.adapters.build_adapter_for_binding(), or declare "
                "a connection_profile on the estate source."
            )
        resolved = self.binding.resolve()
        return psycopg.connect(
            host=resolved.host,
            port=resolved.port,
            user=resolved.user,
            password=resolved.password,
            dbname=resolved.database or self.database,
            connect_timeout=10,
        )

    @staticmethod
    def _split(table_ref: str) -> tuple[str, str]:
        parts = [p for p in table_ref.split(".") if p]
        if len(parts) < 2:
            raise ValueError(
                f"Cannot resolve {table_ref!r} to a schema and table — expected "
                f"'schema.table' or 'system.database.schema.table'."
            )
        return parts[-2], parts[-1]

    # -- Discovery ----------------------------------------------------------

    def discover_tables(self) -> list[dict]:
        with self._connect() as conn:
            return catalog_postgres_tables(conn, database=self.database, system=self.system)

    def discover_pipelines(self) -> list[dict]:
        # Postgres carries no scheduling metadata of its own in this estate,
        # exactly as SQL Server does not — jobs live in a separate artifact
        # source when an estate declares one.
        return []

    def fetch_rows(self, table_id: str, order_by: str) -> Iterator[dict]:
        schema_name, table_name = self._split(table_id)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f'SELECT * FROM "{schema_name}"."{table_name}" ORDER BY "{order_by}"'
            )
            columns = [description[0] for description in cursor.description]
            while True:
                batch = cursor.fetchmany(DEFAULT_BATCH_SIZE)
                if not batch:
                    break
                for row in batch:
                    yield dict(zip(columns, (_json_safe(v) for v in row)))

    # -- Health -------------------------------------------------------------

    def health_check(self) -> dict:
        started = time.monotonic()
        try:
            conn = self._connect()
        except Exception as exc:  # noqa: BLE001
            record = self.record_connection_health(
                "UNREACHABLE", detail=f"connection failed: {type(exc).__name__}: {exc}"
            )
            return {**record, "latency_ms": int((time.monotonic() - started) * 1000)}

        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute("SELECT version()")
                version = str(cursor.fetchone()[0]).split(" on ")[0].strip()
                cursor.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_type = 'BASE TABLE' "
                    "AND table_schema NOT IN ('pg_catalog', 'information_schema')"
                )
                object_count = int(cursor.fetchone()[0])
        except Exception as exc:  # noqa: BLE001
            record = self.record_connection_health(
                "DEGRADED", detail=f"connected, but introspection failed: {exc}"
            )
            return {**record, "latency_ms": int((time.monotonic() - started) * 1000)}

        detail = f"{version}; {object_count} base tables"
        if self.binding is not None and self.binding.requires_connection:
            from tools.secret_resolver import describe_resolution

            profile = self.binding.connection_profile or {}
            detail += (
                f"; credential via "
                f"{describe_resolution(profile.get('password_secret_ref'), env_fallback=profile.get('password_env'))}"
            )

        record = self.record_connection_health("HEALTHY", detail=detail, object_count=object_count)
        return {**record, "latency_ms": int((time.monotonic() - started) * 1000)}

    # -- Reconciliation -----------------------------------------------------

    def get_table_schema(self, table_ref: str) -> list[dict]:
        schema_name, table_name = self._split(table_ref)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema_name, table_name),
            )
            return [
                {"name": n, "data_type": t, "is_nullable": nullable == "YES"}
                for n, t, nullable in cursor.fetchall()
            ]

    def count_rows(self, table_ref: str) -> int:
        schema_name, table_name = self._split(table_ref)
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(f'SELECT count(*) FROM "{schema_name}"."{table_name}"')
            return int(cursor.fetchone()[0])

    def source_facts(self, target: dict) -> dict:
        """Source side of every reconciliation check, in one connection.

        Byte-for-byte the same return shape as SqlServerAdapter.source_facts,
        which is what lets agents/validation/agent.py reconcile this estate
        without knowing it exists.
        """
        self._require(CAPABILITY_RECONCILE, "source_facts")
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

        qualified = f'"{schema_name}"."{table_name}"'
        with self._connect() as conn:
            cursor = conn.cursor()

            cursor.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (schema_name, table_name),
            )
            columns = [r[0] for r in cursor.fetchall()]

            cursor.execute(f"SELECT count(*) FROM {qualified}")
            row_count = int(cursor.fetchone()[0])

            numeric_sum = None
            if numeric_column:
                cursor.execute(f'SELECT sum("{numeric_column}") FROM {qualified}')
                numeric_sum = float(cursor.fetchone()[0] or 0)

            null_count = None
            if null_check_column:
                cursor.execute(
                    f'SELECT count(*) FROM {qualified} WHERE "{null_check_column}" IS NULL'
                )
                null_count = int(cursor.fetchone()[0])

            cursor.execute(
                f'SELECT CAST("{key_column}" AS VARCHAR) FROM {qualified} '
                f'ORDER BY "{key_column}"'
            )
            keys = [r[0] for r in cursor.fetchall()]

        return {
            "columns": columns,
            "row_count": row_count,
            "numeric_sum": numeric_sum,
            "null_count": null_count,
            "keys": keys,
        }

    # -- Transfer -----------------------------------------------------------

    def column_plan(self, table_ref: str, type_rules: dict) -> tuple[list[str], list[str], list[str]]:
        """Which columns can be carried.

        No dialect conversion trick here, unlike SQL Server's `.ToString()`
        for geography/geometry/hierarchyid: a Postgres type either converts
        cleanly to JSON or the pack excludes it. Inventing a conversion that
        has not been proven against real data would be a guess dressed as a
        capability.
        """
        schema_name, table_name = self._split(table_ref)
        scalar_safe = {t.lower() for t in type_rules.get("scalar_safe", set())}

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (schema_name, table_name),
            )
            rows = cursor.fetchall()

        select_exprs: list[str] = []
        carried: list[str] = []
        for name, data_type in rows:
            if str(data_type).lower() in scalar_safe:
                select_exprs.append(f'"{name}"')
                carried.append(name)
        excluded = [name for name, _ in rows if name not in carried]
        return select_exprs, carried, excluded
