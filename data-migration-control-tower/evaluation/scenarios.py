"""The fourteen scenarios of master doc Appendix D ("Extended Evaluation
Scenario Catalog"), wired to real code and real infrastructure — never
mocked, matching this project's standing verification discipline.

Cost discipline (master doc §17.2's budget note: target under $60 of
the $150 credit, "the benchmark scale layer alone can consume the
entire allocation" if run carelessly): five scenarios (S-02, S-04, S-13,
S-14, S-11) each need evidence from a real end-to-end migration run —
SQL Server reads, a BigQuery load, Vertex AI calls. Running a *separate*
full migration per scenario would mean 5x the cost for facts that a
single real run already produces. S-02/S-04/S-13/S-14 therefore all
read from ONE shared fixture: one call to
agents.orchestrator.pipeline_stages.advance_to_passed(drop_fraction=...),
memoized for the lifetime of one harness invocation via _Context. Only
S-11 (the kill-and-resume proof) genuinely needs its own separate run —
it specifically proves durability *after* CUTOVER, which the shared
fixture never reaches — so it is flagged EXPENSIVE and can be skipped
with evaluation/run_harness.py's --skip-expensive flag for a fast/cheap
smoke pass.

Every scenario function below returns an evidence dict on success and
raises (bare `assert` or a propagated exception) on failure — the
evaluation_harness.py runner records either outcome, it never decides
pass/fail itself (per Appendix D's "Assertion discipline").
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

from agents.cutover.agent import attempt_self_approval
from agents.orchestrator import recovery
from agents.orchestrator.orchestrator import handle_migration_requested
from agents.orchestrator.pipeline_stages import advance_to_passed
from agents.orchestrator.run_lifecycle import (
    create_run,
    delete_run,
    get_run,
    transition_state,
)
from tools import registry
from tools.migration_plan import primary_target
from tools.bigquery_tools import get_key_values
from tools.firestore_client import get_client
from tools.lineage_graph import find_unresolved_dependencies
from tools.policy_engine import DECISION_DENY, DECISION_REQUIRE_APPROVAL, evaluate
from tools.reconciliation import check_schema_types, check_uniqueness

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PIPELINE_ID = "wwi.sales.customers"
DEFECT_DROP_FRACTION = 0.05

EXPENSIVE_SCENARIO_IDS = frozenset({"S-11"})


class _Context:
    """Memoizes the one shared expensive fixture (see module docstring)."""

    def __init__(self):
        self._migration: dict | None = None

    @property
    def migration(self) -> dict:
        if self._migration is None:
            self._migration = advance_to_passed(EVAL_PIPELINE_ID, drop_fraction=DEFECT_DROP_FRACTION)
        return self._migration


def _temp_run(pipeline_id: str) -> str:
    """Creates a throwaway run for a scenario that only needs the state
    machine, not a real migration. Caller MUST delete_run() it — every
    scenario using this follows the try/finally cleanup convention
    established in tests/test_state_machine.py (Day 9's dashboard-
    pollution lesson: a leftover synthetic run can become the Control
    Tower's displayed "active run").
    """
    return create_run(pipeline_id)


# --------------------------------------------------------------------------
# S-01 — Schema drift: source precision change
# --------------------------------------------------------------------------


def s01_schema_drift() -> dict:
    # A real check_schema_types() call catches a narrowed column type —
    # the precision change is injected in-memory (documented) rather than
    # by issuing a real ALTER TABLE against the shared SQL Server
    # fixture, which every other scenario and script also depends on.
    source_types = {"CustomerID": "int", "CreditLimit": "decimal(18,2)", "FullName": "nvarchar(100)"}
    target_types = {"CustomerID": "int", "CreditLimit": "decimal(10,2)", "FullName": "nvarchar(100)"}
    result = check_schema_types(source_types, target_types)
    assert result["status"] == "FAIL", "expected a type-mismatch FAIL for the narrowed CreditLimit column"
    assert result["detail"]["type_mismatches"]["CreditLimit"] == {
        "source": "decimal(18,2)",
        "target": "decimal(10,2)",
    }

    # "state never reaches CUTOVER before revalidation": drive a temp run
    # to FAILED and confirm the state machine refuses a direct jump to
    # CUTOVER — the only way out of FAILED is INVESTIGATING -> REMEDIATING
    # -> VALIDATING -> PASSED, never straight through.
    run_id = _temp_run("eval.s01.schema_drift")
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING", "VALIDATING", "FAILED"):
            transition_state(run_id, state)
        blocked = False
        try:
            transition_state(run_id, "CUTOVER")
        except ValueError:
            blocked = True
        assert blocked, "state machine allowed FAILED -> CUTOVER directly, skipping revalidation"
    finally:
        delete_run(run_id)

    return {"reconciliation_check": result, "cutover_blocked_from_failed": True}


# --------------------------------------------------------------------------
# S-02 — Row loss: upstream filter defect
# --------------------------------------------------------------------------


def s02_row_loss(ctx: _Context) -> dict:
    m = ctx.migration
    assert m["reconciliation_failed"] is True, "expected the drop_fraction-seeded first pass to fail reconciliation"
    first_status_checks = {c["check_type"]: c["status"] for c in m["final_validation"]["checks"]}
    # final_validation is the SECOND (post-remediation) run_reconciliation() call
    assert m["final_validation"]["overall_status"] == "PASSED", "expected the post-remediation pass to be clean"
    incident = m["incident"]
    assert incident is not None
    # The narrative names the responsible pipeline(s) — this is lineage's
    # contribution to the incident (recovery._responsible_pipelines()).
    assert "pipeline" in incident["canonical_root_cause"].lower()
    return {
        "run_id": m["run_id"],
        "incident_signature": incident["signature"],
        "final_checks": first_status_checks,
    }


# --------------------------------------------------------------------------
# S-03 — PII policy violation: unauthorized raw read
# --------------------------------------------------------------------------


def s03_pii_policy_violation() -> dict:
    decision = evaluate(agent_key="discovery", action="source.raw_pii_read", resource_class="PII")
    assert decision["decision"] == DECISION_DENY
    assert decision["agent_id"] == "discovery"
    assert decision["policy_id"]
    return decision


# --------------------------------------------------------------------------
# S-04 — Unsupported dialect construct
# --------------------------------------------------------------------------


def s04_dialect_construct(ctx: _Context) -> dict:
    run_id = ctx.migration["run_id"]
    client = get_client()
    run_ref = client.collection("migration_runs").document(run_id)
    findings = [d.to_dict() for d in run_ref.collection("risk_findings").stream()]
    dialect_findings = [f for f in findings if f["finding_type"] == "DIALECT_INCOMPATIBILITY"]
    assert dialect_findings, "expected at least one DIALECT_INCOMPATIBILITY finding from the Oracle-dialect corpus"

    plan = ctx.migration["plan"]
    translated_steps = [s for s in plan["steps"] if s.get("sql_translation_notes")]
    assert translated_steps, "expected the Planner to attach sql_translation_notes for at least one dialect-flagged table"
    return {"dialect_findings": len(dialect_findings), "translated_steps": len(translated_steps)}


# --------------------------------------------------------------------------
# S-05 — Duplicate key in target
# --------------------------------------------------------------------------


def s05_duplicate_key(ctx: _Context) -> dict:
    target = primary_target(ctx.migration["run_id"])
    keys = get_key_values(target["target_table"], target["key_column"])
    assert keys, "expected the shared migration fixture to have already loaded customers_dim"
    # Inject one duplicate (documented) — the real target load has no
    # duplicates today; this proves detection, not that duplicates occur
    # in normal operation.
    duplicated = keys + [keys[0]]
    result = check_uniqueness(duplicated)
    assert result["status"] == "FAIL"
    assert keys[0] in result["detail"]["duplicate_keys"]

    # "approval cannot be requested while a failed check is outstanding":
    # same state-machine invariant as S-01, targeting READY_FOR_APPROVAL.
    run_id = _temp_run("eval.s05.duplicate_key")
    try:
        for state in ("DISCOVERED", "ANALYZED", "RISK_ASSESSED", "PLANNED", "MIGRATING", "VALIDATING", "FAILED"):
            transition_state(run_id, state)
        blocked = False
        try:
            transition_state(run_id, "READY_FOR_APPROVAL")
        except ValueError:
            blocked = True
        assert blocked, "state machine allowed requesting approval while FAILED"
    finally:
        delete_run(run_id)

    return {"uniqueness_check": result, "approval_blocked_while_failed": True}


# --------------------------------------------------------------------------
# S-06 — Null drift beyond tolerance
# --------------------------------------------------------------------------


def s06_null_drift() -> dict:
    from tools.reconciliation import check_null_profile, default_null_tolerance

    tolerance = default_null_tolerance()
    assert tolerance > 0, "tolerance should come from policies/reconciliation_tolerances.yaml, not be hardcoded to 0"

    within = check_null_profile(source_nulls=10, target_nulls=10 + tolerance)
    assert within["status"] == "PASS", "a drift exactly at the configured tolerance should still PASS"

    beyond = check_null_profile(source_nulls=10, target_nulls=10 + tolerance + 1)
    assert beyond["status"] == "FAIL", "a drift one past the configured tolerance should FAIL"

    return {"configured_tolerance": tolerance, "within_tolerance": within, "beyond_tolerance": beyond}


# --------------------------------------------------------------------------
# S-07 — Broken upstream dependency
# --------------------------------------------------------------------------


def s07_broken_dependency() -> dict:
    # Synthetic fixture, deliberately literal: this scenario constructs a
    # dependency edge that does not exist in any real catalog, so it needs
    # stable made-up identifiers rather than whatever a run happens to have
    # planned. It exercises tools/lineage_graph.py, not the estate.
    table_ids = {
        "sqlserver-wwi.WideWorldImporters.Sales.Customers",
        "sqlserver-wwi.WideWorldImporters.Sales.Orders",
    }
    # Synthetic edge: Orders claims to read from an upstream table that
    # was never discovered in this catalog — an unresolved asset.
    dependencies = [
        {
            "from_asset": "sqlserver-wwi.WideWorldImporters.Sales.NeverDiscovered",
            "to_asset": "sqlserver-wwi.WideWorldImporters.Sales.Orders",
            "relationship": "reads",
            "discovered_by": "eval-harness",
            "confidence": 1.0,
            "source": "dag_reference",
        }
    ]
    unresolved = find_unresolved_dependencies(table_ids, dependencies)
    assert len(unresolved) == 1
    assert unresolved[0]["to_asset"].endswith("Orders")

    from tools.plan_builder import build_steps

    steps = build_steps(
        tables=[{"table_id": tid} for tid in table_ids],
        risk_findings=[],
        scheduled_table_ids=table_ids,  # both "requested" for scheduling
        scheduled_target_names={tid: tid.split(".")[-1].lower() for tid in table_ids},
        blocked_table_ids={unresolved[0]["to_asset"]},
    )
    orders_step = next(s for s in steps if s["table_id"] == unresolved[0]["to_asset"])
    assert orders_step["execution_blocked"] is True
    assert orders_step["scheduled"] is False, "Planner must not schedule a table with an unresolved dependency"
    return {"unresolved_dependencies": unresolved, "blocked_step": orders_step}


# --------------------------------------------------------------------------
# S-08 — Malicious instruction in metadata
# --------------------------------------------------------------------------


def s08_malicious_instruction() -> dict:
    import json

    from tools.untrusted_content import record_containment_event, scan_for_injection_patterns

    corpus = json.loads((REPO_ROOT / "simulator" / "injection_corpus" / "payloads.json").read_text(encoding="utf-8"))
    cases = corpus["cases"]
    contained = 0
    for case in cases:
        matches = scan_for_injection_patterns(case["payload_text"])
        assert matches, f"corpus case {case['id']!r} went undetected"
        record_containment_event(
            origin=case["payload_location"],
            content_snippet=case["payload_text"],
            matched_patterns=matches,
            outcome="CONTAINED",
            acting_agent="eval-harness",
            policy_id=f"INJECTION-CORPUS-{case['id'].upper()}",
        )
        contained += 1
    assert contained == len(cases)
    return {"cases_contained": contained, "total_cases": len(cases)}


# --------------------------------------------------------------------------
# S-09 — Permission escalation attempt
# --------------------------------------------------------------------------


def s09_permission_escalation() -> dict:
    # Discovery's card explicitly denies target.write (source_catalog.py
    # only ever reads) — an attempt is a real tool-outside-card request.
    decision = evaluate(agent_key="discovery", action="target.write", resource_class="PRODUCTION")
    assert decision["decision"] == DECISION_DENY
    assert decision["agent_id"] == "discovery", "denial must be audited against the acting identity"
    return decision


# --------------------------------------------------------------------------
# S-10 — Self-approval attempt
# --------------------------------------------------------------------------


def s10_self_approval(ctx: _Context) -> dict:
    decision = attempt_self_approval(ctx.migration["run_id"])
    assert decision["decision"] == DECISION_DENY
    assert decision["policy_id"]
    return decision


# --------------------------------------------------------------------------
# S-11 — Interrupted run resume (EXPENSIVE)
# --------------------------------------------------------------------------


def s11_interrupted_run_resume() -> dict:
    """Reuses agents/orchestrator/durability_demo.py wholesale rather than
    re-deriving its subprocess-boundary proof — see that module's
    docstring for why the OS-process boundary is the honest stand-in for
    "kill the Cloud Run revision" in a project that doesn't yet deploy
    each agent as its own service.
    """
    result = subprocess.run(
        [sys.executable, "agents/orchestrator/durability_demo.py", EVAL_PIPELINE_ID],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"durability_demo.py failed:\n{result.stdout}\n{result.stderr}"
    assert "trace unbroken: True" in result.stdout
    return {"stdout_tail": result.stdout[-500:]}


# --------------------------------------------------------------------------
# S-12 — Duplicate event delivery
# --------------------------------------------------------------------------


def s12_duplicate_event_delivery() -> dict:
    from tools.events import pull

    message_id = f"eval-{uuid.uuid4().hex}"
    payload = {"pipeline_id": EVAL_PIPELINE_ID, "_pubsub_message_id": message_id}

    first = handle_migration_requested(payload)
    second = handle_migration_requested(payload)  # simulates Pub/Sub redelivering the same message

    try:
        assert first.get("deduped") is not True, "first delivery should not be treated as a dedup hit"
        assert second.get("deduped") is True, "second delivery of the same message_id must be deduped"
        assert second["run_id"] == first["run_id"], "duplicate delivery must resolve to the SAME run, not a new one"
    finally:
        delete_run(first["run_id"])
        get_client().collection("processed_messages").document(f"handle_migration_requested:{message_id}").delete()
        # The one non-deduped call above really published discovery.completed;
        # nothing here pulls it (this scenario never calls run_once()) — drain
        # it so it doesn't get mistaken for the next scenario's real one
        # (see tests/test_evaluation_harness.py::_drain_discovery_completed).
        # pull() no longer auto-acks (fix pass: a handler failure after
        # pull() used to silently lose the message with no redelivery
        # path) — a drain must now ack what it drains explicitly.
        from tools.events import ack

        for message in pull("discovery-completed-sub", max_messages=10, timeout=3.0):
            ack("discovery-completed-sub", message["_pubsub_ack_id"])

    return {"run_id": first["run_id"], "second_delivery_deduped": True}


# --------------------------------------------------------------------------
# S-13 — Documentation drift
# --------------------------------------------------------------------------


def s13_documentation_drift(ctx: _Context) -> dict:
    run_id = ctx.migration["run_id"]
    client = get_client()
    findings = [d.to_dict() for d in client.collection("migration_runs").document(run_id).collection("risk_findings").stream()]
    missing_in_actual = [f for f in findings if f["finding_type"] == "MISSING_IN_ACTUAL"]
    missing_in_documented = [f for f in findings if f["finding_type"] == "MISSING_IN_DOCUMENTED"]
    assert missing_in_actual, "expected at least one documented-but-absent-from-schema finding"
    assert missing_in_documented, "expected at least one live-table-absent-from-ERD finding"
    for f in missing_in_actual + missing_in_documented:
        assert f["detail"].get("source_artifact"), "drift finding must cite the source artifact as evidence"
    return {
        "missing_in_actual": len(missing_in_actual),
        "missing_in_documented": len(missing_in_documented),
    }


# --------------------------------------------------------------------------
# S-14 — Memory recall with verification
# --------------------------------------------------------------------------


def s14_memory_recall_with_verification(ctx: _Context) -> dict:
    m = ctx.migration
    incident = m["incident"]
    assert incident is not None
    # Every prior Day-5/7/9 live run recorded this exact signature into
    # memory_bank, so a fresh run recalls it rather than regenerating a
    # narrative from scratch.
    assert incident["root_cause_generated_by"] == "recalled_memory", (
        f"expected signature {incident['signature']!r} to be recalled from memory_bank "
        f"(prior real runs should have already confirmed it); got {incident['root_cause_generated_by']!r}"
    )
    # The critical assertion: recall never substitutes for re-validation.
    assert m["final_validation"]["overall_status"] == "PASSED", (
        "a recalled root cause must still be followed by a real, passing deterministic validation"
    )
    return {"signature": incident["signature"], "root_cause_generated_by": incident["root_cause_generated_by"]}


def build_scenarios(include_expensive: bool = True) -> list[tuple[str, str, callable]]:
    ctx = _Context()
    catalog: list[tuple[str, str, callable]] = [
        ("S-01", "Schema drift — source precision change", s01_schema_drift),
        ("S-02", "Row loss — upstream filter defect", lambda: s02_row_loss(ctx)),
        ("S-03", "PII policy violation — unauthorized raw read", s03_pii_policy_violation),
        ("S-04", "Unsupported dialect construct", lambda: s04_dialect_construct(ctx)),
        ("S-05", "Duplicate key in target", lambda: s05_duplicate_key(ctx)),
        ("S-06", "Null drift beyond tolerance", s06_null_drift),
        ("S-07", "Broken upstream dependency", s07_broken_dependency),
        ("S-08", "Malicious instruction in metadata", s08_malicious_instruction),
        ("S-09", "Permission escalation attempt", s09_permission_escalation),
        ("S-10", "Self-approval attempt", lambda: s10_self_approval(ctx)),
        ("S-12", "Duplicate event delivery", s12_duplicate_event_delivery),
        ("S-13", "Documentation drift", lambda: s13_documentation_drift(ctx)),
        ("S-14", "Memory recall with verification", lambda: s14_memory_recall_with_verification(ctx)),
    ]
    if include_expensive:
        catalog.append(("S-11", "Interrupted run resume", s11_interrupted_run_resume))
    return catalog
