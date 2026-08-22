"""Migration-run lifecycle and deterministic state machine in Firestore
(master doc §8, §9).

State-transition legality is deterministic code, not agent code — per
§9's rule, "state-machine transition rules" belong on the deterministic
side; an agent (or a prompt-injected table comment) proposing an illegal
transition gets a ValueError, never a silent state jump.

Firestore layout:
    migration_runs/{run_id}                          run document
    migration_runs/{run_id}/catalog/{table_id}         Table records (Discovery)
    migration_runs/{run_id}/pipelines/{pipeline_id}    Pipeline records (Discovery)
    migration_runs/{run_id}/dependencies/{edge_id}     Dependency records (Lineage)
    migration_runs/{run_id}/risk_findings/{finding_id} RiskFinding records (Risk)
    migration_runs/{run_id}/policy_decisions/{seq}     PolicyDecision records (policy engine)
    migration_runs/{run_id}/seeded_defects/{defect_id} fault-injection manifest (simulator)
    migration_runs/{run_id}/reconciliation/{check_id}  ValidationResult records (Validation)
"""

from __future__ import annotations

import datetime as dt
import re
import uuid

from google.cloud import firestore
from google.cloud.firestore_v1.field_path import FieldPath

from tools.connection_context import DEFAULT_ESTATE_ID
from tools.firestore_client import get_client

# Full state list from master doc §8.
STATES = [
    "REQUESTED",
    "DISCOVERED",
    "ANALYZED",
    "RISK_ASSESSED",
    "PLANNED",
    "MIGRATING",
    "VALIDATING",
    "FAILED",
    "INVESTIGATING",
    "REMEDIATING",
    "PASSED",
    "READY_FOR_APPROVAL",
    "APPROVED",
    "CUTOVER",
    "MONITORING",
    "COMPLETE",
]

# The canonical state machine, exactly as diagrammed in §8:
#   DISCOVERED -> ANALYZED -> RISK_ASSESSED -> PLANNED -> MIGRATING
#   -> VALIDATING -> FAILED -> INVESTIGATING -> REMEDIATING -> VALIDATING
#   -> PASSED -> READY_FOR_APPROVAL -> APPROVED -> CUTOVER -> MONITORING
#   -> COMPLETE
_CANONICAL_TRANSITIONS: dict[str, set[str]] = {
    "REQUESTED": {"DISCOVERED"},
    "DISCOVERED": {"ANALYZED"},
    "ANALYZED": {"RISK_ASSESSED"},
    "RISK_ASSESSED": {"PLANNED"},
    "PLANNED": {"MIGRATING"},
    "MIGRATING": {"VALIDATING"},
    "VALIDATING": {"FAILED", "PASSED"},
    "FAILED": {"INVESTIGATING"},
    "INVESTIGATING": {"REMEDIATING"},
    "REMEDIATING": {"VALIDATING"},
    "PASSED": {"READY_FOR_APPROVAL"},
    "READY_FOR_APPROVAL": {"APPROVED"},
    "APPROVED": {"CUTOVER"},
    "CUTOVER": {"MONITORING"},
    "MONITORING": {"COMPLETE"},
    "COMPLETE": set(),
}

# Temporary shortcuts, honestly declared rather than hidden (§19's
# degradation-ladder ethos). Empty as of Day 5: the Migration Planner
# (agents/planner/) and migration executor (tools/migration_executor.py)
# now exist, so RISK_ASSESSED -> PLANNED -> MIGRATING -> VALIDATING is
# the only legal path — the shortcut that used to skip straight from
# RISK_ASSESSED to VALIDATING has been removed, not just left unused.
_PROVISIONAL_TRANSITIONS: dict[str, set[str]] = {}


def _allowed_next_states(current_state: str) -> set[str]:
    return _CANONICAL_TRANSITIONS.get(current_state, set()) | _PROVISIONAL_TRANSITIONS.get(
        current_state, set()
    )


RUN_COLLECTION = "migration_runs"


def _safe_doc_id(raw_id: str) -> str:
    """Firestore document IDs may not contain '/'; substitute defensively."""
    return re.sub(r"[/\s]+", "_", raw_id)


