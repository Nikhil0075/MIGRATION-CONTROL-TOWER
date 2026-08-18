"""Binds a run to one source of one estate (Day 11 Phase 1, master doc §32.3).

A SourceBinding is the answer to "which estate, which source, reached how?"
— the piece of context that had no representation before. Discovery
instantiated adapter classes by name, tools/sqlserver_client.py read a
process-global `SQLSERVER_*` namespace, and the estate's declared
`connection_profile` was never read by anything, so a second SQL Server
estate would have silently connected with the first one's credentials.

Estate documents are loaded from YAML here. Phase 2 puts the Firestore
estate registry in front of this, at which point `_load_estate_documents`
gains a registry lookup and YAML becomes the import/seed path — every
caller of `binding_for()` is unaffected by that change, which is the
reason this indirection exists now rather than later.

Nothing in this module holds a credential. Resolution is deferred to
SourceBinding.resolve(), which returns a redacting ResolvedConnection
(tools/secret_resolver.py) at the moment a connection is actually opened.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from tools.secret_resolver import ResolvedConnection, resolve_connection

logger = logging.getLogger("connection_context")

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The estate this project ships and demos with. Used as the fallback for
#: run documents created before `estate_id` was recorded on them — see
#: binding_for_run(). Readers must treat a *missing* estate_id as this
#: value rather than as "no match", or the Control Tower dashboard goes
#: empty for every historical run.
DEFAULT_ESTATE_ID = "wwi-demo-estate"

#: Where committed estate YAML lives. A directory is globbed for *.yaml so
#: a second estate is added by dropping in a file, not by editing code.
ESTATE_SEARCH_PATHS: list[Path] = [
    REPO_ROOT / "simulator" / "source_setup" / "estate.yaml",
    REPO_ROOT / "config" / "estates",
]

#: Per-adapter-family connection fallbacks. These reproduce exactly what
#: tools/sqlserver_client.py::get_connection() has always defaulted to, so
#: the committed demo estate resolves identically to before this module
#: existed — the estate.yaml it ships with declares host_env/port_env/
#: user_env and a password_secret_ref, but no password_env, and without
#: these defaults it would have no local-dev fallback to resolve against.
ADAPTER_CONNECTION_DEFAULTS: dict[str, dict] = {
    "sqlserver": {
        "host": "localhost",
        "port": 1433,
        "user": "sa",
        "database": "WideWorldImporters",
        "password_env": "SQLSERVER_PASSWORD",
    },
    "postgres": {
        "host": "localhost",
        "port": 5433,
        "user": "postgres",
        "password_env": "POSTGRES_PASSWORD",
    },
}


class EstateNotFound(KeyError):
    pass


class SourceNotFound(KeyError):
    pass


@dataclass(frozen=True)
class SourceBinding:
    """One source of one estate, plus how to reach it — by reference."""

    estate_id: str
    source_id: str
    adapter: str
    config: dict = field(default_factory=dict)
    connection_profile: dict | None = None
    pack_id: str | None = None

    @property
    def requires_connection(self) -> bool:
        """False for static-file sources (the Oracle DDL corpus, DAG
        artifacts) which declare `connection_profile: null` — they have no
        live server, and asking them to resolve credentials is a bug."""
        return self.connection_profile is not None

    @property
    def health_key(self) -> str:
        """Document id for connection_health. Scoped by estate so two
        estates using the same adapter do not overwrite each other's
        freshness snapshot."""
        return f"{self.estate_id}__{self.source_id}"

    def resolve(self) -> ResolvedConnection:
        """Resolves live connection parameters. Call as late as possible."""
        if not self.requires_connection:
            raise SourceNotFound(
                f"Source {self.source_id!r} of estate {self.estate_id!r} declares no "
                f"connection_profile — it is a static-file source with no live server."
            )
        return resolve_connection(
            self.connection_profile,
            config=self.config,
            defaults=ADAPTER_CONNECTION_DEFAULTS.get(self.adapter, {}),
        )


# ---------------------------------------------------------------------------
# Estate document loading (Phase 2 puts the Firestore registry in front)
# ---------------------------------------------------------------------------


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for entry in ESTATE_SEARCH_PATHS:
        if entry.is_dir():
            files.extend(sorted(entry.glob("*.yaml")))
        elif entry.is_file():
            files.append(entry)
    return files


@functools.lru_cache(maxsize=1)
def _estate_index() -> dict[str, dict]:
    index: dict[str, dict] = {}
    for path in _candidate_files():
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError(f"{path} is not valid YAML: {exc}") from exc
        if not isinstance(doc, dict) or "estate_id" not in doc:
            continue
        doc = dict(doc)
        doc.setdefault("_source_path", str(path))
        index[doc["estate_id"]] = doc
    return index


def clear_cache() -> None:
    """Drops the estate index. Tests and estate edits call this; Phase 2's
    registry makes it a no-op for registry-backed estates."""
    _estate_index.cache_clear()


def list_estate_documents() -> list[dict]:
    """Every known estate — registry entries take precedence over the
    committed YAML of the same estate_id."""
    documents = dict(_estate_index())
    try:
        from tools.estate_registry import list_estates

        for estate in list_estates():
            if estate.get("estate_id"):
                documents[estate["estate_id"]] = estate
    except Exception as exc:  # noqa: BLE001 — offline checkout lists YAML only
        logger.debug("estate registry unavailable for listing (%s); using YAML", exc)
    return list(documents.values())


def _from_registry(estate_id: str) -> dict | None:
    """Reads the Firestore estate registry, or None if it can't answer.

    Deliberately never raises: an estate created in the console is the
    authority when Firestore is reachable, but a local checkout with no
    GCP credentials must still resolve the committed YAML estates so
    `python agents/discovery/run_discovery.py` works offline. A registry
    miss and an unreachable registry both mean "fall through to YAML".
    """
    try:
        from tools.estate_registry import EstateNotFound as _RegistryMiss
        from tools.estate_registry import get_estate

        return get_estate(estate_id)
    except _RegistryMiss:
        return None
    except Exception as exc:  # noqa: BLE001 — unreachable registry is not fatal
        logger.debug("estate registry unavailable for %s (%s); using YAML", estate_id, exc)
        return None


def load_estate_document(estate_id: str) -> dict:
    """Resolves an estate: registry first, committed YAML as the fallback.

    Registry reads are uncached. They are a single document get, and an
    operator who just edited an estate in the console must see the change
    on the next run rather than after a process restart.
    """
    from_registry = _from_registry(estate_id)
    if from_registry is not None:
        return from_registry

    index = _estate_index()
    try:
        return index[estate_id]
    except KeyError as exc:
        raise EstateNotFound(
            f"No estate {estate_id!r} in the registry or on disk. Known committed "
            f"estates: {sorted(index) or '(none)'}. Searched: "
            f"{[str(p) for p in ESTATE_SEARCH_PATHS]}. Seed committed estates with "
            f"`python infrastructure/seed_estates.py`."
        ) from exc


def find_source(estate: dict, source_id: str) -> dict:
    for source in estate.get("sources", []):
        if source.get("source_id") == source_id:
            return source
    raise SourceNotFound(
        f"No source {source_id!r} in estate {estate.get('estate_id')!r}. Declared "
        f"sources: {[s.get('source_id') for s in estate.get('sources', [])]}."
    )


def binding_from_estate(estate: dict, source_id: str) -> SourceBinding:
    source = find_source(estate, source_id)
    return SourceBinding(
        estate_id=estate["estate_id"],
        source_id=source["source_id"],
        adapter=source["adapter"],
        config=dict(source.get("config") or {}),
        connection_profile=source.get("connection_profile"),
        pack_id=source.get("pack_id"),
    )


def binding_for(estate_id: str, source_id: str) -> SourceBinding:
    return binding_from_estate(load_estate_document(estate_id), source_id)


def bindings_for_estate(estate_id: str) -> list[SourceBinding]:
    estate = load_estate_document(estate_id)
    return [
        binding_from_estate(estate, source["source_id"])
        for source in estate.get("sources", [])
    ]


def binding_for_run(run_id: str, source_id: str | None = None) -> SourceBinding:
    """Resolves the binding a run is executing against.

    Run documents gain `estate_id`/`source_id` in Phase 2. Until every run
    carries them — and for the history that never will — a missing
    `estate_id` resolves to DEFAULT_ESTATE_ID rather than raising, which is
    the documented one-release compatibility path.
    """
    from tools.firestore_client import get_client

    snapshot = get_client().collection("migration_runs").document(run_id).get()
    if not snapshot.exists:
        raise EstateNotFound(f"No run {run_id!r}.")
    run = snapshot.to_dict() or {}

    estate_id = run.get("estate_id") or DEFAULT_ESTATE_ID
    estate = load_estate_document(estate_id)
    resolved_source = source_id or run.get("source_id")
    if not resolved_source:
        # A run predating source_id, or one whose estate has a single
        # connectable source: pick the only live source rather than guess.
        connectable = [
            s for s in estate.get("sources", []) if s.get("connection_profile") is not None
        ]
        if len(connectable) != 1:
            raise SourceNotFound(
                f"Run {run_id!r} records no source_id and estate {estate_id!r} has "
                f"{len(connectable)} connectable sources — cannot infer which one. "
                f"Pass source_id explicitly."
            )
        resolved_source = connectable[0]["source_id"]
    return binding_from_estate(estate, resolved_source)
