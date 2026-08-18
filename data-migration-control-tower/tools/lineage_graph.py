"""Lineage Agent's toolset (Day 4, master doc §4): derives Dependency
edges (contracts/metadata_model.json) from two real sources — never
seeded/hand-typed:

  - parse_pipeline_dependencies(): the upstream_tables/downstream_tables
    already captured on each Pipeline record by Discovery's DAG-artifact
    parser (tools/source_catalog.py::catalog_dag_artifacts). High
    confidence (1.0) — these came straight from the DAG's own declared
    metadata.
  - parse_sql_view_dependencies(): a real (if lightweight) SQL parse of
    the Oracle-dialect corpus's CREATE OR REPLACE VIEW statements,
    extracting every table referenced in a FROM/JOIN clause. Lower
    confidence (0.85) — regex-based, not a full SQL AST, so it is
    flagged as such rather than overclaimed.

Deterministic, no LLM call — matches §9's "hypothesizing lineage edges
from code" being the one Gemini-eligible half of lineage work and
"confirmed database metadata reads" being deterministic; the DAG-based
edges here are metadata reads, and even the regex SQL parse is exact
pattern matching, not interpretation, so it stays on the deterministic
side too. A future day may add an LLM-assisted pass for edges regex
can't reach (e.g. lineage embedded in stored-procedure logic) as an
explicit, separately-confidence-scored source.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "contracts" / "metadata_model.json"

DISCOVERED_BY = "lineage-agent"


def _dependency_schema() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return schema["definitions"]["Dependency"]


_DEPENDENCY_SCHEMA = _dependency_schema()


def _validate(record: dict) -> dict:
    jsonschema.validate(instance=record, schema=_DEPENDENCY_SCHEMA)
    return record


def parse_pipeline_dependencies(pipeline_records: list[dict]) -> list[dict]:
    """Derives reads/writes edges from each Pipeline's declared table refs."""
    edges: list[dict] = []
    for pipeline in pipeline_records:
        pipeline_id = pipeline["pipeline_id"]
        for upstream in pipeline.get("upstream_tables", []):
            edges.append(
                _validate(
                    {
                        "from_asset": upstream,
                        "to_asset": pipeline_id,
                        "relationship": "reads",
                        "discovered_by": DISCOVERED_BY,
                        "confidence": 1.0,
                        "source": "dag_reference",
                    }
                )
            )
        for downstream in pipeline.get("downstream_tables", []):
            edges.append(
                _validate(
                    {
                        "from_asset": pipeline_id,
                        "to_asset": downstream,
                        "relationship": "writes",
                        "discovered_by": DISCOVERED_BY,
                        "confidence": 1.0,
                        "source": "dag_reference",
                    }
                )
            )
    return edges


_VIEW_RE = re.compile(
    r"CREATE\s+OR\s+REPLACE\s+VIEW\s+(?P<name>\w+\.\w+)\s+AS\b",
    re.IGNORECASE,
)
_TABLE_REF_RE = re.compile(r"(?:FROM|JOIN)\s+(\w+\.\w+)", re.IGNORECASE)


def parse_sql_view_dependencies(corpus_path: str | Path) -> list[dict]:
    """Regex-parses CREATE OR REPLACE VIEW ... FROM/JOIN clauses into edges."""
    edges: list[dict] = []
    for sql_file in sorted(Path(corpus_path).glob("*.sql")):
        text = sql_file.read_text(encoding="utf-8")
        matches = list(_VIEW_RE.finditer(text))
        for i, match in enumerate(matches):
            view_name = match.group("name")
            block_start = match.end()
            block_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            block = text[block_start:block_end]

            referenced_tables = {
                ref for ref in _TABLE_REF_RE.findall(block) if ref.upper() != view_name.upper()
            }
            for table in sorted(referenced_tables):
                edges.append(
                    _validate(
                        {
                            "from_asset": table,
                            "to_asset": view_name,
                            "relationship": "reads",
                            "discovered_by": DISCOVERED_BY,
                            "confidence": 0.85,
                            "source": "sql_view_parse",
                        }
                    )
                )
    return edges


def find_unresolved_dependencies(table_ids: set[str], dependencies: list[dict]) -> list[dict]:
    """Day 10 (Appendix D, S-07): flags a Dependency edge whose from_asset
    isn't among the run's own catalogued table_ids as an unresolved
    upstream asset — the source table an edge claims to read from was
    never actually discovered.

    Pipeline-to-pipeline and pipeline-to-view edges (to_asset is a
    pipeline_id/view name, never in table_ids) are not flagged; only a
    'reads' edge whose *source* table is missing is a broken dependency —
    a downstream consumer pointing at an asset that doesn't exist in the
    estate. Pure function, no Firestore/network access, so the Planner
    (and this scenario's test) can call it against either a run's real
    lineage.dependencies collection or a synthetic list.
    """
    unresolved = []
    for edge in dependencies:
        if edge["relationship"] != "reads":
            continue
        if edge["from_asset"] not in table_ids:
            unresolved.append(edge)
    return unresolved