def create_run(
    pipeline_id: str,
    drop_fraction: float = 0.0,
    mode: str = "execution",
    *,
    estate_id: str | None = None,
    source_id: str | None = None,
    pack_id: str | None = None,
) -> str:
    """Creates a new migration run in state REQUESTED and returns its run_id.

    estate_id / source_id / pack_id (Day 11 Phase 2, master doc §32.2):
    which estate this run migrates, which of its sources, and under which
    Migration Pack. Recorded here so every downstream consumer —
    tools/connection_context.py::binding_for_run(), the Wave Manager's
    per-estate slot document, connection health, and the console's estate
    filter — can resolve it from the run rather than from a module
    constant.

    Deliberately keyword-with-default rather than required: roughly
    fifteen callers exist (the test suite, run_assessment.py,
    seed_long_horizon_fixture.py, durability_demo.py), and making these
    required would break all of them for no safety gain while the project
    still ships one estate. `estate_id` defaults to the demo estate, which
    is also how readers must treat a *missing* estate_id on the historical
    runs that predate this field — see scripts/backfill_estate_id.py.
    Treating missing as "no match" instead would empty the dashboard.

    drop_fraction (Day 10 Phase 2): stored on the run record so
    agents/orchestrator/orchestrator.py::handle_planned() — several
    Pub/Sub hops downstream of create_run() — knows whether to seed the
    §7.2 row-loss fault, without threading the value through every
    intermediate event payload. 0.0 (a clean load) is the default.

    mode (Day 10 Phase 3, master doc §32): "execution" (default, backward
    compatible) is this project's flagship full-lifecycle path. "assessment"
    is Discovery -> Lineage -> Risk -> Planner, then stop — no migration
    execution, no target write — the mode agents/discovery/run_assessment.py
    uses to evaluate a new estate/source (via a Migration Pack) before
    ever touching production. Purely descriptive here; nothing in
    run_lifecycle.py enforces it — agents/discovery/run_assessment.py
    simply never calls the stages that would move data.
    """
    run_id = f"run_{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    client = get_client()
    client.collection(RUN_COLLECTION).document(run_id).set(
        {
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "estate_id": estate_id or DEFAULT_ESTATE_ID,
            "source_id": source_id,
            "pack_id": pack_id,
            "drop_fraction": drop_fraction,
            "mode": mode,
            "state": "REQUESTED",
            "attempt": 1,
            "created_at": now,
            "last_transition_at": now,
            # Real transition history, not just current state — the
            # Control Tower's run-detail timeline (master doc §11, Day 9)
            # renders this directly rather than reconstructing it from
            # scattered subcollection timestamps.
            "state_history": [{"state": "REQUESTED", "at": now}],
        }
    )
    return run_id


def get_latest_run_id() -> str:
    """Returns the most recently created run's ID.

    Convenience for chaining Day 3+ agent scripts on the command line
    (`python agents/risk/run_risk.py` with no argument operates on
    whatever Discovery just created) without a real orchestrator wiring
    them together yet — that lands with the Wed-19-Aug state machine.
    """
    from google.cloud.firestore_v1 import Query

    client = get_client()
    docs = list(
        client.collection(RUN_COLLECTION)
        .order_by("created_at", direction=Query.DESCENDING)
        .limit(1)
        .stream()
    )
    if not docs:
        raise RuntimeError("No migration runs found — run agents/discovery/run_discovery.py first.")
    return docs[0].id


def pin_agent_version(run_id: str, agent_key: str, version: str) -> None:
    """Records which registry-resolved agent version handled a stage of
    this run (master doc §20.4, §21.1's 'pinned_agents' run-record field).

    Written before dispatching, per §20.4: "The orchestrator resolves the
    next actor by capability at every state transition and writes the
    resolved agent_id and version into the run record before dispatching."

    Found live (Deploy & Harden Phase 5 close-out — the first time
    Discovery ever actually succeeded against a real reachable source
    and this function got called with a real agent_id for real):
    `{f"pinned_agents.{agent_key}": version}` is a DOTTED-STRING update
    key, and Firestore's Python SDK parses those with
    FieldPath.from_string(), which rejects any segment containing a
    character outside [a-zA-Z0-9_] — every agent_id in this whole system
    is hyphenated ("lineage-agent", "discovery-agent", ...), so this
    call has never once succeeded with a real agent_id against real
    Firestore. No test caught it because nothing exercised this against
    a live backend with a real hyphenated key before now.

    Fixed by building the key via FieldPath(...).to_api_repr() — the
    backtick-escaped dotted string ("pinned_agents.`lineage-agent`"),
    not a bare FieldPath object: this installed SDK version's own
    .update() re-parses whatever key it's given through
    FieldPath.from_api_repr(), which requires a string and raises
    AttributeError on an actual FieldPath instance (confirmed directly —
    passing the FieldPath object itself fails differently, not more
    correctly). The escaped string is what from_string()'s own
    backtick-aware parser is designed to round-trip.
    """
    client = get_client()
    key = FieldPath("pinned_agents", agent_key).to_api_repr()
    client.collection(RUN_COLLECTION).document(run_id).update({key: version})


def delete_run(run_id: str) -> None:
    """Hard-delete, for test/dev hygiene only — the same reasoning as
    tools/registry.py's delete_card(). A throwaway test run left behind
    (e.g. tests/test_state_machine.py creating dozens of 'test.*'
    pipeline_id runs) doesn't just clutter Firestore: the Control Tower
    dashboard picks the single most-recently-created run as the
    "active run" (frontend/app.py::dashboard()), so a leftover test run
    can silently become what a judge sees front and center. Tests that
    call create_run() must clean up in teardown.

    Deletes the subcollections too, which it used to leave behind.

    The old behaviour deleted only the run document, on the reasoning
    that the leftovers were "orphaned but harmless — nothing queries them
    without a live parent reference, and test runs never populate them
    anyway". Both halves were wrong, and a survey of the live project
    measured how wrong: 9,891 orphaned documents under 1,028 dead
    parents, 554 of which had the real run_YYYYMMDD_... shape rather than
    a test id.

    Not harmless, because of how this project has to query. A
    collection-group query combined with `order_by("created_at")` needs a
    composite index that does not exist here (see CLAUDE.md), so every
    console aggregate streams the WHOLE group and filters in Python. The
    console paid to read every orphan on every uncached request and then
    threw it away — `catalog` was 6,428 dead documents out of 9,090.

    `recursive_delete` walks the subcollections; the extra round trips
    are worth not leaving a permanent tax on every later read.
    """
    client = get_client()
    client.recursive_delete(client.collection(RUN_COLLECTION).document(run_id))


