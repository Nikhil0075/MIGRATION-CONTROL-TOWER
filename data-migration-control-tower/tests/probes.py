"""Dependency reachability probes, shared by conftest and by test modules.

A separate module rather than living only in conftest.py, because ten
test modules evaluate reachability at IMPORT time to build a
`pytest.mark.skipif`, and importing conftest from a test module to get at
one function is a circular-looking arrangement nobody should have to
reason about.

Why this exists at all: those ten modules each carried a private

    def _firestore_reachable() -> bool:
        try:
            get_client()
            return True
        except Exception:
            return False

which never probed anything. `firestore.Client(...)` is lazy — it builds
an object and performs no I/O — so the function returned True whenever
the import succeeded, including when Firestore was unreachable, the
project was wrong, or the database did not exist. Ninety-two tests that
believed they would skip instead failed with transport errors.
"""

from __future__ import annotations

import functools


@functools.lru_cache(maxsize=1)
def firestore_reachable() -> bool:
    """True only if a real round trip to the configured database works.

    `next(client.collections(), None)` is the cheapest call that actually
    goes to the server. Constructing the client proves nothing.

    Cached: ten modules ask this at import time, and the answer cannot
    change within a session.
    """
    try:
        from tools.firestore_client import get_client

        next(get_client().collections(), None)
        return True
    except Exception:  # noqa: BLE001 — any failure means "skip", never "fail"
        return False


def firestore_skip_reason() -> str:
    from tools.firestore_client import database_id

    return (
        f"Firestore database {database_id()!r} is not reachable. Create it with "
        f"`bash infrastructure/gcp_setup.sh` and seed it with "
        f"`python -m infrastructure.seed_test_database`."
    )


DEFAULT_TEST_DATABASE = "mct-tests"


def resolve_test_database(env: dict[str, str]) -> str | None:
    """Which database a test session may write to; None means production.

    A function taking an explicit mapping rather than logic inlined into
    conftest, so the rule can be tested with the environments that matter
    instead of only the one the current process happens to have.

    The opt-out is exact-match "1". "0", "false" and "no" all read to a
    human as "do not do this", and any truthiness check would turn the
    guard ON for someone trying to turn it off safely.
    """
    if env.get("MCT_TESTS_MAY_WRITE_PRODUCTION") == "1":
        return None
    return env.get("MCT_TEST_FIRESTORE_DATABASE") or DEFAULT_TEST_DATABASE
