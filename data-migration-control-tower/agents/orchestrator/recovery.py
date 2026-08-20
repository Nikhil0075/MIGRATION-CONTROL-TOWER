"""Recovery loop (Day 5, master doc's architecture diagram: "PASS/FAIL ->
Recovery -> retry").

Not one of the six named agents — recovery coordination is an
orchestrator responsibility (§3.1: "retry, timeout, idempotency, and
failure-recovery rules"), reading evidence the Validation Agent already
produced rather than re-deciding pass/fail itself.

Two stages, matching §8's FAILED -> INVESTIGATING -> REMEDIATING ->
VALIDATING states:

  investigate() — builds an Incident record (contracts/metadata_model.json)
    from real evidence: which checks failed, the hash check's missing-key
    list, and the responsible pipeline found by traversing the Lineage
    edges the same run already produced (the "lineage is traversed to
    identify upstream filter" outcome §7.2 specifies for row loss).

    Checks tools/memory_bank.py FIRST (§21.2 proof 3): if a prior run
    already confirmed a fix for this exact signature, that fact is cited
    as the root cause verbatim (root_cause_generated_by='recalled_memory')
    and the run record's memory_refs is updated — but this NEVER skips
    deterministic re-validation later in the loop, only informs the
    narrative. Only when nothing is recalled does this fall back to a
    root-cause *narrative* attempt via a direct Vertex AI call (§9's
    Gemini-eligible half), with a deterministic template as an honest
    fallback if that call isn't available either. The fleet's outcome
    never depends on the model call succeeding.

  remediate() — applies a deterministic, pre-cataloged fix. Today's only
    known signature is row_loss, whose fix is "reload the table cleanly"
    (tools/migration_executor.py, drop_fraction=0.0). This is exactly
    §9's split: Gemini/memory explains, deterministic code acts.

  close_incident() — on RESOLVED, records the confirmed fact into
    tools/memory_bank.py so a *future* run with the same signature can
    recall it. Memory only grows from confirmed outcomes, never from an
    unvalidated guess.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from tools import memory_bank
from tools.usage_meter import extract_model_usage, record_model_usage  # noqa: E402
from tools.firestore_client import get_client
from tools.migration_executor import execute_migration

logger = logging.getLogger("recovery")

RUN_COLLECTION = "migration_runs"


def _latest_reconciliation(run_id: str) -> list[dict]:
    client = get_client()
    return [
        d.to_dict()
        for d in client.collection(RUN_COLLECTION)
        .document(run_id)
        .collection("reconciliation")
        .stream()
    ]


def _responsible_pipelines(run_id: str, table_ref: str) -> list[str]:
    """Traverses the Lineage-derived dependency graph to find which
    pipeline reads (and therefore could have filtered) `table_ref`."""
    client = get_client()
    edges = [
        d.to_dict()
        for d in client.collection(RUN_COLLECTION)
        .document(run_id)
        .collection("dependencies")
        .stream()
    ]
    return sorted(
        {
            e["to_asset"]
            for e in edges
            if e["from_asset"] == table_ref and e["relationship"] == "reads"
        }
    )


def _deterministic_narrative(failed_checks: list[dict], table_ref: str, pipelines: list[str]) -> str:
    check_names = ", ".join(c["check_type"] for c in failed_checks)
    pipeline_note = f" via pipeline(s) {pipelines}" if pipelines else ""
    return (
        f"Reconciliation checks [{check_names}] failed for {table_ref}. "
        f"The hash/row-count mismatch is consistent with rows being dropped "
        f"during load{pipeline_note}. Deterministic remediation: reload "
        f"{table_ref} cleanly and re-validate."
    )


def _try_gemini_narrative(
    failed_checks: list[dict],
    table_ref: str,
    pipelines: list[str],
    run_id: str | None = None,
) -> str | None:
    """Best-effort Gemini call for a natural-language explanation (§9).

    Returns None on any failure — model unavailability must never block
    the recovery loop, only its explanatory text.
    """
    try:
        import os

        import vertexai
        from vertexai.generative_models import GenerativeModel

        vertexai.init(project=os.environ["GCP_PROJECT_ID"], location="us-central1")
        model = GenerativeModel(os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"))
        evidence = "; ".join(
            f"{c['check_type']}: source={c.get('source_value')!r} target={c.get('target_value')!r}"
            for c in failed_checks
        )
        prompt = (
            "You are explaining a data migration reconciliation failure to an "
            "engineer, in two sentences, plain language, no speculation beyond "
            "the evidence given. Table: "
            f"{table_ref}. Candidate responsible pipeline(s): {pipelines or 'unknown'}. "
            f"Failed check evidence: {evidence}."
        )
        response = model.generate_content(prompt)
        # Token counts as the model itself reports them, attributed to
        # the run that caused the call. Measured, never inferred from
        # prompt length — a cost derived from a guess is the kind of
        # invented evidence this project exists not to produce.
        usage = extract_model_usage(response)
        if usage:
            record_model_usage(
                run_id,
                model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
                input_tokens=usage[0],
                output_tokens=usage[1],
                purpose="recovery.narrative",
            )
        text = (response.text or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 — any failure here is non-fatal
        logger.info("Gemini narrative unavailable (%s); using deterministic template.", exc)
        return None


def _failing_table_ref(failed_checks: list[dict], results: list[dict]) -> str:
    """Which table this run's failure is actually about.

    Derived from the reconciliation records rather than passed in by the
    caller: they already record `table` per check, and in a multi-target
    run the table that failed is the one worth investigating. A caller-
    supplied default (this used to be "Sales.Customers") would name the
    wrong table the moment a run migrated more than one.
    """
    for check in failed_checks:
        if check.get("table"):
            return check["table"]
    for check in results:
        if check.get("table"):
            return check["table"]
    return "unknown"


def investigate(run_id: str, table_ref: str | None = None) -> dict:
    """Tool: builds an Incident from the latest reconciliation results.

    Checks memory_bank.recall() first — see module docstring for why a
    recall never substitutes for the deterministic re-validation that
    still runs afterward.

    table_ref defaults to the table the failing checks name; pass it
    explicitly only to investigate a specific table of a multi-table run.
    """
    results = _latest_reconciliation(run_id)
    failed_checks = [r for r in results if r["status"] == "FAIL"]
    if table_ref is None:
        table_ref = _failing_table_ref(failed_checks, results)

    pipelines = _responsible_pipelines(run_id, table_ref)

    signature = f"row_loss:{table_ref}" if any(
        c["check_type"] in ("row_count", "hash") for c in failed_checks
    ) else f"reconciliation_failure:{table_ref}"

    recalled = memory_bank.recall(signature)
    if recalled is not None:
        # canonical_root_cause stays the stable, unwrapped fact text —
        # close_incident() re-confirms THIS into memory, never the
        # display-only "Recalled from memory (...)" wrapper below. Without
        # this split, a fact recalled and re-confirmed across N runs would
        # nest an extra "Recalled from memory (...)" wrapper each time.
        canonical_root_cause = recalled["root_cause"]
        narrative = (
            f"Recalled from memory (previously confirmed on run(s) "
            f"{recalled['source_run_ids']}, reused {recalled['reuse_count']} time(s) "
            f"before this): {canonical_root_cause} Known fix: {recalled['fix']}"
        )
        root_cause_generated_by = "recalled_memory"
        memory_bank.mark_recalled(signature, run_id)
        _record_memory_ref(run_id, signature)
    else:
        narrative = _try_gemini_narrative(failed_checks, table_ref, pipelines, run_id=run_id)
        root_cause_generated_by = "gemini" if narrative else "deterministic"
        if narrative is None:
            narrative = _deterministic_narrative(failed_checks, table_ref, pipelines)
        canonical_root_cause = narrative

    incident = {
        "incident_id": str(uuid.uuid4()),
        "signature": signature,
        # Which table this incident is about, so the orchestrator can
        # resolve the matching plan target for remediation without
        # re-deriving it from the signature string.
        "table_ref": table_ref,
        "root_cause": narrative,
        "canonical_root_cause": canonical_root_cause,
        "root_cause_generated_by": root_cause_generated_by,
        "fix": "",
        "outcome": "PENDING",
        "reusable_memory": True,
        "run_id": run_id,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }

    client = get_client()
    client.collection(RUN_COLLECTION).document(run_id).collection("incidents").document(
        incident["incident_id"]
    ).set(incident)

    logger.info("investigate: %s (%s)", incident["signature"], root_cause_generated_by)
    return incident


def _record_memory_ref(run_id: str, signature: str) -> None:
    """Run record's memory_refs (§21.3 example: 'memory_refs': ['incident/...'])."""
    client = get_client()
    run_ref = client.collection(RUN_COLLECTION).document(run_id)
    run_doc = run_ref.get().to_dict() or {}
    memory_refs = run_doc.get("memory_refs", [])
    ref = f"memory_bank/{signature}"
    if ref not in memory_refs:
        memory_refs.append(ref)
    run_ref.update({"memory_refs": memory_refs})


