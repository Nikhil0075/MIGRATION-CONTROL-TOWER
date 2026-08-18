"""Contract and authorization coverage for the production console API."""

from __future__ import annotations

import re

import firebase_admin
import pytest
from fastapi.testclient import TestClient

from frontend.app import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _claims(monkeypatch: pytest.MonkeyPatch, *roles: str) -> None:
    from firebase_admin import auth

    # Authentication itself is covered by the existing Firebase integration
    # tests. API contract tests supply verified-token output at that boundary.
    monkeypatch.delenv("APPROVER_ALLOWLIST", raising=False)
    monkeypatch.setattr(firebase_admin, "_apps", {"test": object()})
    monkeypatch.setattr(
        auth,
        "verify_id_token",
        lambda _token: {
            "uid": "contract-user",
            "email": "contract-user@example.internal",
            "roles": list(roles),
        },
    )


def _headers(**extra: str) -> dict[str, str]:
    return {"Authorization": "Bearer verified-contract-token", **extra}


def test_runtime_config_is_public_and_versioned(client: TestClient) -> None:
    response = client.get("/api/v1/config")
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["product_name"] == "Migration Control Tower"
    assert body["meta"]["freshness"] == "live"


def test_v1_data_requires_authentication(client: TestClient) -> None:
    response = client.get("/api/v1/runs")
    assert response.status_code == 401


