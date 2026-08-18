"""Fast, cheap first-pass sensitivity screen (Block C, master doc §22.3).

    Use it as a cheap, self-hosted first-pass classifier for sensitive
    field names, running ahead of the main risk agent. [...] Disagreement
    between the two models is itself recorded as a signal.

**Documented Rung-2 substitution, not a silent shortcut**: §22.3 asks
for a self-hosted Gemma model. Ollama is installed on this development
machine but no model has been pulled (confirmed with the user rather
than downloading one unasked — see this build day's notes). This module
fills Gemma's *architectural role* — a cheap, broad, low-precision first
pass, independent of the careful classifier — using a deterministic
keyword screen instead of a second model call. The role it plays in the
pipeline (cheap wide screen -> disagreement escalation -> expensive
narrow reasoner) is real and unchanged; only the implementation of the
cheap screen is a stand-in. Swapping in a real Ollama-hosted Gemma call
later only touches `fast_screen_table()`.

Deliberately NOT the same rule set as tools/data_classifier.py: the
"careful pass" uses precise multi-word regex; this screen uses bare
substring matching, on purpose, so it's both cheaper AND less precise —
producing genuine disagreements in both directions (this screen flags
things the careful pass clears, e.g. 'CustomerID' via its 'id'
substring; and it can also miss things the careful pass would classify
as PII, e.g. a column matched only by a precise pattern this screen's
marker list doesn't include, such as 'passport' or 'iban'). Both
directions are recorded, never silently resolved to whichever is more
convenient.
"""

from __future__ import annotations

from tools.data_classifier import classify_table

# Deliberately broad/naive — bare substring match, no word boundaries,
# no regex. Cheap and imprecise by design (the architectural point of
# the Gemma-style first pass), not a bug.
FAST_SCREEN_MARKERS = [
    "email", "phone", "mobile", "fax", "name", "address", "street",
    "postal", "zip", "dob", "birth", "salary", "account", "credit", "id",
    "contact",
]


def fast_screen_table(table_record: dict) -> set[str]:
    """Returns column names this cheap screen flags as sensitivity candidates."""
    return {
        c["name"]
        for c in table_record.get("columns", [])
        if any(marker in c["name"].lower() for marker in FAST_SCREEN_MARKERS)
    }


def compare_screens(table_record: dict) -> list[dict]:
    """Returns SENSITIVITY_SCREEN_DISAGREEMENT RiskFindings (contracts/
    metadata_model.json) wherever the fast screen and the careful
    classifier (tools/data_classifier.py) disagree. Both directions are
    reported — over-flagging (fast screen wrong) and under-flagging
    (fast screen missed real PII, the more concerning direction).
    """
    fast_flagged = fast_screen_table(table_record)
    _, careful_matches = classify_table(table_record)
    careful_flagged = {m["column"] for m in careful_matches}

    table_id = table_record["table_id"]
    findings: list[dict] = []

    for column in sorted(fast_flagged - careful_flagged):
        findings.append(
            {
                "finding_type": "SENSITIVITY_SCREEN_DISAGREEMENT",
                "table_id": table_id,
                "severity": "LOW",
                "detail": {
                    "column": column,
                    "fast_screen": "FLAGGED",
                    "careful_pass": "CLEARED",
                    "explanation": "cheap screen over-flagged; careful classifier found no PII pattern — "
                    "likely a false positive, escalated for human review rather than silently dropped",
                },
            }
        )

    for column in sorted(careful_flagged - fast_flagged):
        findings.append(
            {
                "finding_type": "SENSITIVITY_SCREEN_DISAGREEMENT",
                "table_id": table_id,
                "severity": "MEDIUM",
                "detail": {
                    "column": column,
                    "fast_screen": "CLEARED",
                    "careful_pass": "FLAGGED",
                    "explanation": "cheap screen missed a column the careful classifier confirms as PII — "
                    "the more concerning disagreement direction, escalated for human review",
                },
            }
        )

    return findings
