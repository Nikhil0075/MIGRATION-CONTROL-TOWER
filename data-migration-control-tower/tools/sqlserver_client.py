"""Shared SQL Server connection helper for the legacy source estate.

Used directly by the Day 1 hello-agent (simple table listing) and by
tools/source_catalog.py's catalog_sql_server_tables() on Day 2 (full
schema/column/PK introspection). Kept separate from firestore_client.py
because source-plane and state-plane connectivity are different
concerns with different failure modes and, eventually, different
identities in the policy model (master doc §5.1).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pyodbc

if TYPE_CHECKING:  # import-time cycle otherwise: secrets -> (nothing), but
    # connection_context -> secrets -> ... and adapters -> this module.
    # The hint is only needed for type checkers.
    from tools.connection_context import SourceBinding  # noqa: F401
    from tools.secret_resolver import ResolvedConnection


# Preference order when SQLSERVER_ODBC_DRIVER isn't set explicitly. The
# legacy "SQL Server" driver ships in-box on Windows and works fine for
# introspection queries, so dev machines without the msodbcsql18 package
# installed still work — this is a Rung-2-style fallback, not a hard
# requirement on the newest driver.
_DRIVER_PREFERENCE = [
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server",
]


def _resolve_driver() -> str:
    configured = os.environ.get("SQLSERVER_ODBC_DRIVER")
    if configured:
        return configured
    available = set(pyodbc.drivers())
    for candidate in _DRIVER_PREFERENCE:
        if candidate in available:
            return candidate
    raise RuntimeError(
        "No SQL Server ODBC driver found. Install 'ODBC Driver 18 for SQL "
        "Server' (recommended) or set SQLSERVER_ODBC_DRIVER to a driver "
        f"name from: {sorted(available)}"
    )


def _connect(driver: str, host, port, database: str, user: str, password: str) -> pyodbc.Connection:
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={host},{port};"
        f"DATABASE={database};"
        f"UID={user};PWD={password};"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, timeout=10)


def get_connection(
    database: str | None = None,
    *,
    profile: "ResolvedConnection | None" = None,
) -> pyodbc.Connection:
    """Opens a connection to a SQL Server source.

    Two paths, deliberately:

    `profile` supplied — the per-estate path (Day 11 Phase 1). The caller
    resolved a SourceBinding's ConnectionProfile into live parameters, so
    two estates can be connected from one process without either seeing
    the other's credentials. This is what every new call site should use.

    `profile` omitted — the original process-global environment path,
    preserved verbatim. Every existing caller (the Day 1 hello-agent,
    tools/source_catalog.py, the test suite's reachability probes) keeps
    working unchanged, and it remains the documented local-dev path for a
    single-estate checkout. It is not deprecated by accident: removing it
    would make a plain `python agents/discovery/run_discovery.py` require
    an estate document, which is a worse developer experience for no
    safety gain in a single-estate repo.
    """
    driver = _resolve_driver()

    if profile is not None:
        return _connect(
            driver,
            profile.host,
            profile.port,
            database or profile.database,
            profile.user,
            profile.password,
        )

    return _connect(
        driver,
        os.environ.get("SQLSERVER_HOST", "localhost"),
        os.environ.get("SQLSERVER_PORT", "1433"),
        database or os.environ.get("SQLSERVER_DB", "WideWorldImporters"),
        os.environ.get("SQLSERVER_USER", "sa"),
        os.environ["SQLSERVER_PASSWORD"],
    )


def get_connection_for(binding: "SourceBinding", database: str | None = None) -> pyodbc.Connection:
    """Opens a connection for a SourceBinding (tools/connection_context.py).

    The credential is resolved here and never returned to the caller — the
    ResolvedConnection stays local to this frame, so a traceback from a
    failed connect does not carry the password up the stack.
    """
    return get_connection(database=database, profile=binding.resolve())


def list_table_names(conn: pyodbc.Connection) -> list[str]:
    """Returns fully-qualified table names (schema.table) for the metadata tool."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT TABLE_SCHEMA, TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_SCHEMA, TABLE_NAME
        """
    )
    return [f"{schema}.{name}" for schema, name in cursor.fetchall()]