def test_session_derives_roles_from_custom_claims(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _claims(monkeypatch, "operator")
    response = client.get("/api/v1/session", headers=_headers())
    assert response.status_code == 200
    assert response.json()["data"]["roles"] == ["operator", "viewer"]
    assert response.json()["data"]["wildcard_roles"] == ["operator", "viewer"]
    assert response.json()["data"]["estate_roles"]["*"] == ["operator", "viewer"]


def test_progress_uses_durable_milestones_not_elapsed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    from frontend.api_v1 import _run_progress

    monkeypatch.setattr("frontend.api_v1._collection_docs", lambda *_args, **_kwargs: [])
    progress = _run_progress({
        "run_id": "run_progress",
        "state": "READY_FOR_APPROVAL",
        "created_at": "2020-01-01T00:00:00+00:00",
        "last_transition_at": "2026-08-18T00:00:00+00:00",
    })
    assert progress["percent"] == 67
    assert progress["status"] == "waiting"
    assert progress["current_stage"] == "READY_FOR_APPROVAL"


def test_assessment_progress_reaches_100_at_planned(monkeypatch: pytest.MonkeyPatch) -> None:
    from frontend.api_v1 import _run_progress

    monkeypatch.setattr("frontend.api_v1._collection_docs", lambda *_args, **_kwargs: [])
    progress = _run_progress({"run_id": "assessment_1", "mode": "assessment", "state": "PLANNED"})
    assert progress["percent"] == 100
    assert progress["status"] == "complete"


def test_validation_failure_holds_at_validation_milestone(monkeypatch: pytest.MonkeyPatch) -> None:
    from frontend.api_v1 import _run_progress

    monkeypatch.setattr("frontend.api_v1._collection_docs", lambda *_args, **_kwargs: [])
    progress = _run_progress({"run_id": "run_failed", "state": "FAILED"})
    assert progress["percent"] == 50
    assert progress["status"] == "failed"


def test_viewer_cannot_start_assessment(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _claims(monkeypatch, "viewer")
    response = client.post(
        "/api/v1/assessments",
        headers=_headers(**{"Idempotency-Key": "contract-viewer-denial"}),
        json={"pack_id": "wwi_sqlserver_v1", "justification": "Contract authorization check"},
    )
    assert response.status_code == 403


def test_authenticated_user_without_claims_cannot_read_console(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claims(monkeypatch)
    response = client.get("/api/v1/runs", headers=_headers())
    assert response.status_code == 403


def test_operator_action_requires_idempotency_key(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _claims(monkeypatch, "operator")
    response = client.post(
        "/api/v1/assessments",
        headers=_headers(),
        json={"pack_id": "wwi_sqlserver_v1", "justification": "Contract header check"},
    )
    assert response.status_code == 422


def test_operator_action_publishes_durable_request_contract(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claims(monkeypatch, "operator")
    captured: dict = {}

    def fake_queue_operation(**kwargs):
        captured.update(kwargs)
        return {"operation_id": "op_contract", "status": "published", "message_id": "message-1"}

    monkeypatch.setattr("frontend.api_v1.queue_operation", fake_queue_operation)
    response = client.post(
        "/api/v1/assessments",
        headers=_headers(**{"Idempotency-Key": "contract-assessment-001"}),
        json={"pack_id": "wwi_sqlserver_v1", "justification": "Validate the selected migration pack"},
    )
    assert response.status_code == 202
    assert response.json()["data"]["operation_id"] == "op_contract"
    assert captured["topic"] == "assessment.requested"
    assert captured["idempotency_key"] == "contract-assessment-001"
    assert captured["event"]["pack_id"] == "wwi_sqlserver_v1"


def test_invalid_cursor_fails_closed(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _claims(monkeypatch, "viewer")
    monkeypatch.setattr("frontend.api_v1._all_runs", lambda _limit=500, estate_id=None: [])
    response = client.get("/api/v1/runs?cursor=not-a-cursor", headers=_headers())
    assert response.status_code == 400


def test_new_shell_csp_only_allows_the_firebase_popup_script_origin(client: TestClient) -> None:
    csp = client.get("/api/v1/config").headers["Content-Security-Policy"]
    script_directive = next(part for part in csp.split(";") if part.strip().startswith("script-src"))
    assert "unpkg.com" not in script_directive
    assert "gstatic.com" not in script_directive
    assert "https://apis.google.com" in script_directive


def test_legacy_shell_retains_temporary_compatibility_csp(client: TestClient) -> None:
    csp = client.get("/legacy").headers["Content-Security-Policy"]
    assert "https://unpkg.com" in csp


def test_client_deep_link_returns_release_shell(client: TestClient) -> None:
    response = client.get("/system-health")
    assert response.status_code == 200
    assert "Migration Control Tower" in response.text
    assert response.headers["Cache-Control"] == "no-cache"


def test_fingerprinted_release_assets_are_immutable(client: TestClient) -> None:
    index = client.get("/overview")
    asset = re.search(r'src="([^"]+\.[0-9a-f]{8,}\.[^"]+)"', index.text)
    assert asset, "The Oracle JET release build must emit fingerprinted JavaScript."
    response = client.get("/" + asset.group(1).lstrip("/"))
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_release_assets_are_referenced_absolutely(client: TestClient) -> None:
    """Asset URLs must be origin-absolute, not relative to the current route.

    With relative paths a nested deep link like /estates/new resolves
    "js/main.<hash>.js" against /estates/, hits the SPA fallback, receives
    index.html, and the application never boots — while every
    single-segment route keeps working, so the breakage is invisible until
    the first nested route ships. Fixed by webpack's output.publicPath in
    frontend/client/ojet.config.js; asserted here because a future build
    config change could silently undo it.
    """
    index = client.get("/overview")
    references = re.findall(r'(?:src|href)="([^"]+\.[0-9a-f]{8,}\.[^"]+)"', index.text)
    assert references, "The release build must emit fingerprinted assets."
    relative = [ref for ref in references if not ref.startswith("/")]
    assert not relative, (
        f"these release assets are referenced relatively and will 404 on a nested "
        f"route: {relative}"
    )


# --- Estate scoping of read endpoints (Day 11 Phase 4) -------------------


def test_estate_filter_treats_a_missing_estate_id_as_the_default_estate():
    """Runs created before Phase 2 carry no estate_id. Treating them as
    unmatched would empty the dashboard between deploying the filter and
    running scripts/backfill_estate_id.py."""
    from frontend.api_v1 import _for_estate
    from tools.connection_context import DEFAULT_ESTATE_ID

    records = [
        {"run_id": "legacy"},                                # no estate_id at all
        {"run_id": "explicit", "estate_id": DEFAULT_ESTATE_ID},
        {"run_id": "other", "estate_id": "acme-legacy"},
    ]
    kept = {r["run_id"] for r in _for_estate(records, DEFAULT_ESTATE_ID)}
    assert kept == {"legacy", "explicit"}


def test_estate_filter_excludes_other_estates():
    from frontend.api_v1 import _for_estate

    records = [{"run_id": "a", "estate_id": "acme"}, {"run_id": "b", "estate_id": "other"}]
    assert [r["run_id"] for r in _for_estate(records, "acme")] == ["a"]


def test_no_estate_filter_returns_everything():
    from frontend.api_v1 import _for_estate

    records = [{"run_id": "a", "estate_id": "acme"}, {"run_id": "b"}]
    assert len(_for_estate(records, None)) == 2


def test_wave_state_merges_estates_when_unfiltered(monkeypatch):
    """Picking one estate's document arbitrarily would under-report running
    work as soon as a second estate existed."""
    from frontend import api_v1

    class _Snap:
        def __init__(self, doc_id, data):
            self.id = doc_id
            self._data = data

        def to_dict(self):
            return self._data

    class _Collection:
        def stream(self):
            return [
                _Snap("estate-a", {"running_by_source": {"src": ["a-1"]}, "running_critical": ["a-1"]}),
                _Snap("estate-b", {"running_by_source": {"src": ["b-1"]}, "running_critical": []}),
            ]

    monkeypatch.setattr(api_v1, "get_client", lambda: type("C", (), {"collection": lambda self, name: _Collection()})())

    state = api_v1._wave_state(None)
    assert set(state["running_by_source"]) == {"estate-a:src", "estate-b:src"}
    assert state["running_critical"] == ["a-1"]


def test_catalog_system_is_resolved_from_the_adapter_not_a_hardcoded_map():
    """system ("sqlserver-wwi") and source_id ("wwi-sqlserver") were never
    equal; the adapter declares the mapping so historical catalog records
    stay matchable."""
    from frontend.api_v1 import _catalog_system_for

    assert _catalog_system_for({"adapter": "sqlserver", "source_id": "wwi-sqlserver"}) == "sqlserver-wwi"
    assert _catalog_system_for({"adapter": "oracle_corpus", "source_id": "oracle-corpus"}) == "oracle-corpus"


def test_unknown_adapter_falls_back_to_the_source_id():
    from frontend.api_v1 import _catalog_system_for

    assert _catalog_system_for({"adapter": "brand-new", "source_id": "acme-src"}) == "acme-src"


# --- Theme integrity (Day 11 Phase 9) ------------------------------------


def test_released_css_contains_no_stray_byte_order_mark():
    """A BOM anywhere but a file's start silently invalidates the rule it
    prefixes.

    This shipped: the bundler emitted one at the seam between the JET theme
    and app.css, so the selector became "﻿:root" and matched nothing.
    Every --mct-* custom property was undefined, `background: var(--mct-red)`
    computed to transparent, and the literal `color: #fff` still applied —
    the "Sign in with Google" button rendered white on a white card. It was
    present, focusable and 376x34px, so nothing failed; it was simply
    invisible.

    No component or browser test caught it, because the DOM was correct.
    Only the bytes were wrong.
    """
    from pathlib import Path

    web = Path(__file__).resolve().parents[1] / "frontend" / "client" / "web"
    stylesheets = list(web.glob("styles/*.css"))
    assert stylesheets, "no release stylesheet found — run `npm run build` first"
    for sheet in stylesheets:
        raw = sheet.read_bytes()
        assert b"\xef\xbb\xbf" not in raw, (
            f"{sheet.name} contains a U+FEFF byte-order mark; any CSS rule it "
            f"prefixes will silently never match."
        )


def test_released_css_defines_the_theme_variables():
    """The :root custom properties must survive the build.

    Asserted on the built artifact rather than the source, because the
    source was correct the whole time the UI was broken.
    """
    import re
    from pathlib import Path

    web = Path(__file__).resolve().parents[1] / "frontend" / "client" / "web"
    css = "\n".join(p.read_text(encoding="utf-8") for p in web.glob("styles/*.css"))
    match = re.search(r"(?<![\w\﻿]):root\s*\{([^}]*)\}", css)
    assert match, "no usable :root rule in the release stylesheet"
    for variable in ("--mct-red", "--mct-ink", "--mct-surface", "--mct-canvas"):
        assert variable in match.group(1), f"{variable} missing from :root"
