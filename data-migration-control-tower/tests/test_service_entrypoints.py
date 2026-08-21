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
