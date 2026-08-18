"""hello-agent — Day 1 foundation proof.

Tools:
  - list_source_tables(): reads INFORMATION_SCHEMA from the local/
    simulated on-prem SQL Server via tools/sqlserver_client.py.
  - write_firestore_row(note): writes a bootstrap-check row to Firestore
    via tools/firestore_client.py.

Framework: uses Google ADK if importable; otherwise falls back to a
minimal callable-tool wrapper with an identical interface. This is a
documented Rung-2 substitution (master doc §19) — not a silent
downgrade. See infrastructure/README.md.

This agent does no reasoning yet; it exists purely to prove the plumbing
end to end (Day 1 exit condition). The Discovery Agent (Day 2, see
agents/discovery/) is the first agent that actually reasons over the
estate.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from tools.firestore_client import write_document
from tools.sqlserver_client import get_connection, list_table_names

logger = logging.getLogger("hello_agent")

AGENT_ID = "hello-agent"
AGENT_VERSION = "0.1.0"


def list_source_tables() -> list[str]:
    """Tool: returns fully-qualified table names from the legacy source."""
    conn = get_connection()
    try:
        return list_table_names(conn)
    finally:
        conn.close()


def write_firestore_row(note: str) -> str:
    """Tool: writes a single bootstrap-check row to Firestore.

    Returns the Firestore document path so the caller can confirm the
    write without a second round-trip.
    """
    doc_id = str(uuid.uuid4())
    path = write_document(
        collection="_bootstrap_check",
        doc_id=doc_id,
        data={
            "agent_id": AGENT_ID,
            "agent_version": AGENT_VERSION,
            "note": note,
            "written_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    )
    logger.info("wrote bootstrap-check row at %s", path)
    return path


try:
    from google.adk.agents import Agent  # type: ignore

    hello_agent = Agent(
        # ADK requires a Python-identifier node name; AGENT_ID (with the
        # hyphen) remains the registry/logging identifier (master doc §20).
        name=AGENT_ID.replace("-", "_"),
        model="gemini-3.5-flash",
        description=(
            "Day 1 foundation agent: proves source-metadata read and "
            "Firestore state-plane write, no cutover/migration authority."
        ),
        instruction=(
            "You are a bootstrap/connectivity-check agent for a data "
            "migration control tower. You may call list_source_tables to "
            "read the legacy estate's table catalog, and write_firestore_row "
            "to record a bootstrap-check event. You have no other "
            "capabilities and must never claim to perform a migration."
        ),
        tools=[list_source_tables, write_firestore_row],
    )
    AGENT_FRAMEWORK = "google-adk"
except ImportError as exc:  # pragma: no cover — exercised only when google-adk is absent
    logger.warning(
        "google-adk not importable (%s); using Rung-2 direct tool-call "
        "fallback (see infrastructure/README.md).",
        exc,
    )
    hello_agent = None
    AGENT_FRAMEWORK = "direct-fallback"
