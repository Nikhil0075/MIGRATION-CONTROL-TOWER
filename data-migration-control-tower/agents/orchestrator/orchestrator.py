"""Event-driven orchestrator (Day 4-6, master doc §3, §8.1, §20.4; Day 10
Phase 2 hardening extends the event chain through Planner, migration
execution, and Validation's recovery loop).

    The orchestrator owns the migration state machine, publishes/
    consumes events, and selects the next permitted action.

    [§20.4] The design rule: the orchestrator must never name an agent
    in code. It resolves actors by querying the registry for a
    capability, and it records the resolved agent version in the run
    record before dispatching.

This is the piece that makes the Day-4 exit condition true: "the run
advances between states on events without manual invocation of each
agent." Every dispatch below goes through tools/registry.py's capability
resolution (discover -> invoke_capability), not a static `from agents.x
.agent import y`. A caller publishes exactly one event
(migration.requested); the handlers cascade Discovery -> Lineage -> Risk
-> Planner -> migration execution -> Validation (looping through
recovery on a failed check) without anyone running five separate
scripts by hand, AND without this module importing any agent module at
the top level.

verify_pii_access_boundary and the Finance Reporting Impact Agent trigger
remain direct/registry-resolved-separately calls respectively — the
former is Risk's own internal compliance self-test (not something
another consumer would independently discover), the latter is the
§20.3 cross-department proof (see trigger_finance_impact_check below).

Scope as of Day 10 Phase 2: migration.requested -> Discovery ->
discovery.completed -> Lineage -> Risk -> risk.assessed -> Planner ->
plan.created -> migration execution -> validation.requested ->
Validation -> validation.passed | validation.failed (looping back
through investigate/remediate -> validation.requested on failure).
migration_executor.execute_migration is deterministic "Migration Tools"
code (master doc §3.1), not itself a registered agent capability — the
same category as write_catalog() below — so PLANNED -> MIGRATING ->
VALIDATING happens inside handle_planned() directly, the same way
write_catalog() runs inline inside handle_migration_requested().

Cutover keeps the human gate separate: READY_FOR_APPROVAL -> APPROVED is
performed only by the authenticated approval service. The production console
then publishes ``cutover.approved`` for
``agents/cutover/run_cutover_worker.py`` to finish CUTOVER -> MONITORING ->
COMPLETE. The original CLI/full-migration runner remains available for live
evaluation and compatibility.

Transport: real Pub/Sub (tools/events.py), synchronous pull. Production
shape (Eventarc-triggered Cloud Run push subscriptions per agent) is the
next rung up — see infrastructure/README.md.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from google.cloud import firestore

from tools import registry, tracing, wave_manager
from tools.events import ack, nack, publish, pull
from tools.firestore_client import get_client
from tools.migration_executor import execute_migration

from tools import migration_plan
from tools.migration_plan import plan_binding, scheduled_targets

from agents.orchestrator import recovery
from agents.orchestrator.run_lifecycle import create_run, get_run, pin_agent_version, transition_state

logger = logging.getLogger("orchestrator")

MIGRATION_REQUESTED_SUB = "migration-requested-sub"
DISCOVERY_COMPLETED_SUB = "discovery-completed-sub"
RISK_ASSESSED_SUB = "risk-assessed-sub"
PLAN_CREATED_SUB = "plan-created-sub"
VALIDATION_REQUESTED_SUB = "validation-requested-sub"
VALIDATION_PASSED_SUB = "validation-passed-sub"
VALIDATION_FAILED_SUB = "validation-failed-sub"

ORACLE_CORPUS_PATH = "simulator/source_setup/oracle_dialect_corpus"
DAG_ARTIFACTS_PATH = "simulator/source_setup/dags"

DISCOVERY_CAPABILITY = "discovery.catalog.estate"
LINEAGE_CAPABILITY = "lineage.graph.build"
RISK_CAPABILITY = "risk.assess.estate"
PLANNER_CAPABILITY = "planner.plan.propose"
VALIDATION_CAPABILITY = "validation.reconcile.source_target"

# Which tables a run migrates, and on which columns, comes from the run's
# plan (tools/migration_plan.py) — not from constants here. Removing
# KEY_COLUMN and WAVE_SOURCE_ID is the point of Day 11 Phase 3b: they
# hardcoded one estate's schema into the orchestrator, so a second estate
# could not be executed without editing this file.
WAVE_RESERVE_RETRY_ATTEMPTS = 5
WAVE_RESERVE_RETRY_SECONDS = 2.0


def _derive_run_risk_class(run_id: str) -> str:
    """Deterministic CRITICAL|HIGH|MEDIUM classification for Wave Manager
    admission, derived from the real risk_findings this run's Risk agent
    already wrote (agents/risk/agent.py::classify_estate) — never a
    model judgment call, same discipline as tools/policy_engine.py.
    CRITICAL if any CRITICAL_DEPENDENCY finding exists (this pipeline
    feeds something downstream cares about — exactly what the wave
    manager's global CRITICAL cap exists to bound), else HIGH if any
    PII_DETECTED finding exists, else MEDIUM.
    """
    client = get_client()
    findings = [
        d.to_dict()
        for d in client.collection("migration_runs").document(run_id).collection("risk_findings").stream()
    ]
    finding_types = {f.get("finding_type") for f in findings}
    if "CRITICAL_DEPENDENCY" in finding_types:
        return "CRITICAL"
    if "PII_DETECTED" in finding_types:
        return "HIGH"
    return "MEDIUM"


def _pull_or_timeout(subscription: str, topic: str, poll_timeout: float) -> dict:
    messages = pull(subscription, timeout=poll_timeout)
    if not messages:
        raise TimeoutError(
            f"No {topic!r} message received within {poll_timeout}s on {subscription!r} — "
            "check that infrastructure/gcp_setup.sh has been run and its subscription list is current."
        )
    return messages[0]


def _consume(subscription: str, topic: str, handler, poll_timeout: float):
    """Pulls exactly one message from `subscription`, invokes `handler`
    with its decoded payload, and only THEN acknowledges it.

    Fixes a real defect found live: tools/events.py::pull() used to
    auto-ack before returning, so any handler failure downstream (a Wave
    Manager capacity HOLD from handle_planned, any exception) silently
    discarded the message with no Pub/Sub redelivery — a run could get
    stuck in PLANNED forever with no recovery path, despite handler code
    that explicitly raised expecting to be retried on redelivery.

    On success: ack() the message, return the handler's result.
    On failure: nack() the message (resets its ack deadline to 0, making
    it available for redelivery immediately rather than waiting out the
    default deadline) and re-raise. The redelivered copy is safe to
    reprocess because every handler below claims its message atomically
    via _dedup_claim() before doing any side effect (see that function's
    docstring for exactly what "safe" means here).
    """
    message = _pull_or_timeout(subscription, topic, poll_timeout)
    ack_id = message.get("_pubsub_ack_id")
    try:
        result = handler(message)
    except Exception:
        if ack_id:
            nack(subscription, ack_id)
        raise
    if ack_id:
        ack(subscription, ack_id)
    return result


def _dedup_claim(payload: dict, handler_name: str) -> tuple[str, dict | None]:
    """Atomically claims `payload`'s message for `handler_name` via a
    Firestore transaction — the fix for a real defect found live: the
    previous check-then-act-then-mark pattern (a plain read, followed by
    the handler's real side effects, followed eventually by a separate
    write) was not atomic. A process crash between a side effect
    (execute_migration, publish, transition_state) and that final write
    left no record the work had even started, so a redelivered copy of
    the same message would repeat it in full — silently, since nothing
    recorded the first attempt ever began.

    Returns one of:
      ("done", cached_result) — this message was already fully processed
        by this handler; the caller must return cached_result as-is
        (already has deduped=True merged in) without doing any work.
      ("claimed", None) — this call just claimed the message for the
        first time; the caller should do its work, then call
        _dedup_complete().
      ("stale_claim", None) — a PRIOR claim exists but was never
        completed (the process crashed mid-handler). The caller should
        redo the work. This project runs a single orchestrator process
        (documented at the top of this module — no fleet of concurrent
        workers pulling the same subscription), so a fresh "claimed"
        state can only mean an in-flight crash recovery, never real
        concurrent contention; redoing is safe because every handler's
        side effects are themselves safe to repeat (execute_migration()
        truncates-and-reloads on its first batch — tools/migration_
        executor.py — the same "clean reload" primitive tools/recovery.py
        already uses for real remediation) and transition_state() is
        graph-checked (agents/orchestrator/run_lifecycle.py), so a redo
        that tries an already-applied transition fails loudly with
        ValueError instead of silently double-applying it.

    The atomicity here closes the race the check-then-act pattern left
    open: the claim write happens inside the SAME transaction as the
    existence check, so two callers racing on the same message_id cannot
    both observe "not yet claimed" and both proceed.
    """
    message_id = payload.get("_pubsub_message_id")
    if not message_id:
        return "claimed", None  # no dedup possible without a message id (e.g. a direct/test call)

    doc_ref = get_client().collection("processed_messages").document(f"{handler_name}:{message_id}")

    @firestore.transactional
    def _txn(transaction: firestore.Transaction) -> tuple[str, dict | None]:
        snapshot = doc_ref.get(transaction=transaction)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        if not snapshot.exists:
            transaction.set(doc_ref, {"handler": handler_name, "status": "claimed", "claimed_at": now})
            return "claimed", None
        data = snapshot.to_dict()
        if data.get("status") == "done":
            logger.info("%s: message %s already processed (deduped, no-op)", handler_name, message_id)
            return "done", {**data.get("result", {}), "deduped": True}
        logger.warning(
            "%s: message %s has a stale claim (claimed_at=%s, never completed) — "
            "treating as a crash-recovery redo, not concurrent contention.",
            handler_name, message_id, data.get("claimed_at"),
        )
        transaction.set(doc_ref, {"handler": handler_name, "status": "claimed", "claimed_at": now})
        return "stale_claim", None

    return _txn(get_client().transaction())


def _dedup_complete(payload: dict, handler_name: str, result: dict) -> None:
    """Marks a claim (see _dedup_claim) as done, with the result to
    return on any future duplicate delivery. Call only after every side
    effect has genuinely finished."""
    operation_id = payload.get("operation_id")
    if operation_id:
        get_client().collection("operation_requests").document(operation_id).update(
            {
                "status": "accepted",
                "result": result,
                "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )
    message_id = payload.get("_pubsub_message_id")
    if not message_id:
        return
    get_client().collection("processed_messages").document(f"{handler_name}:{message_id}").set(
        {"handler": handler_name, "status": "done", "result": result}
    )


def handle_migration_requested(payload: dict) -> dict:
    """Consumes migration.requested: resolves Discovery by capability, runs it, publishes discovery.completed.

    A second real defect, distinct from the generic _dedup_claim race,
    lived here specifically: the old code marked the message "processed"
    immediately after create_run(), BEFORE write_catalog()/publish() ran
    — the earliest possible point, not the latest. A crash between that
    mark and publish() would leave the run stuck at REQUESTED forever
    (no discovery.completed ever published) while a redelivered copy of
    the same message would be treated as already fully done and silently
    no-op. Fixed by marking done only at the very end, like every other
    handler — but create_run() itself genuinely isn't safe to call
    twice (unlike every other handler's side effects, which act on an
    existing run_id), so a stale-claim redo reuses the run_id recorded
    right after create_run() succeeded rather than creating a second run
    for the same message.
    """
    status, cached = _dedup_claim(payload, "handle_migration_requested")
    if status == "done":
        return cached

    message_id = payload.get("_pubsub_message_id")
    claim_doc = (
        get_client().collection("processed_messages").document(f"handle_migration_requested:{message_id}")
        if message_id
        else None
    )

    run_id = None
    if status == "stale_claim" and claim_doc is not None:
        existing = claim_doc.get().to_dict() or {}
        run_id = existing.get("run_id")

    pipeline_id = payload["pipeline_id"]
    # drop_fraction rides along on the initiating event so handle_planned()
    # (several hops downstream) knows whether to seed the §7.2 row-loss
    # fault — it's stored on the run record here rather than threaded
    # through every intermediate event payload.
    drop_fraction = payload.get("drop_fraction", 0.0)
    # Which estate this run migrates rides along the same way. The console
    # authorizes the requester against it before publishing
    # (frontend/api_v1.py::start_run -> authorize_estate), and recording it
    # here is what lets every downstream consumer — the wave key, the
    # connection binding, the console's estate filter — resolve it from the
    # run instead of assuming one estate exists.
    estate_id = payload.get("estate_id")
    source_id = payload.get("source_id")
    pack_id = payload.get("pack_id")
    if run_id is None:
        run_id = create_run(
            pipeline_id,
            drop_fraction=drop_fraction,
            estate_id=estate_id,
            source_id=source_id,
            pack_id=pack_id,
        )
        if claim_doc is not None:
            claim_doc.set({"run_id": run_id}, merge=True)
        operation_id = payload.get("operation_id")
        if operation_id:
            get_client().collection("operation_requests").document(operation_id).set(
                {
                    "status": "active",
                    "run_id": run_id,
                    "estate_id": estate_id,
                    "result": {"run_id": run_id},
                    "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                },
                merge=True,
            )
        logger.info("migration.requested -> run created: %s (drop_fraction=%s)", run_id, drop_fraction)
    else:
        logger.info("migration.requested -> resuming existing run %s after a crash-recovery redo", run_id)

    with tracing.span("handle_migration_requested", run_id=run_id, pipeline_id=pipeline_id):
        (table_records, pipeline_records), agent_id, version = registry.invoke_capability(
            DISCOVERY_CAPABILITY, ORACLE_CORPUS_PATH, DAG_ARTIFACTS_PATH
        )
        pin_agent_version(run_id, agent_id, version)
        logger.info("resolved %s (capability=%s) -> v%s", agent_id, DISCOVERY_CAPABILITY, version)

        # write_catalog is deterministic state-plane code (run_lifecycle.py),
        # not an agent capability — imported directly like every other
        # orchestrator-owned state transition.
        from agents.orchestrator.run_lifecycle import write_catalog

        catalog_summary = write_catalog(run_id, table_records, pipeline_records)

        publish("discovery.completed", {"run_id": run_id})
        logger.info("discovery.completed published for run: %s", run_id)

    result = {"run_id": run_id, **catalog_summary}
    _dedup_complete(payload, "handle_migration_requested", result)
    return result


def handle_discovery_completed(payload: dict) -> dict:
    """Consumes discovery.completed: resolves Lineage then Risk by capability,
    advancing ANALYZED -> RISK_ASSESSED, then publishes risk.assessed."""
    status, cached = _dedup_claim(payload, "handle_discovery_completed")
    if status == "done":
        return cached

    run_id = payload["run_id"]

    with tracing.span("handle_discovery_completed", run_id=run_id):
        with tracing.span("lineage.graph.build", run_id=run_id, capability=LINEAGE_CAPABILITY):
            lineage_summary, lineage_agent_id, lineage_version = registry.invoke_capability(
                LINEAGE_CAPABILITY, run_id
            )
            pin_agent_version(run_id, lineage_agent_id, lineage_version)
            transition_state(run_id, "ANALYZED")
            logger.info(
                "resolved %s (capability=%s) -> v%s, run %s -> ANALYZED (%s)",
                lineage_agent_id, LINEAGE_CAPABILITY, lineage_version, run_id, lineage_summary,
            )

        with tracing.span(
            "risk.assess.estate", run_id=run_id, capability=RISK_CAPABILITY
        ) as risk_span:
            risk_summary, risk_agent_id, risk_version = registry.invoke_capability(RISK_CAPABILITY, run_id)
            pin_agent_version(run_id, risk_agent_id, risk_version)
            if risk_span is not None:
                risk_span.set_attribute("agent_id", risk_agent_id)
                risk_span.set_attribute("version", risk_version)

            # verify_pii_access_boundary, run_fast_pii_prescreen, and
            # assess_documentation_drift are Risk's own additional tools, always
            # run together with classify_estate as part of the same agent turn —
            # not independently capability-resolved (see agents/risk/README.md).
            from agents.risk.agent import (
                assess_documentation_drift,
                run_fast_pii_prescreen,
                verify_pii_access_boundary,
            )

            prescreen_summary = run_fast_pii_prescreen(run_id)
            drift_summary = assess_documentation_drift(run_id)
            pii_decision = verify_pii_access_boundary(run_id)
            transition_state(run_id, "RISK_ASSESSED")
            logger.info(
                "resolved %s (capability=%s) -> v%s, run %s -> RISK_ASSESSED (%s)",
                risk_agent_id, RISK_CAPABILITY, risk_version, run_id, risk_summary,
            )

        finance_impact = trigger_finance_impact_check(run_id)

        publish("risk.assessed", {"run_id": run_id})
        logger.info("risk.assessed published for run: %s", run_id)

    result = {
        "run_id": run_id,
        "lineage": lineage_summary,
        "risk": risk_summary,
        "fast_pii_prescreen": prescreen_summary,
        "documentation_drift": drift_summary,
        "pii_access_denied": pii_decision["decision"] == "DENY",
        "finance_impact": finance_impact,
    }
    _dedup_complete(payload, "handle_discovery_completed", result)
    return result


def trigger_finance_impact_check(run_id: str, capability: str = "impact.assessment.*") -> dict | None:
    """The §20.3 cross-department discovery proof.

    Queries the registry for ANY APPROVED agent advertising
    'impact.assessment.*' — a capability namespace this module has no
    hardcoded knowledge of beyond the wildcard prefix. If the Finance
    Reporting Impact Agent (owned by a different department, published
    by a different identity — see infrastructure/seed_finance_agent.py)
    is APPROVED, it gets invoked here with zero orchestrator changes. If
    it's ever deprecated, this logs and returns None rather than
    crashing — the negative proof from §20.3.
    """
    try:
        result, agent_id, version = registry.invoke_capability(capability, run_id)
    except registry.NoApprovedProvider as exc:
        logger.warning("finance impact check skipped: %s", exc)
        return None

    pin_agent_version(run_id, agent_id, version)
    logger.info("resolved %s (capability=%s) -> v%s for finance impact check", agent_id, capability, version)
    return result


def handle_risk_assessed(payload: dict) -> dict:
    """Consumes risk.assessed: resolves the Planner by capability,
    proposes the migration plan, transitions RISK_ASSESSED -> PLANNED,
    publishes plan.created. Returns the full MigrationPlan dict."""
    status, cached = _dedup_claim(payload, "handle_risk_assessed")
    if status == "done":
        return cached

    run_id = payload["run_id"]
    with tracing.span("handle_risk_assessed", run_id=run_id, capability=PLANNER_CAPABILITY) as current_span:
        plan, agent_id, version = registry.invoke_capability(PLANNER_CAPABILITY, run_id)
        pin_agent_version(run_id, agent_id, version)
        if current_span is not None:
            current_span.set_attribute("agent_id", agent_id)
            current_span.set_attribute("version", version)
            current_span.set_attribute("plan_hash", plan["plan_hash"])
        transition_state(run_id, "PLANNED")
        logger.info(
            "resolved %s (capability=%s) -> v%s, run %s -> PLANNED (plan_hash=%s, steps=%d)",
            agent_id, PLANNER_CAPABILITY, version, run_id, plan["plan_hash"][:12], len(plan["steps"]),
        )

        publish("plan.created", {"run_id": run_id})
        logger.info("plan.created published for run: %s", run_id)
    _dedup_complete(payload, "handle_risk_assessed", plan)
    return plan


def handle_planned(payload: dict) -> dict:
    """Consumes plan.created: executes the migration (PLANNED -> MIGRATING
    -> VALIDATING), then publishes validation.requested. Returns the
    execution manifest.

    execute_migration is deterministic data-movement tool code (master
    doc §3.1's "Migration Tools" box), not itself a registered agent
    capability — see this module's docstring.

    Three boundaries enforced here, added after the second audit round:
      - Idempotency (_dedup_claim/_dedup_complete): this is the handler
        that actually MOVES DATA — a redelivered plan.created message
        must never re-run a real migration, claimed atomically before
        anything else (see _dedup_claim's docstring for why the older
        check-then-act pattern here was itself a real defect).
      - Assessment-mode guard: a run created with mode="assessment"
        (agents/discovery/run_assessment.py) must never reach migration
        execution even if somehow handed to this handler (it isn't,
        today — run_assessment.py stops at PLANNED via its own direct
        calls, never publishing plan.created — but the boundary should
        hold on its own, not rely on every future caller behaving).
      - Wave Manager admission (tools/wave_manager.py::reserve_slot):
        real dispatch, not just evaluate_wave() called from tests/the
        scale harness — a transactional Firestore reservation against
        policies/wave_limits.yaml's concurrency caps, bounded-retried a
        few times, released in a finally block regardless of outcome.
        Raising below on exhausted retries genuinely triggers Pub/Sub
        redelivery now — this handler is invoked via
        orchestrator.py::_consume(), which only acks after success and
        nack()s on any exception (see that function's docstring for the
        real defect this fixes: tools/events.py::pull() used to
        auto-ack, so this exact raise used to silently lose the message
        with no redelivery, leaving the run stuck in PLANNED forever).
    """
    status, cached = _dedup_claim(payload, "handle_planned")
    if status == "done":
        return cached

    run_id = payload["run_id"]
    run = get_run(run_id)
    if run.get("mode") == "assessment":
        raise RuntimeError(
            f"Run {run_id!r} is in assessment mode — refusing to execute a migration. "
            "Assessment mode stops at PLANNED by design (see agents/discovery/run_assessment.py)."
        )
    drop_fraction = run.get("drop_fraction", 0.0)
    risk_class = _derive_run_risk_class(run_id)

    # What to migrate comes from the plan the Planner wrote, not from
    # constants here. scheduled_targets() raises if a plan schedules
    # nothing, which is deliberate: a migration that moves zero tables and
    # reports success is the worst available outcome.
    targets = scheduled_targets(run_id)
    binding = plan_binding(run_id)

    # Reserve ONCE per run, keyed by estate and source — not per target.
    # A per-target reservation would make a multi-table run contend with
    # itself and deadlock against its own max_concurrent_per_source.
    wave_key = f"{binding.estate_id}:{binding.source_id}"

    reservation = None
    for attempt in range(1, WAVE_RESERVE_RETRY_ATTEMPTS + 1):
        reservation = wave_manager.reserve_slot(wave_key, run_id, risk_class=risk_class)
        if reservation["decision"] == wave_manager.DECISION_ADMIT:
            break
        logger.info(
            "handle_planned: wave admission HOLD for run %s (attempt %d/%d): %s",
            run_id, attempt, WAVE_RESERVE_RETRY_ATTEMPTS, reservation["reason"],
        )
        if attempt < WAVE_RESERVE_RETRY_ATTEMPTS:
            time.sleep(WAVE_RESERVE_RETRY_SECONDS)
    if reservation["decision"] != wave_manager.DECISION_ADMIT:
        # Raising here (rather than returning a "held" result) is what
        # makes _consume() nack() the message instead of acking it — the
        # message was only ever claimed above (_dedup_claim), never
        # completed, so it's genuinely retried on redelivery: the "stale
        # claim -> safe redo" path handles the retry correctly.
        raise RuntimeError(
            f"Run {run_id!r} held by Wave Manager after {WAVE_RESERVE_RETRY_ATTEMPTS} attempts: "
            f"{reservation['reason']}. Will be retried on redelivery."
        )
    logger.info(
        "handle_planned: wave admission ADMIT for run %s (source=%s, risk_class=%s, targets=%d)",
        run_id, wave_key, risk_class, len(targets),
    )

    try:
        with tracing.span("handle_planned", run_id=run_id, drop_fraction=drop_fraction):
            transition_state(run_id, "MIGRATING")

            executions = []
            for target in targets:
                # Fail fast on the first failing target: earlier targets
                # stay loaded, and the _dedup_claim stale-claim path re-runs
                # all of them on redelivery. That is safe because each
                # execute_migration truncates the target table on its first
                # batch. Partial-resume bookkeeping is deliberately not
                # attempted here.
                executions.append(
                    execute_migration(
                        run_id=run_id,
                        target=target,
                        binding=binding,
                        drop_fraction=drop_fraction,
                    )
                )

            # Aggregate sums, not the last target's figures: the Overview
            # page's migrated_percent (frontend/api_v1.py) and
            # evaluation/scenarios.py both read these, and silently
            # narrowing them to one table would misreport progress.
            manifest = {
                **executions[-1],
                "executions": executions,
                "target_count": sum(e.get("target_count") or 0 for e in executions),
                "source_count": sum(e.get("source_count") or 0 for e in executions),
                "dropped_count": sum(e.get("dropped_count") or 0 for e in executions),
                "targets_migrated": len(executions),
            }

            transition_state(run_id, "VALIDATING")
            logger.info(
                "plan.created -> %d target(s) executed (drop_fraction=%s, loaded %d/%d rows), "
                "run %s -> VALIDATING",
                len(executions), drop_fraction,
                manifest["target_count"], manifest["source_count"], run_id,
            )

            publish("validation.requested", {"run_id": run_id})
            logger.info("validation.requested published for run: %s", run_id)
    finally:
        wave_manager.release_slot(wave_key, run_id)

    _dedup_complete(payload, "handle_planned", manifest)
    return manifest


def _open_incident(run_id: str) -> dict | None:
    """The most recent PENDING incident for this run, if any — fetched
    unfiltered and filtered in Python (this project's established
    workaround for collection_group()/`.where()` needing a composite
    index that doesn't exist here; a direct subcollection query like
    this one doesn't strictly need it, but the pattern is kept
    consistent with the rest of the codebase)."""
    client = get_client()
    incidents = [
        d.to_dict()
        for d in client.collection("migration_runs").document(run_id).collection("incidents").stream()
    ]
    pending = [i for i in incidents if i.get("outcome") == "PENDING"]
    return pending[-1] if pending else None


def handle_validation_requested(payload: dict) -> dict:
    """Consumes validation.requested: runs reconciliation via Validation's
    registry capability. If this attempt follows a remediation (an open
    incident exists), closes it with THIS attempt's real outcome — never
    optimistically. Transitions VALIDATING -> PASSED or VALIDATING ->
    FAILED and publishes the matching event.
    """
    status, cached = _dedup_claim(payload, "handle_validation_requested")
    if status == "done":
        return cached

    run_id = payload["run_id"]
    with tracing.span(
        "handle_validation_requested", run_id=run_id, capability=VALIDATION_CAPABILITY
    ) as current_span:
        result, agent_id, version = registry.invoke_capability(VALIDATION_CAPABILITY, run_id)
        pin_agent_version(run_id, agent_id, version)
        if current_span is not None:
            current_span.set_attribute("agent_id", agent_id)
            current_span.set_attribute("version", version)
            current_span.set_attribute("overall_status", result["overall_status"])

        incident = _open_incident(run_id)
        if incident is not None:
            recovery.close_incident(run_id, incident, resolved=result["overall_status"] == "PASSED")

        if result["overall_status"] == "PASSED":
            transition_state(run_id, "PASSED")
            publish("validation.passed", {"run_id": run_id})
            logger.info(
                "resolved %s (capability=%s) -> v%s, run %s -> PASSED",
                agent_id, VALIDATION_CAPABILITY, version, run_id,
            )
        else:
            transition_state(run_id, "FAILED")
            publish("validation.failed", {"run_id": run_id})
            logger.info(
                "resolved %s (capability=%s) -> v%s, run %s -> FAILED",
                agent_id, VALIDATION_CAPABILITY, version, run_id,
            )

    result_out = {"run_id": run_id, "overall_status": result["overall_status"], "checks": result["checks"]}
    _dedup_complete(payload, "handle_validation_requested", result_out)
    return result_out


def handle_validation_failed(payload: dict) -> dict:
    """Consumes validation.failed: drives FAILED -> INVESTIGATING ->
    REMEDIATING -> VALIDATING via recovery.py, then republishes
    validation.requested for the re-check — a genuine second Pub/Sub
    round trip through handle_validation_requested above, not a direct
    re-call. The incident stays 'PENDING' until that re-check closes it.
    """
    status, cached = _dedup_claim(payload, "handle_validation_failed")
    if status == "done":
        return cached

    run_id = payload["run_id"]

    with tracing.span("handle_validation_failed", run_id=run_id) as current_span:
        transition_state(run_id, "INVESTIGATING")
        # table_ref is derived from the failing reconciliation records
        # rather than passed in: in a multi-target run the table that
        # actually failed is the one worth investigating, and only the
        # results know which that was.
        incident = recovery.investigate(run_id)
        if current_span is not None:
            current_span.set_attribute("incident_signature", incident["signature"])
            current_span.set_attribute("root_cause_generated_by", incident["root_cause_generated_by"])
        logger.info("validation.failed -> investigated: %s (%s)", incident["signature"], incident["root_cause_generated_by"])

        transition_state(run_id, "REMEDIATING")
        # Remediate the target that failed, resolved from the plan by the
        # table the incident names.
        failed_target = migration_plan.target_for_table_ref(
            run_id, incident.get("table_ref") or ""
        ) or scheduled_targets(run_id)[0]
        recovery.remediate(
            run_id,
            incident,
            source_schema=failed_target["source_schema"],
            source_table=failed_target["source_table"],
            target_table=failed_target["target_table"],
            key_column=failed_target["key_column"],
            binding=plan_binding(run_id),
        )
        transition_state(run_id, "VALIDATING")

        publish("validation.requested", {"run_id": run_id})
        logger.info("validation.requested (re-check) published for run: %s", run_id)
    result_out = {"run_id": run_id, "incident": incident}
    _dedup_complete(payload, "handle_validation_failed", result_out)
    return result_out


def run_once(pipeline_id: str, drop_fraction: float = 0.0, poll_timeout: float = 30.0) -> dict:
    """Publishes migration.requested and drives the cascade to RISK_ASSESSED.

    This is the Day 4 exit-condition scope (agents/orchestrator/
    run_orchestrator.py's contract: "the run advances DISCOVERED ->
    ANALYZED -> RISK_ASSESSED automatically") and stays exactly that
    scope — see advance_through_validation() below for the Day 10
    Phase 2 extension through Planner/migration/Validation. Returns a
    combined summary of every stage. Raises TimeoutError if an expected
    event doesn't arrive within poll_timeout — a real signal something's
    wrong (a handler crashed, a subscription is misconfigured), not
    swallowed silently.
    """
    publish("migration.requested", {"pipeline_id": pipeline_id, "drop_fraction": drop_fraction})
    logger.info("migration.requested published for pipeline: %s (drop_fraction=%s)", pipeline_id, drop_fraction)

    discovery_summary = _consume(
        MIGRATION_REQUESTED_SUB, "migration.requested", handle_migration_requested, poll_timeout
    )
    analysis_summary = _consume(
        DISCOVERY_COMPLETED_SUB, "discovery.completed", handle_discovery_completed, poll_timeout
    )

    return {"discovery": discovery_summary, "analysis": analysis_summary}


def advance_through_validation(
    pipeline_id: str,
    drop_fraction: float = 0.0,
    poll_timeout: float = 30.0,
    max_validation_attempts: int = 5,
) -> dict:
    """Continues past run_once()'s RISK_ASSESSED stopping point, through
    Planner, migration execution, and Validation (looping through the
    recovery cycle on a failed check) to a terminal PASSED outcome —
    purely via further Pub/Sub round trips, one real message per stage.
    Raises TimeoutError if PASSED is never reached within
    max_validation_attempts.

    This is what agents/orchestrator/pipeline_stages.py::advance_to_passed()
    (the Day 5+ full-milestone driver used by run_full_migration.py,
    durability_demo.py, and evaluation/scenarios.py) actually calls.

    The whole call is wrapped in one outer span (Day 10 Phase 5) so every
    handler's own span nests underneath it in Cloud Trace — one trace
    tree per run, filterable by its run_id attribute, even though every
    stage below runs in this same process today rather than as separate
    Cloud Run services (see this module's docstring).
    """
    with tracing.span("advance_through_validation", pipeline_id=pipeline_id) as outer_span:
        cascade = run_once(pipeline_id, drop_fraction=drop_fraction, poll_timeout=poll_timeout)
        run_id = cascade["discovery"]["run_id"]
        if outer_span is not None:
            outer_span.set_attribute("run_id", run_id)

        plan = _consume(RISK_ASSESSED_SUB, "risk.assessed", handle_risk_assessed, poll_timeout)
        manifest = _consume(PLAN_CREATED_SUB, "plan.created", handle_planned, poll_timeout)

        validation_summary: dict | None = None
        had_failure = False
        incident: dict | None = None
        for _attempt in range(1, max_validation_attempts + 1):
            validation_summary = _consume(
                VALIDATION_REQUESTED_SUB, "validation.requested", handle_validation_requested, poll_timeout
            )
            if validation_summary["overall_status"] == "PASSED":
                # Assert the event really arrived on the wire, not just that
                # the handler said PASSED — the same discipline as every
                # other _consume() call in this chain. No real handler
                # needed for this one, just confirm-and-ack.
                _consume(VALIDATION_PASSED_SUB, "validation.passed", lambda _payload: None, poll_timeout)
                break
            had_failure = True
            recovery_summary = _consume(
                VALIDATION_FAILED_SUB, "validation.failed", handle_validation_failed, poll_timeout
            )
            incident = recovery_summary["incident"]
        else:
            raise TimeoutError(
                f"Run {run_id!r} did not reach a PASSED validation within {max_validation_attempts} attempt(s)."
            )

    return {
        "run_id": run_id,
        "cascade": cascade,
        "plan": plan,
        "first_pass": manifest,
        "reconciliation_failed": had_failure,
        "incident": incident,
        "final_validation": {
            "overall_status": validation_summary["overall_status"],
            "checks": validation_summary["checks"],
        },
    }