def get_run(run_id: str) -> dict:
    client = get_client()
    doc = client.collection(RUN_COLLECTION).document(run_id).get()
    if not doc.exists:
        raise KeyError(f"No such run: {run_id!r}")
    return doc.to_dict()


def transition_state(run_id: str, new_state: str, *, force: bool = False) -> None:
    """Moves a run to new_state, enforcing the state machine unless force=True.

    force is for tests/tooling only — normal agent code must never set it,
    per §9's rule that state-transition legality is not negotiable.
    """
    if new_state not in STATES:
        raise ValueError(f"Unknown state: {new_state!r}")

    current_run = get_run(run_id)
    current_state = current_run["state"]
    if not force and new_state not in _allowed_next_states(current_state):
        raise ValueError(
            f"Illegal transition for run {run_id!r}: {current_state!r} -> {new_state!r}. "
            f"Allowed from {current_state!r}: {sorted(_allowed_next_states(current_state)) or '(none)'}"
        )

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    client = get_client()
    run_ref = client.collection(RUN_COLLECTION).document(run_id)
    batch = client.batch()
    batch.update(
        run_ref,
        {
            "state": new_state,
            "last_transition_at": now,
            "state_history": firestore.ArrayUnion([{"state": new_state, "at": now}]),
        }
    )
    # Durable stage timing for the operator console. Throwaway test runs are
    # intentionally excluded so their deleted parent documents do not leave
    # orphaned telemetry subcollections behind.
    if not str(current_run.get("pipeline_id", "")).startswith("test."):
        started_at = current_run.get("last_transition_at") or current_run.get("created_at")
        duration_ms = None
        if started_at:
            try:
                duration_ms = max(
                    0,
                    round(
                        (dt.datetime.fromisoformat(now) - dt.datetime.fromisoformat(started_at)).total_seconds()
                        * 1000
                    ),
                )
            except ValueError:
                duration_ms = None
        agent_by_stage = {
            "REQUESTED": "discovery-agent",
            "DISCOVERED": "lineage-agent",
            "ANALYZED": "risk-agent",
            "RISK_ASSESSED": "planner-agent",
            "PLANNED": "migration-tools",
            "MIGRATING": "validation-agent",
            "FAILED": "orchestrator",
            "INVESTIGATING": "orchestrator",
            "REMEDIATING": "orchestrator",
            "PASSED": "cutover-agent",
        }
        agent_id = agent_by_stage.get(current_state)
        pinned = current_run.get("pinned_agents") or {}
        batch.set(
            run_ref.collection("stage_metrics").document(str(uuid.uuid4())),
            {
                "run_id": run_id,
                "stage": current_state,
                "next_stage": new_state,
                "attempt": current_run.get("attempt", 1),
                "started_at": started_at,
                "completed_at": now,
                "duration_ms": duration_ms,
                "status": "FAILED" if new_state == "FAILED" else "COMPLETED",
                "agent_id": agent_id,
                "version": pinned.get(agent_id) if agent_id else None,
                "model": (current_run.get("pinned_models") or {}).get(agent_id) if agent_id else None,
                "trace_id": current_run.get("trace_id"),
            },
        )
    batch.commit()


def write_catalog(run_id: str, table_records: list[dict], pipeline_records: list[dict]) -> dict:
    """Persists Discovery's output under the run and moves state to DISCOVERED.

    Returns a small summary dict (counts) for the caller to log/print.
    """
    client = get_client()
    run_ref = client.collection(RUN_COLLECTION).document(run_id)

    # Firestore batches cap at 500 writes; fine for the Day 2 estate size
    # (12-20+ tables, a handful of pipelines). Chunking is a scale-layer
    # concern (master doc §2.2's 100-20,000 pipeline range), not Day 2's.
    batch = client.batch()
    for record in table_records:
        doc_id = _safe_doc_id(record["table_id"])
        batch.set(run_ref.collection("catalog").document(doc_id), record)
    for record in pipeline_records:
        doc_id = _safe_doc_id(record["pipeline_id"])
        batch.set(run_ref.collection("pipelines").document(doc_id), record)
    batch.commit()

    transition_state(run_id, "DISCOVERED")

    return {
        "run_id": run_id,
        "tables_written": len(table_records),
        "pipelines_written": len(pipeline_records),
        "state": "DISCOVERED",
    }
