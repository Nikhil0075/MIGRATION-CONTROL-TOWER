"""Lineage Agent (Day 4, master doc §4).

Responsibility: "Build dependency graph — parse reads/writes and
orchestration edges; produce upstream/downstream impact graph." Tool set
(§4.2): metadata + code read, graph write — no raw data, no cutover, no
permission changes.

The actual edge derivation is deterministic (tools/lineage_graph.py);
this agent orchestrates those calls against a run's already-discovered
catalog/pipelines and persists the resulting Dependency edges.
"""

from __future__ import annotations

import logging
import os
import uuid

from tools.firestore_client import get_client
from tools.lineage_graph import parse_pipeline_dependencies, parse_sql_view_dependencies

logger = logging.getLogger("lineage_agent")

AGENT_ID = "lineage-agent"
AGENT_POLICY_KEY = "lineage"
AGENT_VERSION = "2.0.0"

RUN_COLLECTION = "migration_runs"


def build_dependency_graph(run_id: str, oracle_corpus_path: str | None = None) -> dict:
    """Tool: derives and persists Dependency edges for a run.

    Combines DAG-declared table references (from the run's own Pipeline
    records — high confidence) with a regex SQL parse of the Oracle
    corpus's view definitions (lower confidence, explicitly labeled).
    Neither source is hand-typed; both come from artifacts already on
    disk/in Firestore.
    """
    client = get_client()
    run_ref = client.collection(RUN_COLLECTION).document(run_id)

    pipeline_docs = list(run_ref.collection("pipelines").stream())
    pipelines = [d.to_dict() for d in pipeline_docs]

    corpus_path = oracle_corpus_path or os.environ.get(
        "ORACLE_CORPUS_PATH", "simulator/source_setup/oracle_dialect_corpus"
    )

    edges = parse_pipeline_dependencies(pipelines) + parse_sql_view_dependencies(corpus_path)

    from tools.model_gateway import LineageReasoning, generate_structured

    catalog = [d.to_dict() for d in run_ref.collection("catalog").stream()]
    evidence_refs = [str(item.get("table_id")) for item in catalog if item.get("table_id")]
    evidence_refs.extend(str(item.get("pipeline_id")) for item in pipelines if item.get("pipeline_id"))
    reasoning = generate_structured(
        run_id=run_id,
        agent_id=AGENT_ID,
        capability="lineage.reason.candidates",
        stage="LINEAGE",
        instruction=(
            "Identify only plausible missing lineage candidates from the supplied catalog, pipelines and parsed edges. "
            "Candidates are advisory and must cite supplied assets; deterministic parsed edges remain authoritative."
        ),
        payload={
            "assets": [
                {"evidence_ref": item.get("table_id"), "schema": item.get("schema"), "table": item.get("table")}
                for item in catalog
            ],
            "pipelines": [
                {"evidence_ref": item.get("pipeline_id"), "reads": item.get("reads"), "writes": item.get("writes")}
                for item in pipelines
            ],
            "deterministic_edges": edges,
        },
        output_schema=LineageReasoning,
        evidence_refs=evidence_refs,
        tool_calls=[
            {"tool": "parse_pipeline_dependencies", "status": "COMPLETED"},
            {"tool": "parse_sql_view_dependencies", "status": "COMPLETED"},
        ],
        required=True,
        agent_version=AGENT_VERSION,
        generated_artifact_refs=["agent_artifacts/lineage_reasoning", "ai_lineage_candidates/*"],
    )

    batch = client.batch()
    for edge in edges:
        batch.set(run_ref.collection("dependencies").document(str(uuid.uuid4())), edge)
    if reasoning is not None:
        batch.set(run_ref.collection("agent_artifacts").document("lineage_reasoning"), reasoning.model_dump())
        for candidate in reasoning.candidates:
            batch.set(
                run_ref.collection("ai_lineage_candidates").document(str(uuid.uuid4())),
                {**candidate.model_dump(), "status": "PROPOSED", "created_by": AGENT_ID},
            )
    batch.commit()

    summary = {
        "run_id": run_id,
        "edges_written": len(edges),
        "dag_reference_edges": sum(1 for e in edges if e["source"] == "dag_reference"),
        "sql_view_parse_edges": sum(1 for e in edges if e["source"] == "sql_view_parse"),
    }
    logger.info("build_dependency_graph: %s", summary)
    return summary


try:
    from google.adk.agents import Agent  # type: ignore

    lineage_agent = Agent(
        name=AGENT_ID.replace("-", "_"),
        model="gemini-3.7-flash",
        description=(
            "Derives the dependency graph for a migration run from DAG "
            "table references and parsed SQL view definitions. Never "
            "seeds or hand-writes edges."
        ),
        instruction=(
            "You build the lineage graph for a discovered legacy estate. "
            "Use build_dependency_graph to derive Dependency edges from "
            "the run's pipelines and the Oracle-dialect SQL corpus. "
            "Report edge counts by source so a reviewer can see what was "
            "derived from DAG metadata versus parsed SQL."
        ),
        tools=[build_dependency_graph],
    )
    AGENT_FRAMEWORK = "google-adk"
except ImportError as exc:  # pragma: no cover
    logger.warning(
        "google-adk not importable (%s); using Rung-2 direct tool-call fallback.", exc
    )
    lineage_agent = None
    AGENT_FRAMEWORK = "direct-fallback"
