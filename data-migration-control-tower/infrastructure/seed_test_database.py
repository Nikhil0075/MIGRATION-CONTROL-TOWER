"""Seeds the Firestore database the test suite writes to.

`tests/conftest.py` points the session at a separate database — see
MCT_TEST_FIRESTORE_DATABASE — so that `pytest` stops writing to the one
the console reads. A fresh database is empty, and a good many tests
assume the same baseline a real deployment has: an Agent Registry with
APPROVED cards, and at least one estate to resolve a source connection
against.

This is the same seeding a real environment gets, aimed at the test
database instead of `(default)`. It reuses the existing seed scripts
rather than reimplementing them, so the baseline the tests run against
cannot drift away from the baseline production gets.

    bash infrastructure/gcp_setup.sh          # creates the database
    python -m infrastructure.seed_test_database

Idempotent. Safe to re-run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

DEFAULT_TEST_DATABASE = "mct-tests"


def _target_database(explicit: str | None) -> str:
    return (
        explicit
        or os.environ.get("MCT_TEST_FIRESTORE_DATABASE")
        or DEFAULT_TEST_DATABASE
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        help="Database to seed. Defaults to MCT_TEST_FIRESTORE_DATABASE, then 'mct-tests'.",
    )
    args = parser.parse_args(argv)

    database = _target_database(args.database)

    # Refusing this by name rather than trusting the caller. The whole
    # point of the test database is that the suite cannot touch
    # production, and a seeding script that would happily rewrite the
    # Agent Registry in `(default)` because someone passed the wrong flag
    # is the same class of accident with a bigger blast radius.
    from tools.firestore_client import DEFAULT_DATABASE

    if database == DEFAULT_DATABASE:
        print(
            f"Refusing to seed {DEFAULT_DATABASE!r} from the test seeder. "
            f"Use infrastructure/seed_registry.py and seed_estates.py for a "
            f"real environment.",
            file=sys.stderr,
        )
        return 1

    # Set before importing anything that builds a client, since the
    # client is cached per (project, database).
    os.environ["FIRESTORE_DATABASE"] = database

    from infrastructure import seed_finance_agent, seed_registry
    from tools.estate_registry import seed_committed_estates
    from tools.firestore_client import database_id

    assert database_id() == database, "the client is not pointed at the test database"
    print(f"==> Seeding Firestore database {database!r}\n")

    print("--> Agent Registry")
    seed_registry.main()

    print("\n--> Finance Impact agent (wildcard capability)")
    seed_finance_agent.main()

    print("\n--> Committed estates")
    for estate in seed_committed_estates(actor="seed-test-database"):
        print(f"    {estate.get('estate_id')}")

    print(f"\n==> Done. `pytest` will now run against {database!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
