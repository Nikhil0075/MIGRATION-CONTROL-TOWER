"""Discovery Agent's toolset (Day 2): inventories the legacy estate into
normalized Table / Pipeline records validated against
contracts/metadata_model.json.

Three introspection functions, one per source in the simulated estate:
  - catalog_sql_server_tables(conn)  — live SQL Server introspection
  - catalog_oracle_corpus(path)      — static Oracle-dialect .sql files
  - catalog_dag_artifacts(path)      — static Airflow-style DAG stubs

Deliberately deterministic, no LLM calls: table/column/PK introspection
and DDL/DAG-metadata parsing are exact, testable operations, not
interpretation — matches master doc §9's AI-vs-deterministic rule
("confirmed database metadata reads" belongs on the deterministic side).
Discovery does not assign PII/sensitivity classification — every emitted
Table record is classification='UNCLASSIFIED'; that judgment belongs to
the Risk & Compliance Agent (§4.2 tool boundary).

DAG stub files are parsed with `ast`, never imported/executed — legacy
artifact content is treated as untrusted data, not code, per §7.2's
"malicious instruction in metadata" fault class.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import re
from pathlib import Path

import jsonschema
import pyodbc

DISCOVERED_BY = "discovery-agent"
REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "metadata_model.json"


def _load_schema(definition: str) -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    node = schema["definitions"][definition]
    # jsonschema needs the $defs context to resolve internal structure; the
    # leaf definitions here are self-contained so this is sufficient.
    return node


_TABLE_SCHEMA = _load_schema("Table")
_PIPELINE_SCHEMA = _load_schema("Pipeline")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _validate(record: dict, schema: dict) -> dict:
    jsonschema.validate(instance=record, schema=schema)
    return record


# --------------------------------------------------------------------------
# SQL Server (WideWorldImporters)
# --------------------------------------------------------------------------


def catalog_sql_server_tables(conn: pyodbc.Connection, database: str = "WideWorldImporters") -> list[dict]:
    """Introspects INFORMATION_SCHEMA + primary keys for every base table."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT TABLE_SCHEMA, TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_SCHEMA, TABLE_NAME
        """
    )
    tables = cursor.fetchall()

    records: list[dict] = []
    for schema_name, table_name in tables:
        columns = _sql_server_columns(cursor, schema_name, table_name)
        pk = _sql_server_primary_key(cursor, schema_name, table_name)
        row_count = _sql_server_row_count_estimate(cursor, schema_name, table_name)
        size_bytes = _sql_server_size_bytes(cursor, schema_name, table_name)

        record = {
            "table_id": f"sqlserver-wwi.{database}.{schema_name}.{table_name}",
            "system": "sqlserver-wwi",
            "database": database,
            "schema": schema_name,
            "table": table_name,
            "classification": "UNCLASSIFIED",
            "row_count_baseline": row_count,
            "size_bytes": size_bytes,
            "primary_key": pk,
            "columns": columns,
            "discovered_at": _now(),
            "discovered_by": DISCOVERED_BY,
        }
        records.append(_validate(record, _TABLE_SCHEMA))
    return records


def _sql_server_columns(cursor, schema_name: str, table_name: str) -> list[dict]:
    """Column name, type, and nullability.

    is_nullable exists so tools/plan_builder.py can pick a MigrationTarget's
    null_check_column from discovered metadata instead of the hardcoded
    'PhoneNumber' that used to live in agents/validation/agent.py. Without it
    there is no non-fabricated way to choose that column for a newly
    onboarded estate — the check would have to be dropped or guessed.
    """
    cursor.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
        """,
        schema_name,
        table_name,
    )
    return [
        {"name": name, "data_type": data_type, "is_nullable": is_nullable == "YES"}
        for name, data_type, is_nullable in cursor.fetchall()
    ]


def _sql_server_primary_key(cursor, schema_name: str, table_name: str) -> list[str]:
    cursor.execute(
        """
        SELECT kcu.COLUMN_NAME
        FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
        JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
          ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
         AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
        WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
          AND tc.TABLE_SCHEMA = ? AND tc.TABLE_NAME = ?
        ORDER BY kcu.ORDINAL_POSITION
        """,
        schema_name,
        table_name,
    )
    return [row[0] for row in cursor.fetchall()]


