"""The test suite must not write to the database the console reads.

It used to. The live project accumulated hundreds of `test_run_*`
documents and 9,891 orphaned subcollection records, and because
collection-group queries here run unfiltered — no composite index exists
for group + order_by, per CLAUDE.md — the console paid to stream all of
it on every uncached request.

The tests below guard the isolation itself. They are meta-tests: if they
fail, every other test in this repository is writing somewhere it should
not, and would keep passing while doing it.
"""

from __future__ import annotations

import os

import pytest

from tools.firestore_client import DEFAULT_DATABASE, database_id, get_client


def test_this_very_session_is_not_pointed_at_production():
    """The one that matters.

    Asserted about the RUNNING session rather than about a function's
    return value, because the failure being prevented is a conftest
    regression — someone removes the assignment, every test still passes,
    and the writes quietly go back to `(default)`. Nothing else in the
    suite would notice.
    """
    if os.environ.get("MCT_TESTS_MAY_WRITE_PRODUCTION") == "1":
        pytest.skip("explicitly opted in to writing to the production database")
    assert database_id() != DEFAULT_DATABASE, (
        "this test session is pointed at the production Firestore database; "
        "tests/conftest.py should have redirected it"
    )
    assert os.environ.get("FIRESTORE_DATABASE") == database_id()


def test_the_opt_out_has_to_be_spelled_out_exactly():
    """A truthy-looking value must not be enough.

    "0", "false" and "no" all read as "do not do this" to a human. If any
    of them enabled production writes, someone trying to turn the guard
    OFF-but-safely would turn it on.
    """
    from tests.probes import DEFAULT_TEST_DATABASE, resolve_test_database

    # None means "write to production".
    assert resolve_test_database({"MCT_TESTS_MAY_WRITE_PRODUCTION": "1"}) is None
    for value in ("0", "false", "no", "", "true", "yes", "TRUE"):
        assert resolve_test_database({"MCT_TESTS_MAY_WRITE_PRODUCTION": value}) == (
            DEFAULT_TEST_DATABASE
        ), f"{value!r} was treated as permission to write to production"


def test_the_test_database_can_be_named_without_editing_code():
    from tests.probes import DEFAULT_TEST_DATABASE, resolve_test_database

    assert resolve_test_database({}) == DEFAULT_TEST_DATABASE
    assert resolve_test_database({"MCT_TEST_FIRESTORE_DATABASE": "scratch"}) == "scratch"


def test_the_database_is_read_from_the_environment_not_baked_in(monkeypatch):
    monkeypatch.setenv("FIRESTORE_DATABASE", "some-other-database")
    assert database_id() == "some-other-database"
    monkeypatch.delenv("FIRESTORE_DATABASE")
    assert database_id() == DEFAULT_DATABASE


def test_changing_the_database_returns_a_different_client(monkeypatch):
    """The cache is keyed, not global.

    `get_client` was `lru_cache(maxsize=1)` over a no-argument function.
    A process that changed FIRESTORE_DATABASE would have been handed back
    the client for the database it just left — writing to production
    while believing it had moved away.
    """
    monkeypatch.setenv("FIRESTORE_DATABASE", "database-a")
    first = get_client()
    monkeypatch.setenv("FIRESTORE_DATABASE", "database-b")
    second = get_client()
    assert first is not second

    monkeypatch.setenv("FIRESTORE_DATABASE", "database-a")
    assert get_client() is first, "the cache stopped caching"


def test_the_reachability_probe_actually_contacts_the_server():
    """The bug that made ten modules' skip guards decorative.

    Each carried `get_client(); return True`. The Firestore client is
    lazy and performs no I/O when constructed, so the probe returned True
    whenever the import succeeded — including against a database that did
    not exist. Ninety-two tests that believed they would skip failed with
    transport errors instead.
    """
    import inspect

    from tests.probes import firestore_reachable

    source = inspect.getsource(firestore_reachable)
    assert "collections()" in source, "the probe does not perform a round trip"


@pytest.mark.parametrize("module", [
    "test_approval_service", "test_evaluation_harness", "test_frontend_api",
    "test_injection_defense", "test_memory_bank", "test_orchestrator",
    "test_policy_engine", "test_registry", "test_state_machine", "test_wave_manager",
])
def test_no_module_keeps_its_own_hollow_probe(module):
    """Ten copies of the same broken check is how it survived so long.

    A module that reintroduces the local version gets a passing suite and
    a guard that never fires, so this asserts on the source: the probe
    must delegate, not reimplement.
    """
    from pathlib import Path

    source = (Path(__file__).parent / f"{module}.py").read_text(encoding="utf-8")
    assert "from tests.probes import firestore_reachable" in source, (
        f"{module} defines its own Firestore probe again"
    )


def test_the_test_seeder_refuses_to_touch_production(capsys):
    from infrastructure import seed_test_database

    assert seed_test_database.main(["--database", DEFAULT_DATABASE]) == 1
    assert "Refusing to seed" in capsys.readouterr().err
