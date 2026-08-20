"""Contract and authorization coverage for the production console API."""

from __future__ import annotations

import re
import threading

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


def test_a_slow_read_cannot_repopulate_cache_after_a_write(monkeypatch: pytest.MonkeyPatch) -> None:
    from frontend import api_v1

    monkeypatch.setattr(api_v1, "_CACHE_TTL_SECONDS", 60)
    api_v1.clear_response_cache()
    started = threading.Event()
    release = threading.Event()

    def slow_old_read():
        started.set()
        release.wait(timeout=2)
        return {"data": ["pre-write"], "meta": {"freshness": "live"}}

    thread = threading.Thread(target=lambda: api_v1._cached("estates", slow_old_read))
    thread.start()
    assert started.wait(timeout=2)
    api_v1.clear_response_cache()
    release.set()
    thread.join(timeout=2)

    assert "estates" not in api_v1._response_cache
    current = api_v1._cached(
        "estates", lambda: {"data": ["post-write"], "meta": {"freshness": "live"}}
    )
    assert current["data"] == ["post-write"]


def test_creating_an_estate_invalidates_the_estate_list_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frontend import api_v1

    _claims(monkeypatch, "operator")
    api_v1.clear_response_cache()
    api_v1._response_cache["estates:None:*"] = (
        float("inf"),
        {"data": [{"estate_id": "old"}], "meta": {"freshness": "live"}},
    )
    monkeypatch.setattr(
        "tools.estate_registry.create_estate",
        lambda document, **_kwargs: {**document, "status": "ACTIVE"},
    )
    monkeypatch.setattr(api_v1, "_record_estate_operation", lambda **_kwargs: None)
    monkeypatch.setattr(api_v1, "_sanitized_estate", lambda estate, _run: estate)

    response = client.post(
        "/api/v1/estates",
        headers=_headers(**{"Idempotency-Key": "estate-cache-regression"}),
        json={
            "estate_id": "new-estate",
            "display_name": "New estate",
            "sources": [{"source_id": "new-sql", "adapter": "sqlserver"}],
            "justification": "Verify immediate estate visibility",
        },
    )

    assert response.status_code == 201
    assert api_v1._response_cache == {}


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


def test_migrating_progress_counts_plan_targets_and_completed_manifests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from frontend.api_v1 import _run_progress

    def docs(_run_id, name, *_args):
        if name == "migration_plan":
            return [{"targets": [
                {"target_id": "one", "scheduled": True, "blocked": False},
                {"target_id": "two", "scheduled": True, "blocked": False},
            ]}]
        return [{"target_id": "one", "status": "COMPLETED"}]

    monkeypatch.setattr("frontend.api_v1._collection_docs", docs)
    progress = _run_progress({"run_id": "run_migrating", "state": "MIGRATING"})
    # MIGRATING is 5/12 milestones, plus half of the next measured segment.
    assert progress["percent"] == 46


def _execution_fixture(monkeypatch: pytest.MonkeyPatch, *, sources=None, executable=True):
    sources = sources or [{
        "source_id": "primary-sql",
        "adapter": "sqlserver",
        "pack_id": "wwi_sqlserver_v1",
    }]
    monkeypatch.setattr(
        "frontend.api_v1._estate",
        lambda estate_id=None: {"estate_id": estate_id or "estate-one", "sources": sources},
    )
    monkeypatch.setattr(
        "frontend.api_v1._packs",
        lambda: [{
            "pack_id": "wwi_sqlserver_v1",
            "name": "WWI",
            "source_id": "wwi-sqlserver",
            "estate_file": "simulator/source_setup/estate.yaml",
            "execution_supported": executable,
        }],
    )
    monkeypatch.setattr("tools.pack_loader.adapter_type_for", lambda _pack: "sqlserver")


def test_pack_driven_start_needs_no_discovered_pipeline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claims(monkeypatch, "operator")
    _execution_fixture(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "frontend.api_v1.queue_operation",
        lambda **kwargs: captured.update(kwargs) or {"operation_id": "op_pack", "status": "published"},
    )
    response = client.post(
        "/api/v1/runs",
        headers=_headers(**{"Idempotency-Key": "pack-driven-start-001"}),
        json={
            "estate_id": "estate-one",
            "source_id": "primary-sql",
            "pack_id": "wwi_sqlserver_v1",
            "justification": "Execute the selected migration pack",
        },
    )
    assert response.status_code == 202
    assert captured["event"] == {
        "pipeline_id": "wwi_sqlserver_v1",
        "source_id": "primary-sql",
        "pack_id": "wwi_sqlserver_v1",
        "estate_id": "estate-one",
        "drop_fraction": 0.0,
    }


