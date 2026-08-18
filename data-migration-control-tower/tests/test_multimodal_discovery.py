"""Tests for tools/multimodal_discovery.py — the deterministic drift diff
is a pure function, no live services needed. The extraction functions
fall back deterministically when Vertex AI isn't available (documented,
tested separately by checking extraction_method).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.multimodal_discovery import (  # noqa: E402
    compute_schema_drift,
    extract_documented_schema_from_image,
    extract_documented_schema_from_pdf,
)

ERD_PATH = REPO_ROOT / "simulator" / "documentation" / "erd_sales_customers.png"
PDF_PATH = REPO_ROOT / "simulator" / "documentation" / "data_dictionary_co_customers.pdf"


def test_erd_fixture_extraction_has_a_method_and_matches_generator():
    doc = extract_documented_schema_from_image(ERD_PATH)
    assert doc["extraction_method"] in ("gemini_vision", "deterministic_fallback")
    assert doc["table"] == "Sales.Customers"
    assert "PhoneNumber" in doc["columns"]


def test_pdf_fixture_extraction_has_a_method_and_matches_generator():
    doc = extract_documented_schema_from_pdf(PDF_PATH)
    assert doc["extraction_method"] in ("gemini_vision", "deterministic_fallback")
    assert doc["table"] == "CO.CUSTOMERS"
    assert "ACCOUNT_MGR_ID" in doc["columns"]


def test_unknown_fixture_raises_without_a_fallback():
    import pytest

    with pytest.raises(ValueError):
        extract_documented_schema_from_image("no_such_fixture.png")


def _documented(columns: dict) -> dict:
    return {"table": "T", "columns": columns, "source_artifact": "fixture.png", "extraction_method": "deterministic_fallback"}


def test_missing_in_actual_when_documented_column_absent():
    documented = _documented({"Ghost": {"data_type": "INT", "sensitivity": "METADATA"}})
    actual = {"table_id": "t1", "columns": []}
    findings = compute_schema_drift(documented, actual)
    assert findings[0]["finding_type"] == "MISSING_IN_ACTUAL"
    assert findings[0]["detail"]["column"] == "Ghost"


def test_type_divergence_when_base_types_differ():
    documented = _documented({"X": {"data_type": "FLOAT", "sensitivity": "METADATA"}})
    actual = {"table_id": "t1", "columns": [{"name": "X", "data_type": "decimal"}]}
    findings = compute_schema_drift(documented, actual)
    assert any(f["finding_type"] == "TYPE_DIVERGENCE" for f in findings)


def test_no_type_divergence_for_varchar_nvarchar_equivalence():
    documented = _documented({"X": {"data_type": "VARCHAR(50)", "sensitivity": "METADATA"}})
    actual = {"table_id": "t1", "columns": [{"name": "X", "data_type": "nvarchar"}]}
    findings = compute_schema_drift(documented, actual)
    assert not any(f["finding_type"] == "TYPE_DIVERGENCE" for f in findings)


def test_classification_gap_when_documentation_understates_pii():
    documented = _documented({"EmailAddress": {"data_type": "NVARCHAR(100)", "sensitivity": "PUBLIC"}})
    actual = {"table_id": "t1", "columns": [{"name": "EmailAddress", "data_type": "nvarchar"}]}
    findings = compute_schema_drift(documented, actual)
    gap = next(f for f in findings if f["finding_type"] == "CLASSIFICATION_GAP")
    assert gap["severity"] == "HIGH"
    assert gap["detail"]["actual_value"] == "PII"


def test_missing_in_documented_for_undocumented_real_column():
    documented = _documented({})
    actual = {"table_id": "t1", "columns": [{"name": "NationalID", "data_type": "varchar"}]}
    findings = compute_schema_drift(documented, actual)
    missing = next(f for f in findings if f["finding_type"] == "MISSING_IN_DOCUMENTED")
    assert missing["severity"] == "HIGH"  # NationalID classifies as PII, so this is the high-risk case


def test_every_finding_carries_traceable_evidence():
    documented = _documented({"Ghost": {"data_type": "INT", "sensitivity": "METADATA"}})
    actual = {"table_id": "t1", "columns": [{"name": "Extra", "data_type": "int"}]}
    findings = compute_schema_drift(documented, actual)
    assert len(findings) == 2  # Ghost -> MISSING_IN_ACTUAL, Extra -> MISSING_IN_DOCUMENTED
    for finding in findings:
        assert finding["detail"]["source_artifact"] == "fixture.png"
        assert "extracted_value" in finding["detail"]
        assert "actual_value" in finding["detail"]