def _sql_server_row_count_estimate(cursor, schema_name: str, table_name: str) -> int | None:
    cursor.execute(
        """
        SELECT SUM(p.rows)
        FROM sys.partitions p
        JOIN sys.tables t ON t.object_id = p.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ? AND p.index_id IN (0, 1)
        """,
        schema_name,
        table_name,
    )
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _sql_server_size_bytes(cursor, schema_name: str, table_name: str) -> int | None:
    """Bytes this table occupies, from the engine's own allocation metadata.

    Asked of sys.allocation_units rather than measured by reading the
    table: this has to run over every table during discovery, and a
    COUNT-style scan of a large estate would make discovery cost more
    than the migration it is planning. A page is 8 KB, which is a fixed
    property of the storage engine, not a guess.

    `used_pages` rather than `total_pages` — total includes space
    allocated but not yet written, which would overstate what actually
    has to move.

    Returns None rather than 0 when the engine reports nothing, because
    the console distinguishes "no bytes" from "not measured", and a table
    that failed to report is not an empty table.
    """
    cursor.execute(
        """
        SELECT SUM(a.used_pages) * 8192
        FROM sys.allocation_units a
        JOIN sys.partitions p ON p.partition_id = a.container_id
        JOIN sys.tables t ON t.object_id = p.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        WHERE s.name = ? AND t.name = ?
        """,
        schema_name,
        table_name,
    )
    row = cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else None


# --------------------------------------------------------------------------
# Oracle-dialect script corpus (static .sql files, no live Oracle)
# --------------------------------------------------------------------------

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?P<schema>\w+)\.(?P<table>\w+)\s*\((?P<body>.*?)\n\)\s*;",
    re.IGNORECASE | re.DOTALL,
)
_COLUMN_LINE_RE = re.compile(
    r"^\s*(?P<name>[A-Z_][A-Z0-9_]*)\s+(?P<type>[A-Z][A-Z0-9_]*)", re.IGNORECASE
)
# Table-level constraint clauses ("CONSTRAINT PK_x PRIMARY KEY (...)",
# "FOREIGN KEY (...)", etc.) match _COLUMN_LINE_RE's shape too — a real
# bug found on Day 8 when a stray 'CONSTRAINT' pseudo-column started
# showing up in documentation-drift findings for tables that name their
# constraints. Any comma-split line beginning with one of these keywords
# is a constraint clause, never a column definition.
_CONSTRAINT_KEYWORDS = {"CONSTRAINT", "PRIMARY", "FOREIGN", "UNIQUE", "CHECK"}
_PK_INLINE_RE = re.compile(
    r"CONSTRAINT\s+\w+\s+PRIMARY\s+KEY\s*\((?P<cols>[^)]+)\)", re.IGNORECASE
)
_SOURCE_SYSTEM_TAG_RE = re.compile(r"--\s*source_system:\s*(\S+)")


def catalog_oracle_corpus(path: str | Path) -> list[dict]:
    """Parses CREATE TABLE statements out of the static Oracle-dialect corpus."""
    records: list[dict] = []
    for sql_file in sorted(Path(path).glob("*.sql")):
        text = sql_file.read_text(encoding="utf-8")
        tag_match = _SOURCE_SYSTEM_TAG_RE.search(text)
        system = tag_match.group(1) if tag_match else "oracle-corpus"

        for match in _CREATE_TABLE_RE.finditer(text):
            schema_name = match.group("schema")
            table_name = match.group("table")
            body = match.group("body")

            columns = []
            for line in body.split(","):
                col_match = _COLUMN_LINE_RE.match(line)
                if col_match and col_match.group("name").upper() not in _CONSTRAINT_KEYWORDS:
                    columns.append(
                        {
                            "name": col_match.group("name"),
                            "data_type": col_match.group("type"),
                            # Unknown, not assumed-nullable. The body is split on
                            # "," to find column lines, which also splits precision
                            # specs like NUMBER(10,2) — so a trailing "NOT NULL"
                            # can land on the following fragment and reading
                            # nullability from these fragments would be wrong for
                            # exactly the numeric columns that matter most.
                            # Consumers treat None as "not eligible" rather than
                            # guessing; this adapter is assessment-only anyway
                            # (fetch_rows raises NotImplementedError).
                            "is_nullable": None,
                        }
                    )

            pk_match = _PK_INLINE_RE.search(body)
            primary_key = (
                [c.strip() for c in pk_match.group("cols").split(",")] if pk_match else []
            )

            record = {
                "table_id": f"{system}.{schema_name}.{schema_name}.{table_name}",
                "system": system,
                "database": schema_name,
                "schema": schema_name,
                "table": table_name,
                "classification": "UNCLASSIFIED",
                "row_count_baseline": None,
                # A .sql script corpus has no stored bytes to report. Null,
                # never 0: the console distinguishes "not measured" from
                # "measured as empty", and a DDL file is not an empty table.
                "size_bytes": None,
                "primary_key": primary_key,
                "columns": columns,
                "discovered_at": _now(),
                "discovered_by": DISCOVERED_BY,
            }
            records.append(_validate(record, _TABLE_SCHEMA))
    return records