_KNOWN_FIXES = {
    "row_loss": "reload via tools/migration_executor.py with drop_fraction=0.0",
}


def remediate(
    run_id: str,
    incident: dict,
    source_schema: str,
    source_table: str,
    target_table: str,
    key_column: str,
    *,
    binding=None,
) -> dict:
    """Tool: applies the deterministic fix for incident['signature'] and reloads clean data.

    `binding` selects the estate whose credentials the reload connects
    with; None keeps the process-global environment path used by the
    standalone recovery scripts.
    """
    defect_type = incident["signature"].split(":", 1)[0]
    fix_description = _KNOWN_FIXES.get(defect_type, "reload via tools/migration_executor.py")

    manifest = execute_migration(
        run_id=run_id,
        source_schema=source_schema,
        source_table=source_table,
        target_table=target_table,
        key_column=key_column,
        drop_fraction=0.0,
        binding=binding,
    )

    client = get_client()
    client.collection(RUN_COLLECTION).document(run_id).collection("incidents").document(
        incident["incident_id"]
    ).update({"fix": fix_description})

    logger.info("remediate: %s -> %s", incident["signature"], manifest)
    return manifest


def close_incident(run_id: str, incident: dict, resolved: bool) -> None:
    """Closes the incident and, if resolved, confirms the fact into memory_bank
    so a future run with the same signature can recall it (§21.2 proof 3)."""
    client = get_client()
    incident_ref = (
        client.collection(RUN_COLLECTION)
        .document(run_id)
        .collection("incidents")
        .document(incident["incident_id"])
    )
    outcome = "RESOLVED" if resolved else "UNRESOLVED"
    incident_ref.update({"outcome": outcome})

    if resolved:
        defect_type = incident["signature"].split(":", 1)[0]
        fix_description = _KNOWN_FIXES.get(defect_type, incident.get("fix") or "reload cleanly")
        # canonical_root_cause (not root_cause) — the stable fact text,
        # never the "Recalled from memory (...)" display wrapper, so
        # re-confirming an already-recalled fact doesn't nest an extra
        # wrapper layer into memory on every generation.
        memory_bank.record(
            signature=incident["signature"],
            root_cause=incident.get("canonical_root_cause", incident["root_cause"]),
            fix=fix_description,
            source_run_id=run_id,
        )
        logger.info("close_incident: RESOLVED, confirmed into memory_bank: %s", incident["signature"])
