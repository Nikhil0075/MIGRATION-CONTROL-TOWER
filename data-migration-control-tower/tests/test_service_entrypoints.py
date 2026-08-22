"""Import-and-route-shape checks for the per-service Cloud Run
entrypoints (agents/*/service_main.py, Deploy & Harden Phase 2b/2c).

These don't start a server or touch live GCP — they just prove each
entrypoint module imports cleanly (catches a wrong handler name/module
path before it becomes a broken container image) and mounts the routes
its docstring claims. The 9-service topology (docs/ARCHITECTURE.md) only
means something if every one of these actually builds a working app.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


CAPABILITY_ONLY_SERVICES = [
    ("agents.lineage.service_main", "lineage-agent"),
    ("agents.risk.service_main", "risk-agent"),
    ("agents.planner.service_main", "planner-agent"),
    ("agents.validation.service_main", "validation-agent"),
    ("agents.finance.service_main", "finance-impact-agent"),
]


@pytest.mark.parametrize("module_path,expected_service_name", CAPABILITY_ONLY_SERVICES)
def test_capability_only_service_imports_and_serves_invoke(module_path, expected_service_name):
    import importlib

    module = importlib.import_module(module_path)
    routes = {r.path for r in module.app.routes}
    assert "/invoke" in routes
    assert "/status" in routes
    assert "/push" not in "".join(routes)  # these services own no Pub/Sub consumer


@pytest.mark.parametrize(
    "module_path,mount_path",
    [
        ("agents.discovery.service_main", "/push/assessment"),
        ("agents.cutover.service_main", "/push/cutover"),
    ],
)
def test_combined_capability_and_consumer_service_mounts_both(module_path, mount_path):
    import importlib

    module = importlib.import_module(module_path)
    routes = {r.path for r in module.app.routes}
    assert "/invoke" in routes  # the capability route
    assert any(r.startswith(mount_path) for r in routes)  # the mounted push consumer


def test_orchestrator_service_mounts_all_seven_owned_consumers():
    import importlib

    module = importlib.import_module("agents.orchestrator.service_main")
    routes = {r.path for r in module.app.routes}
    for name in ("migration", "discovery", "risk", "plan", "validation", "approval", "recovery"):
        assert any(r.startswith(f"/push/{name}") for r in routes), f"missing /push/{name}"
    # The orchestrator dispatches to other agents via invoke_capability()
    # rather than serving a capability of its own — no /invoke route here.
    assert "/invoke" not in routes


def test_orchestrator_does_not_own_assessment_or_cutover_consumers():
    """Regression guard for the corrected topology (docs/ARCHITECTURE.md):
    a first draft put all 9 consumers on the orchestrator, double-counting
    Discovery's assessment consumer and Cutover's own consumer."""
    import importlib

    module = importlib.import_module("agents.orchestrator.service_main")
    routes = {r.path for r in module.app.routes}
    assert not any(r.startswith("/push/assessment") for r in routes)
    assert not any(r.startswith("/push/cutover") for r in routes)


#: Every consumer mount path this fleet actually has, per each
#: service_main.py's own `app.mount(f"/push/{name}", ...)`. Matches
#: infrastructure/terraform/pubsub.tf's push_targets map — kept in sync
#: by hand (that file's own comment explains why the trailing slash on
#: every path there is load-bearing, not cosmetic).
_ALL_PUSH_MOUNTS = [
    ("agents.orchestrator.service_main", "/push/migration/"),
    ("agents.orchestrator.service_main", "/push/discovery/"),
    ("agents.orchestrator.service_main", "/push/risk/"),
    ("agents.orchestrator.service_main", "/push/plan/"),
    ("agents.orchestrator.service_main", "/push/validation/"),
    ("agents.orchestrator.service_main", "/push/approval/"),
    ("agents.orchestrator.service_main", "/push/recovery/"),
    ("agents.orchestrator.service_main", "/push/migrationcompleted/"),
    ("agents.discovery.service_main", "/push/assessment/"),
    ("agents.cutover.service_main", "/push/cutover/"),
]


@pytest.mark.parametrize("module_path,mount_path", _ALL_PUSH_MOUNTS)
def test_push_mount_bare_path_redirects_but_trailing_slash_does_not(module_path, mount_path):
    """Regression test for a real bug found live (Deploy & Harden Phase 5
    close-out): `app.mount(f"/push/{name}", sub_app)` where sub_app has a
    route at "/" makes Starlette 307-redirect a POST to the BARE mount
    path (no trailing slash) before any handler or auth code runs. Every
    push subscription in pubsub.tf's push_targets was pointed at the bare
    path — every single live delivery attempt failed silently forever
    (Pub/Sub does not follow redirects), which
    tests/test_service_entrypoints.py's existing route-table checks
    never caught, since they never actually issue a request.

    This asserts BOTH halves: the bare path really does redirect (so this
    test documents the trap rather than silently stops testing for it if
    Starlette's default ever changes) and the trailing-slash path — the
    one pubsub.tf actually targets now — does not.
    """
    import importlib

    from fastapi.testclient import TestClient

    module = importlib.import_module(module_path)
    client = TestClient(module.app, follow_redirects=False)

    bare_path = mount_path.rstrip("/")
    bare_response = client.post(bare_path, json={})
    assert bare_response.status_code == 307, (
        f"{module_path}{bare_path} no longer redirects — if Starlette's mount "
        "behavior changed, the trailing slash in pubsub.tf's push_targets may "
        "no longer be necessary, but verify before removing it"
    )

    real_response = client.post(mount_path, json={})
    assert real_response.status_code != 307, (
        f"{module_path}{mount_path} — the exact path pubsub.tf's push subscription "
        "targets — redirects. Every live Pub/Sub push delivery to this consumer "
        "would fail silently and retry forever."
    )
