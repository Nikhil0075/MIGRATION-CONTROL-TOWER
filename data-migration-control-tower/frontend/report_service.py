"""Immutable, private evidence reports for the operator console."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import logging
import os
import uuid
from typing import Literal
from xml.sax.saxutils import escape

from google.cloud import firestore
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from tools.agent_audit import sanitize
from tools.firestore_client import get_client

ReportType = Literal["assessment", "run_evidence", "reconciliation", "approval_audit"]
REPORT_COLLECTION = "report_artifacts"
logger = logging.getLogger("report_service")
_FIRESTORE_CHUNK_BYTES = 350_000
_SUBCOLLECTIONS = {
    "assessment": ["catalog", "pipelines", "dependencies", "risk_findings", "migration_plan", "agent_execution_events"],
    "run_evidence": [
        "catalog", "pipelines", "dependencies", "risk_findings", "migration_plan", "migration_executions",
        "reconciliation", "incidents", "policy_decisions", "approval_history", "stage_metrics",
        "usage_events", "agent_execution_events",
    ],
    "reconciliation": ["reconciliation", "migration_executions", "stage_metrics", "agent_execution_events"],
    "approval_audit": ["policy_decisions", "approval_history", "agent_execution_events", "migration_plan"],
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class ReportQuotaExceeded(RuntimeError):
    """Raised by consume_report_quota() when a user has requested too
    many reports today (Deploy & Harden Phase 5) — mirrors
    frontend/assistant_service.py::_consume_quota()'s proven Firestore-
    transaction pattern (same "read count, refuse or increment, all in
    one transaction" shape), since PDF/JSON report generation is real
    compute + storage cost per request, same as an assistant message,
    and had no rate limit at all before this."""


def consume_report_quota(uid: str) -> None:
    """Atomically checks and increments uid's daily report-generation
    count; raises ReportQuotaExceeded once REPORT_DAILY_LIMIT is hit.
    Call this BEFORE queuing a report — not after — so a user who has
    exhausted their quota never gets a "queued" response that then
    silently never generates.
    """
    client = get_client()
    key = f"{uid}_{dt.datetime.now(dt.timezone.utc).date().isoformat()}"
    ref = client.collection("report_daily_usage").document(key)
    limit = int(os.environ.get("REPORT_DAILY_LIMIT", "20"))

    @firestore.transactional
    def increment(transaction):
        snapshot = ref.get(transaction=transaction)
        count = int((snapshot.to_dict() or {}).get("count", 0)) if snapshot.exists else 0
        if count >= limit:
            raise ReportQuotaExceeded(
                f"Daily report generation limit ({limit}) reached for this user. Try again tomorrow."
            )
        transaction.set(
            ref,
            {
                "uid": uid,
                "date": dt.datetime.now(dt.timezone.utc).date().isoformat(),
                "count": count + 1,
                # Matches assistant_service.py's own 2-day TTL reasoning —
                # long enough to be inspectable the day after, short
                # enough not to accumulate forever.
                "expires_at": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2),
            },
            merge=True,
        )

    increment(client.transaction())


def _snapshot(run_id: str, report_type: ReportType) -> dict:
    client = get_client()
    run_ref = client.collection("migration_runs").document(run_id)
    run_doc = run_ref.get()
    if not run_doc.exists:
        raise KeyError(run_id)
    run = run_doc.to_dict() or {}
    evidence: dict[str, list[dict]] = {}
    for name in _SUBCOLLECTIONS[report_type]:
        evidence[name] = [
            {"id": doc.id, **(doc.to_dict() or {})}
            for doc in list(run_ref.collection(name).stream())[:500]
        ]
    return sanitize({
        "report_type": report_type,
        "generated_at": _now(),
        "run": run,
        "evidence": evidence,
        "disclaimer": "Generated from persisted control-tower evidence. Credentials and raw source data are excluded.",
    })


def _source_revision(snapshot: dict) -> str:
    run = snapshot.get("run") or {}
    plan_rows = (snapshot.get("evidence") or {}).get("migration_plan") or []
    plan_hash = next((row.get("plan_hash") for row in plan_rows if row.get("plan_hash")), None)
    return str(run.get("revision") or plan_hash or run.get("updated_at") or run.get("created_at") or "unavailable")


def _persist_snapshot_chunks(report_ref, json_bytes: bytes, digest: str) -> int:
    """Persist the immutable sanitized snapshot without hitting Firestore's 1 MiB document limit."""
    chunks = [
        json_bytes[offset:offset + _FIRESTORE_CHUNK_BYTES]
        for offset in range(0, len(json_bytes), _FIRESTORE_CHUNK_BYTES)
    ] or [b""]
    for index, chunk in enumerate(chunks):
        chunk_ref = report_ref.collection("evidence_chunks").document(f"{digest}-{index:05d}")
        record = {
            "evidence_hash": digest,
            "index": index,
            "count": len(chunks),
            "encoding": "utf-8-json",
            "data": chunk,
        }
        try:
            chunk_ref.create(record)
        except Exception as exc:
            if type(exc).__name__ != "AlreadyExists":
                raise
            existing = chunk_ref.get().to_dict() or {}
            if existing.get("evidence_hash") != digest or existing.get("data") != chunk:
                raise RuntimeError("Immutable Firestore report evidence chunk conflicts with existing content") from exc
    return len(chunks)


def _pdf(snapshot: dict, digest: str) -> bytes:
    buffer = io.BytesIO()
    styles = getSampleStyleSheet()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, rightMargin=16 * mm, leftMargin=16 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Migration Control Tower — {snapshot['report_type']}",
    )
    story = [
        Paragraph("Migration Control Tower", styles["Title"]),
        Paragraph(str(snapshot["report_type"]).replace("_", " ").title(), styles["Heading1"]),
        Paragraph(f"Evidence hash: {digest}", styles["Code"]),
        Paragraph(f"Generated: {snapshot['generated_at']}", styles["Normal"]),
        Spacer(1, 8),
    ]
    run = snapshot.get("run") or {}
    facts = [
        ["Run", run.get("run_id", "Not available")],
        ["Estate", run.get("estate_id", "Not available")],
        ["State", run.get("state", "Not available")],
        ["Mode", run.get("mode", "Not available")],
        ["Pack", run.get("pack_id", "Not available")],
        ["Plan hash", ((run.get("plan_hash") or "Not available"))],
    ]
    table = Table(facts, colWidths=[36 * mm, 130 * mm])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f1efeb")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbc7c0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
    ]))
    story.extend([table, Spacer(1, 12)])
    for collection, rows in (snapshot.get("evidence") or {}).items():
        story.append(Paragraph(collection.replace("_", " ").title(), styles["Heading2"]))
        story.append(Paragraph(f"{len(rows)} persisted record(s)", styles["Normal"]))
        for row in rows[:100]:
            summary = json.dumps(row, sort_keys=True, default=str)
            story.append(Paragraph(escape(summary[:1800]), styles["Code"]))
            story.append(Spacer(1, 3))
        if len(rows) > 100:
            story.append(Paragraph(f"{len(rows) - 100} additional records are present in the JSON evidence.", styles["Italic"]))
        story.append(PageBreak())
    story.append(Paragraph(snapshot["disclaimer"], styles["Italic"]))
    def invariant_canvas(*args, **kwargs):
        # ReportLab otherwise embeds the wall-clock build time and a random
        # document id, producing different PDF hashes from identical evidence.
        kwargs["invariant"] = 1
        return canvas.Canvas(*args, **kwargs)

    document.build(story, canvasmaker=invariant_canvas)
    return buffer.getvalue()


