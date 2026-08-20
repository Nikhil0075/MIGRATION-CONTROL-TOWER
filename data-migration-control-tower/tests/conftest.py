"""Shared test setup (Day 11 Phase 0, master doc §32's portability model).

Three jobs, in order of how much trouble they save:

1. **Import preamble.** Every test module here repeats the same
   `REPO_ROOT / sys.path.insert / load_dotenv` block before it can import
   anything from `tools/`. pytest imports conftest.py before any test
   module in its directory, so doing it once here means a new test file
   starts with `from tools.x import y` and nothing else. Existing modules
   keep their own copy — it is idempotent, and rewriting 24 files to save
   four lines each is not worth the churn.

2. **One reachability probe per dependency, not nine.** Nine test modules
   currently define their own `_firestore_reachable()` and evaluate it at
   *import* time inside a `skipif`, so a full run opens nine separate
   Firestore connections before the first test executes. The probes here
   are cached and run at collection time via the `requires_*` markers
   below. Existing modules are deliberately left alone; new tests should
   use the markers.

3. **Teardown hygiene.** CLAUDE.md records this as a real, repeated
   failure: tests run against live Firestore, and a leaked run document
   becomes the Control Tower dashboard's "active run" while a leaked
   registry card can shadow a real capability lookup by wildcard.
   Multi-estate work adds two more global collections that can leak
   (`estates`, `wave_state/{estate_id}`), so `firestore_cleanup` exists to
   make "delete what you created" a single registration call rather than a
   try/finally each test has to remember to write correctly.
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

# A test session must never start the in-process event consumers. They
# default to ON — the whole point of the supervisor is that running the
# server is the only thing anyone runs — so importing frontend.app under
# test would otherwise start eight threads that pull REAL Pub/Sub
# subscriptions and mutate live Firestore, stealing messages from the
# developer's own console. Set, not setdefault: a value inherited from
# .env must lose to this.
os.environ["CONTROL_TOWER_WORKERS"] = "0"


# ---------------------------------------------------------------------------
# Which Firestore database this session may write to
# ---------------------------------------------------------------------------
#
# The suite used to write to `(default)` — the same database the console
# reads. That is how the live project accumulated hundreds of
# `test_run_*` documents and 9,891 orphaned subcollection records, and
# why the console paid to stream them on every uncached request (see
# tools/purge_orphans.py).
#
# The session now targets a separate database by default. Set, not
# setdefault, for the same reason CONTROL_TOWER_WORKERS is: a value
# inherited from .env is the developer's console configuration, and it
# must not decide where the tests write.
#
# MCT_TEST_FIRESTORE_DATABASE names it. The escape hatch is deliberately
# ugly and explicit — writing to production from a test run should be a
# thing someone chose, not a thing that happened.
from tests.probes import resolve_test_database  # noqa: E402

TEST_DATABASE = resolve_test_database(dict(os.environ))
if TEST_DATABASE is None:
    os.environ.pop("FIRESTORE_DATABASE", None)
else:
    os.environ["FIRESTORE_DATABASE"] = TEST_DATABASE


# ---------------------------------------------------------------------------
# Reachability probes — cached, so N modules cost one connection attempt each
# ---------------------------------------------------------------------------


# Reachability of the database this session is pointed at. Note what it
# does NOT do: fall back to `(default)` when the test database is
# missing. Skipping is the correct outcome — a silent fallback would put
# the writes back into production, which is the whole thing this is
# preventing, and it would do it at exactly the moment nobody is looking.
from tests.probes import firestore_reachable, firestore_skip_reason  # noqa: E402


@functools.lru_cache(maxsize=1)
def sqlserver_reachable() -> bool:
    try:
        from tools.sqlserver_client import get_connection

        get_connection().close()
        return True
    except Exception:  # noqa: BLE001
        return False


@functools.lru_cache(maxsize=1)
def postgres_reachable() -> bool:
    """Probes the Postgres second-estate fixture (Day 11 Phase 7).

    Returns False until that fixture exists, which is the correct
    behavior in the meantime: its tests skip rather than error.
    """
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return False
    try:
        import os

        with psycopg.connect(
            host=os.environ.get("POSTGRES_HOST", "localhost"),
            port=int(os.environ.get("POSTGRES_PORT", "5433")),
            user=os.environ.get("POSTGRES_USER", "postgres"),
            password=os.environ.get("POSTGRES_PASSWORD", ""),
            dbname=os.environ.get("POSTGRES_DB", "postgres"),
            connect_timeout=3,
        ):
            return True
    except Exception:  # noqa: BLE001
        return False


@functools.lru_cache(maxsize=1)
def pubsub_reachable() -> bool:
    try:
        import os

        from google.cloud import pubsub_v1

        project = os.environ.get("GCP_PROJECT_ID")
        if not project:
            return False
        next(iter(pubsub_v1.PublisherClient().list_topics(
            request={"project": f"projects/{project}"})), None)
        return True
    except Exception:  # noqa: BLE001
        return False


@functools.lru_cache(maxsize=1)
def bigquery_reachable() -> bool:
    try:
        from tools.bigquery_tools import get_client

        get_client().list_datasets(max_results=1)
        return True
    except Exception:  # noqa: BLE001
        return False


_PROBES = {
    "requires_firestore": (firestore_reachable, firestore_skip_reason()),
    "requires_sqlserver": (sqlserver_reachable, "SQL Server container not reachable"),
    "requires_postgres": (postgres_reachable, "Postgres fixture container not reachable"),
    "requires_bigquery": (bigquery_reachable, "BigQuery not reachable"),
    "requires_pubsub": (pubsub_reachable, "Pub/Sub not reachable"),
}


def pytest_configure(config: pytest.Config) -> None:
    for name, (_probe, reason) in _PROBES.items():
        config.addinivalue_line("markers", f"{name}: skip unless reachable — {reason}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Applies the requires_* markers as skips.

    Deliberately at collection time rather than import time: an unreachable
    dependency should cost one probe for the whole session, and only when a
    test that actually needs it was collected.
    """
    needed = {
        name
        for name in _PROBES
        if any(item.get_closest_marker(name) for item in items)
    }
    for name in needed:
        probe, reason = _PROBES[name]
        if probe():
            continue
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            if item.get_closest_marker(name):
                item.add_marker(skip)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