def test_pack_and_legacy_alias_conflict_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claims(monkeypatch, "operator")
    response = client.post(
        "/api/v1/runs",
        headers=_headers(**{"Idempotency-Key": "pack-alias-conflict-001"}),
        json={
            "pack_id": "wwi_sqlserver_v1",
            "execution_profile": "another_pack",
            "justification": "Reject conflicting compatibility fields",
        },
    )
    assert response.status_code == 422
    assert "must match" in response.json()["detail"]


def test_source_is_resolved_only_when_pack_binding_is_unique(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claims(monkeypatch, "operator")
    _execution_fixture(monkeypatch, sources=[
        {"source_id": "one", "adapter": "sqlserver", "pack_id": "wwi_sqlserver_v1"},
        {"source_id": "two", "adapter": "sqlserver", "pack_id": "wwi_sqlserver_v1"},
    ])
    response = client.post(
        "/api/v1/runs",
        headers=_headers(**{"Idempotency-Key": "pack-ambiguous-source-001"}),
        json={
            "estate_id": "estate-one",
            "pack_id": "wwi_sqlserver_v1",
            "justification": "Refuse ambiguous source selection",
        },
    )
    assert response.status_code == 422
    assert "Several sources" in response.json()["detail"]


def test_assessment_only_pack_is_rejected_before_publish(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _claims(monkeypatch, "operator")
    _execution_fixture(monkeypatch, executable=False)
    response = client.post(
        "/api/v1/runs",
        headers=_headers(**{"Idempotency-Key": "assessment-pack-execution-001"}),
        json={
            "estate_id": "estate-one",
            "source_id": "primary-sql",
            "pack_id": "wwi_sqlserver_v1",
            "justification": "Reject an assessment only pack",
        },
    )
    assert response.status_code == 422
    assert "assessment only" in response.json()["detail"]


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


def test_local_redwood_2013_theme_is_loaded_and_immutable(client: TestClient) -> None:
    index = client.get("/overview")
    theme = "/styles/redwood/20.1.3/web/redwood.min.css"
    assert theme in index.text
    response = client.get(theme)
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert "oj-theme-json" in response.text
    assert 'jetReleaseVersion":"v20.1.3' in response.text


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


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/assessments", {"pack_id": "wwi_sqlserver_v1"}),
        ("post", "/api/v1/runs", {"source_id": "wwi-sqlserver", "pack_id": "wwi_sqlserver_v1"}),
        ("put", "/api/v1/waves/wwi-demo-estate:wwi-sqlserver/override", {"state": "HOLD"}),
    ],
)
def test_an_explicit_null_estate_is_refused_with_a_reason_an_operator_can_act_on(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: dict
) -> None:
    """`null` is not `absent`, and the difference was invisible to the operator.

    A pydantic field default applies only when the KEY IS MISSING, so a
    body carrying `"estate_id": null` skipped the default and failed type
    validation with "Input should be a valid string". The console sends
    exactly that shape — three forms post the active estate, which is
    null until /api/v1/estates resolves — so the most likely first click
    after a cold load produced a 422 that named a type, not a fix.
    """
    _claims(monkeypatch, "operator")
    monkeypatch.setattr(
        "frontend.api_v1.queue_operation",
        lambda **_kwargs: {"operation_id": "op", "status": "published", "message_id": "m"},
    )
    response = getattr(client, method)(
        path,
        headers=_headers(**{"Idempotency-Key": f"null-estate-{path}"}),
        json={**body, "estate_id": None, "justification": "Checking the null estate path"},
    )
    assert response.status_code == 422
    message = " ".join(error["msg"] for error in response.json()["detail"])
    assert "No estate is selected" in message
    assert "estate_id" in message
    # The old failure said this and nothing else. It is a type complaint,
    # not an instruction.
    assert "Input should be a valid string" not in message


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/v1/assessments", {"pack_id": "wwi_sqlserver_v1"}),
        ("put", "/api/v1/waves/wwi-demo-estate:wwi-sqlserver/override", {"state": "HOLD"}),
    ],
)
def test_omitting_the_estate_still_falls_back_to_the_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: dict
) -> None:
    """The compatibility default is for callers that leave the field out.

    Refusing an explicit null must not also break a caller that never
    sent the field — that is a different contract, and existing callers
    depend on it.
    """
    _claims(monkeypatch, "operator")
    captured: dict = {}

    def fake_queue_operation(**kwargs):
        captured.update(kwargs)
        return {"operation_id": "op", "status": "published", "message_id": "m"}

    monkeypatch.setattr("frontend.api_v1.queue_operation", fake_queue_operation)
    monkeypatch.setattr("frontend.api_v1.record_wave_override", lambda **_kwargs: {})
    response = getattr(client, method)(
        path,
        headers=_headers(**{"Idempotency-Key": f"absent-estate-{path}"}),
        json={**body, "justification": "Checking the absent estate path"},
    )
    assert response.status_code == 202