def generate(report_id: str) -> dict:
    client = get_client()
    ref = client.collection(REPORT_COLLECTION).document(report_id)
    request = ref.get().to_dict() or {}
    try:
        ref.set({"status": "generating", "progress": {"percent": 10, "stage": "snapshotting_evidence"}}, merge=True)
        snapshot = _snapshot(request["run_id"], request["report_type"])
        json_bytes = json.dumps(snapshot, sort_keys=True, indent=2, default=str).encode()
        digest = hashlib.sha256(json_bytes).hexdigest()
        snapshot_chunks = _persist_snapshot_chunks(ref, json_bytes, digest)
        ref.set({"progress": {"percent": 40, "stage": "rendering_pdf"}}, merge=True)
        pdf_bytes = _pdf(snapshot, digest)
        bucket_name = os.environ.get("REPORTS_BUCKET", "").strip()
        if not bucket_name:
            raise RuntimeError("REPORTS_BUCKET is not configured")
        from google.cloud import storage

        bucket = storage.Client(project=os.environ.get("GCP_PROJECT_ID")).bucket(bucket_name)
        prefix = f"reports/{request['estate_id']}/{request['run_id']}/{request['report_type']}/{digest}"
        objects = {"pdf": f"{prefix}.pdf", "json": f"{prefix}.json"}
        for index, (kind, data, content_type) in enumerate((
            ("pdf", pdf_bytes, "application/pdf"), ("json", json_bytes, "application/json")
        )):
            blob = bucket.blob(objects[kind])
            if not blob.exists():
                blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
            ref.set({"progress": {"percent": 70 + index * 20, "stage": f"storing_{kind}"}}, merge=True)
        ready = {
            "status": "ready", "evidence_hash": digest, "objects": objects,
            "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(), "pdf_size": len(pdf_bytes),
            "json_sha256": digest, "json_size": len(json_bytes), "completed_at": _now(),
            "source_revision": _source_revision(snapshot),
            "generator_identity": os.environ.get("REPORT_GENERATOR_IDENTITY", os.environ.get("K_SERVICE", "control-tower-report-service")),
            "model_sections": [
                {
                    "event_id": event.get("event_id"),
                    "agent_id": event.get("agent_id"),
                    "model": event.get("model"),
                    "prompt_template_hash": event.get("prompt_template_hash"),
                }
                for event in (snapshot.get("evidence") or {}).get("agent_execution_events", [])
                if event.get("model") and event.get("status") == "COMPLETED"
            ],
            "progress": {"percent": 100, "stage": "complete"},
            "snapshot_storage": {
                "collection": "evidence_chunks",
                "chunk_count": snapshot_chunks,
                "encoding": "utf-8-json",
                "sha256": digest,
            },
            "snapshot_summary": {
                "collections": {
                    name: len(rows) for name, rows in (snapshot.get("evidence") or {}).items()
                },
                "disclaimer": snapshot.get("disclaimer"),
            },
        }
        ref.set(ready, merge=True)
        client.collection("operation_audit").document(str(uuid.uuid4())).set({
            "operation_id": report_id,
            "kind": "report.generate",
            "estate_id": request.get("estate_id"),
            "run_id": request.get("run_id"),
            "report_type": request.get("report_type"),
            "event": "completed",
            "artifact_hashes": {"pdf": ready["pdf_sha256"], "json": ready["json_sha256"]},
            "recorded_at": ready["completed_at"],
        })
        return {**request, **ready}
    except Exception as exc:
        completed_at = _now()
        ref.set({
            "status": "failed", "error": str(exc)[:1000], "completed_at": completed_at,
            "progress": {"percent": 100, "stage": "failed"},
        }, merge=True)
        client.collection("operation_audit").document(str(uuid.uuid4())).set({
            "operation_id": report_id,
            "kind": "report.generate",
            "estate_id": request.get("estate_id"),
            "run_id": request.get("run_id"),
            "report_type": request.get("report_type"),
            "event": "failed",
            "error_type": type(exc).__name__,
            "recorded_at": completed_at,
        })
        raise


def generate_background(report_id: str) -> None:
    """Run after the 202 response; failure is persisted, not re-raised into ASGI."""
    try:
        generate(report_id)
    except Exception:
        logger.exception("Report generation failed for %s", report_id)


def download(metadata: dict, kind: Literal["pdf", "json"]) -> tuple[bytes, str, str]:
    bucket_name = os.environ.get("REPORTS_BUCKET", "").strip()
    if not bucket_name:
        raise RuntimeError("REPORTS_BUCKET is not configured")
    from google.cloud import storage

    object_name = (metadata.get("objects") or {}).get(kind)
    if not object_name:
        raise KeyError(kind)
    data = storage.Client(project=os.environ.get("GCP_PROJECT_ID")).bucket(bucket_name).blob(object_name).download_as_bytes()
    content_type = "application/pdf" if kind == "pdf" else "application/json"
    filename = f"migration-control-tower-{metadata['report_type']}-{metadata['run_id']}.{kind}"
    return data, content_type, filename