def seed_migration_plan(run_id: str, targets: list[dict] | None = None, **overrides) -> dict:
    """Writes a minimal MigrationPlan so a test run can reach MIGRATING.

    Since Day 11 Phase 3b the orchestrator, Validation, recovery and the
    cutover worker all read what to migrate from the run's plan rather
    than from module constants. A test that drives a run to PLANNED must
    therefore actually write a plan — which is not test scaffolding for
    its own sake: a run in PLANNED with no plan document was always an
    impossible state, it simply used to go unnoticed because the constants
    answered instead.
    """
    from tools.firestore_client import get_client

    plan = {
        "run_id": run_id,
        "estate_id": "wwi-demo-estate",
        "source_id": "wwi-sqlserver",
        "pack_id": "wwi_sqlserver_v1",
        "steps": [],
        "targets": targets if targets is not None else [{
            "target_id": "wwi-sqlserver:Sales.Customers",
            "table_id": "sqlserver-wwi.WideWorldImporters.Sales.Customers",
            "source_database": "WideWorldImporters",
            "source_schema": "Sales",
            "source_table": "Customers",
            "target_table": "customers_dim",
            "key_column": "CustomerID",
            "order_by": "CustomerID",
            "numeric_column": "CreditLimit",
            "null_check_column": "PhoneNumber",
            "aggregate_check": "applicable",
            "execution_order": 0,
            "scheduled": True,
            "blocked": False,
            "blocked_reason": None,
            "sql_translation_notes": None,
        }],
        "rollback_strategy": "test",
        "plan_hash": "test-plan-hash",
        "created_by": "tests",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    plan.update(overrides)
    get_client().collection("migration_runs").document(run_id).collection(
        "migration_plan"
    ).document("current").set(plan)
    return plan


@pytest.fixture
def firestore_cleanup():
    """Deletes registered Firestore documents in teardown, always.

    Usage:

        def test_thing(firestore_cleanup):
            run_id = create_run("test.pipeline")
            firestore_cleanup.run(run_id)

    Every deletion is independently guarded, so one failure cannot strand
    the rest — the failure mode CLAUDE.md warns about is precisely a
    half-finished teardown leaving state behind.
    """

    class _Cleanup:
        def __init__(self) -> None:
            self._deleters: list[tuple[str, callable]] = []

        def add(self, label: str, delete: callable) -> None:
            self._deleters.append((label, delete))

        def run(self, run_id: str) -> str:
            from agents.orchestrator.run_lifecycle import delete_run

            self.add(f"run {run_id}", lambda: delete_run(run_id))
            return run_id

        def card(self, agent_id: str, version: str) -> None:
            from tools.registry import delete_card

            self.add(f"card {agent_id}@{version}", lambda: delete_card(agent_id, version))

        def document(self, path: str) -> None:
            """path is a slash-separated Firestore path, e.g. 'estates/acme'."""
            from tools.firestore_client import get_client

            self.add(path, lambda: get_client().document(path).delete())

        def _teardown(self) -> list[str]:
            failures = []
            for label, delete in reversed(self._deleters):
                try:
                    delete()
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{label}: {exc}")
            return failures

    cleanup = _Cleanup()
    yield cleanup
    failures = cleanup._teardown()
    if failures:
        # Warn rather than fail: the test's own result is the signal that
        # matters, but a silent leak is exactly what caused trouble before.
        raise pytest.fail.Exception(
            "test left Firestore state behind:\n  " + "\n  ".join(failures)
        )