# --------------------------------------------------------------------------
# DAG / scheduling artifacts (static, parsed with ast — never executed)
# --------------------------------------------------------------------------


def catalog_dag_artifacts(path: str | Path) -> list[dict]:
    """Parses DAG_METADATA dict literals out of the static DAG stub files.

    Uses ast.parse + ast.literal_eval so file content is never imported or
    executed — legacy artifacts are untrusted content (§7.2).
    """
    records: list[dict] = []
    for py_file in sorted(Path(path).glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "DAG_METADATA" for t in node.targets):
                continue

            metadata = ast.literal_eval(node.value)
            record = {
                "pipeline_id": metadata["pipeline_id"],
                "source_system": metadata["source_system"],
                "target_system": metadata["target_system"],
                "schedule": metadata["schedule"],
                "owner": metadata["owner"],
                "criticality": metadata["criticality"],
                "code_path": metadata["code_path"],
                "status": metadata["status"],
                "upstream_tables": metadata.get("upstream_tables", []),
                "downstream_tables": metadata.get("downstream_tables", []),
                "discovered_at": _now(),
                "discovered_by": DISCOVERED_BY,
            }
            records.append(_validate(record, _PIPELINE_SCHEMA))
    return records


# --------------------------------------------------------------------------
# PostgreSQL (Day 11 Phase 7 — the second-estate proof, master doc §32.11)
# --------------------------------------------------------------------------


def catalog_postgres_tables(conn, database: str, system: str = "postgres") -> list[dict]:
    """Introspects information_schema for every base table in a Postgres source.

    Structurally the same function as catalog_sql_server_tables above, which
    is the point: the two dialects differ in their catalog SQL and in
    nothing else a caller can observe. Both emit records validated against
    the same Table definition, so Discovery, Lineage, Risk and the Planner
    cannot tell which engine produced them.

    `system` is a parameter here rather than the hardcoded literal the SQL
    Server function carries. That literal predates estate configuration and
    is load-bearing for every table_id already catalogued; a new adapter has
    no such history, so it takes its tag from configuration instead.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_type = 'BASE TABLE'
          AND table_schema NOT IN ('pg_catalog', 'information_schema')
        ORDER BY table_schema, table_name
        """
    )
    tables = cursor.fetchall()

    records: list[dict] = []
    for schema_name, table_name in tables:
        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema_name, table_name),
        )
        columns = [
            {"name": name, "data_type": data_type, "is_nullable": nullable == "YES"}
            for name, data_type, nullable in cursor.fetchall()
        ]

        cursor.execute(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = %s AND tc.table_name = %s
            ORDER BY kcu.ordinal_position
            """,
            (schema_name, table_name),
        )
        primary_key = [r[0] for r in cursor.fetchall()]

        # reltuples is the planner's estimate — cheap and honest for a
        # discovery-time baseline. Reconciliation always issues a real
        # COUNT(*); this figure never feeds a pass/fail decision.
        cursor.execute(
            "SELECT reltuples::bigint FROM pg_class WHERE oid = to_regclass(%s)",
            (f"{schema_name}.{table_name}",),
        )
        estimate = cursor.fetchone()
        row_count = max(0, int(estimate[0])) if estimate and estimate[0] is not None else None

        # Total relation size: the heap plus its indexes and TOAST, which
        # is what the table actually occupies. pg_relation_size alone
        # would report only the heap and understate a heavily indexed
        # table by a wide margin.
        cursor.execute(
            "SELECT pg_total_relation_size(to_regclass(%s))",
            (f"{schema_name}.{table_name}",),
        )
        measured = cursor.fetchone()
        size_bytes = int(measured[0]) if measured and measured[0] is not None else None

        record = {
            "table_id": f"{system}.{database}.{schema_name}.{table_name}",
            "system": system,
            "database": database,
            "schema": schema_name,
            "table": table_name,
            "classification": "UNCLASSIFIED",
            "row_count_baseline": row_count,
            "size_bytes": size_bytes,
            "primary_key": primary_key,
            "columns": columns,
            "discovered_at": _now(),
            "discovered_by": DISCOVERED_BY,
        }
        records.append(_validate(record, _TABLE_SCHEMA))
    return records
