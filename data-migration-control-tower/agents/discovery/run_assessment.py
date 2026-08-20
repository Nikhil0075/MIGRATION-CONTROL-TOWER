#!/usr/bin/env python
"""Assessment-mode run: Discovery -> Lineage -> Risk -> Planner, then
stops — no migration execution, no target write (Day 10 Phase 3, master
doc §32). Driven entirely by a Migration Pack instead of this project's
hardcoded WWI-only constants, so pointing the tower at a new estate/
source means writing a pack.yaml, not new Python.

This is also the "second-estate onboarding test" Phase 3 set out to
prove: the Oracle-dialect corpus is a structurally distinct second
source family (different dialect, no live database at all), and this
same script produces a real assessment for it exactly as it does for
the WWI SQL Server source — see packs/oracle_corpus_v1/pack.yaml.

Usage (from repo root):
    python agents/discovery/run_assessment.py packs/wwi_sqlserver_v1/pack.yaml
    python agents/discovery/run_assessment.py packs/oracle_corpus_v1/pack.yaml
    python agents/discovery/run_assessment.py                # defaults to wwi_sqlserver_v1
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from agents.lineage.agent import build_dependency_graph  # noqa: E402
from agents.orchestrator.run_lifecycle import (  # noqa: E402
    create_run,
    get_run,
    transition_state,
    write_catalog,
)
from agents.planner.agent import propose_plan  # noqa: E402
from agents.risk.agent import classify_estate, verify_pii_access_boundary  # noqa: E402
from tools.pack_loader import binding_for_estate_pack, build_adapter_for_estate, load_pack  # noqa: E402

REPORTS_DIR = REPO_ROOT / "evaluation" / "reports"

# A guaranteed-nonexistent directory — passed to build_dependency_graph
# when a pack declares no lineage_sql_corpus_path of its own, so its
# glob("*.sql") finds nothing rather than silently picking up an
# unrelated corpus via the function's own default.
_NO_CORPUS_PLACEHOLDER = "simulator/source_setup/_no_corpus_for_this_pack"


def _reusable(run_id: str | None) -> str | None:
    """The run from a previous attempt, if it never got anywhere.

    `create_run` happens before `discover_tables`, which is the step that
    actually fails — a source that is down, a credential that expired, a
    connection that times out. The worker then nacks, Pub/Sub redelivers
    the same message, and this function ran again and created ANOTHER
    run. At ten delivery attempts that is ten runs for one command, nine
    of them abandoned in REQUESTED forever. Twenty-nine such runs were
    found on one estate in the live project, and they are not merely
    untidy: the console picks the newest run to show lineage for, so an
    abandoned empty run made the Lineage page render blank.

    Only a run still in REQUESTED is reused. One that got as far as
    DISCOVERED and failed later cannot be replayed through this function
    at all — `transition_state` refuses DISCOVERED -> DISCOVERED, by
    design — so that case still starts a fresh run and leaves the partial
    one as the evidence it is.
    """
    if not run_id:
        return None
    try:
        run = get_run(run_id)
    except KeyError:
        return None
    return run_id if run.get("state") == "REQUESTED" else None


def run_assessment(
    pack_path: str,
    *,
    estate_id: str | None = None,
    on_run_created: Callable[[str], None] | None = None,
    reuse_run_id: str | None = None,
) -> dict:
    pack = load_pack(pack_path)
    binding = binding_for_estate_pack(pack, estate_id)
    adapter = build_adapter_for_estate(pack, estate_id, binding.source_id)

    print(f"[assessment] pack={pack['pack_id']!r} v{pack['version']}, source={adapter.system!r}")

    reused = _reusable(reuse_run_id)
    run_id = reused or create_run(
        pack["pack_id"],
        mode="assessment",
        estate_id=estate_id,
        source_id=binding.source_id,
        pack_id=pack["pack_id"],
    )
    if on_run_created:
        on_run_created(run_id)
    if reused:
        print(f"[assessment] retrying run: {run_id} (mode=assessment)")
    else:
        print(f"[assessment] created run: {run_id} (mode=assessment)")

    tables = adapter.discover_tables()
    pipelines = adapter.discover_pipelines()
    summary = write_catalog(run_id, tables, pipelines)
    print(f"[assessment] discovered {len(tables)} tables, {len(pipelines)} pipelines -> DISCOVERED")

    lineage_summary = build_dependency_graph(
        run_id, oracle_corpus_path=pack.get("lineage_sql_corpus_path") or _NO_CORPUS_PLACEHOLDER
    )
    transition_state(run_id, "ANALYZED")
    print(f"[assessment] lineage: {lineage_summary} -> ANALYZED")

    risk_summary = classify_estate(run_id)
    pii_decision = verify_pii_access_boundary(run_id)
    transition_state(run_id, "RISK_ASSESSED")
    print(f"[assessment] risk: {risk_summary}, raw-PII-read denied={pii_decision['decision'] == 'DENY'} -> RISK_ASSESSED")

    plan = propose_plan(run_id)
    transition_state(run_id, "PLANNED")
    scheduled = [s for s in plan["steps"] if s["scheduled"]]
    # "scheduled" reflects tools/plan_builder.py's fixed logic (today,
    # only the WWI Sales.Customers table ever matches) regardless of
    # mode — assessment mode's real distinction is what happens AFTER
    # this point: nothing. No migration_executor call follows, no
    # target write, no VALIDATING transition.
    print(
        f"[assessment] plan: {len(plan['steps'])} steps, {len(scheduled)} scheduled "
        "-> PLANNED (assessment mode stops here — no migration execution follows)"
    )

    report = {
        "pack_id": pack["pack_id"],
        "pack_version": pack["version"],
        "run_id": run_id,
        "source_system": adapter.system,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tables_discovered": len(tables),
        "pipelines_discovered": len(pipelines),
        "lineage_edges": lineage_summary["edges_written"],
        "risk_findings": {
            "pii_tables": risk_summary.get("pii_tables"),
            "dialect_findings": risk_summary.get("dialect_findings"),
        },
        "raw_pii_read_denied": pii_decision["decision"] == "DENY",
        "plan_steps": len(plan["steps"]),
        "plan_hash": plan["plan_hash"],
    }
    return report


def write_report(report: dict) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"assessment_{report['pack_id']}_{report['run_id']}.md"
    lines = [
        f"# Assessment report — {report['pack_id']} v{report['pack_version']}",
        "",
        f"Generated {report['generated_at']} · run `{report['run_id']}` · source `{report['source_system']}`",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Tables discovered | {report['tables_discovered']} |",
        f"| Pipelines discovered | {report['pipelines_discovered']} |",
        f"| Lineage edges derived | {report['lineage_edges']} |",
        f"| Tables with PII findings | {report['risk_findings']['pii_tables']} |",
        f"| Dialect-incompatibility findings | {report['risk_findings']['dialect_findings']} |",
        f"| Unauthorized raw-PII read denied | {report['raw_pii_read_denied']} |",
        f"| Plan steps | {report['plan_steps']} |",
        f"| Plan hash | `{report['plan_hash'][:16]}...` |",
        "",
        "No migration executed, no target written — assessment mode stops at PLANNED.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def main() -> None:
    pack_path = sys.argv[1] if len(sys.argv) > 1 else "packs/wwi_sqlserver_v1/pack.yaml"
    report = run_assessment(pack_path)
    path = write_report(report)
    print(f"[assessment] report written: {path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
