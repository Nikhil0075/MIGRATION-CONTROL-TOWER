"""Adapter type registry + factory (Day 10 Phase 3).

Not the same thing as tools/registry.py's Agent Registry (that governs
which *agent* is allowed to act, with publish/approve/discover
semantics) — this is a much simpler local lookup from a string type
name (as declared in an estate.yaml source's `adapter:` field) to the
SourceAdapter class that implements it. Adding a new source family means
adding one entry here, not touching every caller.
"""

from __future__ import annotations

from tools.adapters.base import SourceAdapter
from tools.adapters.dag_artifact_adapter import DagArtifactAdapter
from tools.adapters.oracle_corpus_adapter import OracleCorpusAdapter
from tools.adapters.postgres_adapter import PostgresAdapter
from tools.adapters.sqlserver_adapter import SqlServerAdapter

ADAPTER_TYPES: dict[str, type[SourceAdapter]] = {
    "sqlserver": SqlServerAdapter,
    "oracle_corpus": OracleCorpusAdapter,
    "dag_artifacts": DagArtifactAdapter,
    # The R3 extensibility claim: onboarding a new source family is this
    # line plus one adapter module. Nothing in agents/ changed to support
    # it — tests/test_clean_estate_onboarding.py asserts that mechanically.
    "postgres": PostgresAdapter,
}


def register_adapter(name: str, cls: type[SourceAdapter]) -> None:
    """Registers a source family. Adding one should be one line, not a
    change at every call site — that is the R3 extensibility claim
    (§32.1), and tests/test_clean_estate_onboarding.py enforces it."""
    if not issubclass(cls, SourceAdapter):
        raise TypeError(f"{cls!r} does not implement SourceAdapter.")
    ADAPTER_TYPES[name] = cls


def build_adapter(adapter_type: str, *, binding=None, **config) -> SourceAdapter:
    """Instantiates the adapter named by an estate source's `adapter:`
    field, passing its `config:` block through as kwargs.

    `binding` (Day 11) carries which estate/source this instance serves so
    the adapter can resolve that estate's own credentials instead of a
    process-global environment namespace. Optional: the direct-construction
    path used by local CLI scripts and existing tests still works.
    """
    cls = ADAPTER_TYPES.get(adapter_type)
    if cls is None:
        raise ValueError(f"Unknown adapter type {adapter_type!r}. Known: {sorted(ADAPTER_TYPES)}")
    if binding is None:
        return cls(**config)
    return cls(binding=binding, **config)


def build_adapter_for_binding(binding) -> SourceAdapter:
    return build_adapter(binding.adapter, binding=binding, **(binding.config or {}))


def describe_adapters() -> list[dict]:
    """What each registered adapter type is and can do.

    Feeds the onboarding wizard's adapter picker, so the console renders
    the available source families and disables unsupported actions from
    declared capabilities rather than from a hardcoded list of type names.
    """
    return [
        {
            "adapter_type": name,
            "class_name": cls.__name__,
            "system": getattr(cls, "system", None),
            "capabilities": sorted(getattr(cls, "capabilities", frozenset())),
            "summary": (cls.__doc__ or "").strip().splitlines()[0] if cls.__doc__ else "",
        }
        for name, cls in sorted(ADAPTER_TYPES.items())
    ]


__all__ = [
    "ADAPTER_TYPES",
    "SourceAdapter",
    "build_adapter",
    "build_adapter_for_binding",
    "describe_adapters",
    "register_adapter",
]
