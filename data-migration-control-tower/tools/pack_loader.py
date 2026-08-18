"""Migration Pack registry (master doc §32.4).

A Migration Pack is a versioned bundle of source-family knowledge —
classification rules, dialect notes, data-type behavior, which tables to
schedule, and the default run mode. It is configuration and domain
knowledge, not a fork of the platform: onboarding a new source family
should mean writing a pack, not editing an agent.

Day 11 Phase 3 turned this from a 39-line loader into a registry, and
made three declared-but-dead fields load-bearing. Before that:

  - `load_pack` never validated, so a typo surfaced as a KeyError several
    call frames later, usually inside an agent.
  - Packs were discovered by two independent globs (frontend/api_v1.py
    and agents/discovery/run_assessment.py) that could disagree.
  - `classification_rules`, `dialect_notes` and `default_mode` were
    declared in every pack.yaml and read by nothing. `default_mode` in
    particular claimed to stop the Oracle pack from executing; what
    actually stopped it was a hardcoded `source_id == "wwi-sqlserver"`
    comparison in the API.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import jsonschema
import yaml

from tools.adapters import SourceAdapter, build_adapter

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKS_DIR = REPO_ROOT / "packs"
SCHEMA_PATH = REPO_ROOT / "contracts" / "metadata_model.json"

MODE_ASSESSMENT = "assessment"
MODE_EXECUTION = "execution"


class PackValidationError(ValueError):
    pass


class PackNotFound(KeyError):
    pass


@functools.lru_cache(maxsize=1)
def _pack_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["definitions"]["MigrationPack"]


def validate_pack(pack: dict, *, source: str = "<dict>") -> dict:
    try:
        jsonschema.validate(instance=pack, schema=_pack_schema())
    except jsonschema.ValidationError as exc:
        path = ".".join(str(p) for p in exc.absolute_path) or "(root)"
        raise PackValidationError(f"{source}: {path}: {exc.message}") from exc

    for entry in pack.get("scheduled_tables", []) or []:
        missing = [k for k in ("source_schema", "source_table", "target_table") if not entry.get(k)]
        if missing:
            raise PackValidationError(
                f"{source}: scheduled_tables entry missing {missing}. A scheduled table must "
                f"name its source schema, source table and target table."
            )
    return pack


def load_pack(pack_path: str | Path) -> dict:
    """Loads and validates one pack.yaml. Accepts a directory or a file."""
    path = Path(pack_path)
    if path.is_dir():
        path = path / "pack.yaml"
    if not path.is_file():
        raise PackNotFound(f"No pack.yaml at {path}.")
    pack = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(pack, dict):
        raise PackValidationError(f"{path} is not a YAML mapping.")
    validate_pack(pack, source=str(path))
    return {**pack, "_path": str(path), "_dir": str(path.parent)}


def list_packs(*, packs_dir: Path | None = None) -> list[dict]:
    """Every committed pack, validated. One discovery path, not three."""
    directory = Path(packs_dir) if packs_dir else PACKS_DIR
    return [load_pack(p) for p in sorted(directory.glob("*/pack.yaml"))]


def get_pack(pack_id: str, *, packs_dir: Path | None = None) -> dict:
    for pack in list_packs(packs_dir=packs_dir):
        if pack.get("pack_id") == pack_id:
            return pack
    known = [p.get("pack_id") for p in list_packs(packs_dir=packs_dir)]
    raise PackNotFound(f"No pack {pack_id!r}. Known packs: {known}.")


# ---------------------------------------------------------------------------
# Accessors — the previously-dead fields, now load-bearing
# ---------------------------------------------------------------------------


def default_mode(pack: dict) -> str:
    return pack.get("default_mode", MODE_ASSESSMENT)


def supports_execution(pack: dict) -> bool:
    """Whether this pack may run the full lifecycle.

    Replaces frontend/api_v1.py's hardcoded `source_id == "wwi-sqlserver"`.
    A pack is executable when it says so AND its adapter can actually move
    and reconcile rows — an assessment-only source family (the Oracle DDL
    corpus has no live database at all) is visibly disabled rather than
    accepted and then failed at run time.
    """
    if default_mode(pack) != MODE_EXECUTION:
        return False
    try:
        adapter_type = adapter_type_for(pack)
    except Exception:  # noqa: BLE001 — unresolvable estate means "not executable"
        return False
    from tools.adapters import ADAPTER_TYPES

    adapter_cls = ADAPTER_TYPES.get(adapter_type)
    if adapter_cls is None:
        return False
    capabilities = getattr(adapter_cls, "capabilities", frozenset())
    return {"transfer", "reconcile"} <= set(capabilities) if capabilities else True


def classification_rules_path(pack: dict) -> Path | None:
    declared = pack.get("classification_rules")
    return (REPO_ROOT / declared) if declared else None


def classification_rules(pack: dict) -> dict | None:
    """The pack's classification rules, or None to use the global default.

    tools/data_classifier.py reads policies/data_classification.yaml
    directly today; passing this in is what lets a regulated estate carry
    stricter rules without editing the Risk agent.
    """
    path = classification_rules_path(pack)
    if path is None or not path.is_file():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def dialect_note(pack: dict) -> str | None:
    """The sql_translation_notes text for dialect-incompatible tables.

    Previously hardcoded as an Oracle-specific sentence inside
    tools/plan_builder.py, which meant every source family got told about
    NVL/DECODE/CONNECT BY whether or not it had ever seen them.
    """
    note = pack.get("dialect_notes")
    return note.strip() if isinstance(note, str) and note.strip() else None


def lineage_sql_corpus_path(pack: dict) -> Path | None:
    declared = pack.get("lineage_sql_corpus_path")
    return (REPO_ROOT / declared) if declared else None


def type_rules(pack: dict) -> dict:
    """Data-type behavior for this source family, with SQL-Server-shaped
    defaults for packs that predate the field."""
    declared = pack.get("type_rules") or {}
    return {
        "scalar_safe": set(declared.get("scalar_safe") or _DEFAULT_TYPE_RULES["scalar_safe"]),
        "stringify_via_method": set(
            declared.get("stringify_via_method") or _DEFAULT_TYPE_RULES["stringify_via_method"]
        ),
        "excluded": set(declared.get("excluded") or _DEFAULT_TYPE_RULES["excluded"]),
        "numeric": set(declared.get("numeric") or _DEFAULT_TYPE_RULES["numeric"]),
    }


_DEFAULT_TYPE_RULES = {
    "scalar_safe": {
        "int", "bigint", "smallint", "tinyint", "bit",
        "nvarchar", "varchar", "char", "nchar",
        "decimal", "numeric", "float", "real",
        "date", "datetime", "datetime2", "uniqueidentifier",
    },
    "stringify_via_method": {"geography", "geometry", "hierarchyid"},
    "excluded": {"xml", "varbinary", "image"},
    "numeric": {
        "int", "bigint", "smallint", "tinyint",
        "decimal", "numeric", "float", "real", "money", "smallmoney",
    },
}


def scheduled_tables(pack: dict) -> list[dict]:
    """Explicitly scheduled tables, or [] to derive from the catalog."""
    return list(pack.get("scheduled_tables") or [])


# ---------------------------------------------------------------------------
# Estate / adapter binding
# ---------------------------------------------------------------------------


def load_estate(estate_path: str | Path) -> dict:
    """Kept for callers that hold a path rather than an estate_id. New code
    should go through tools/connection_context.py, which reads the Firestore
    estate registry first and falls back to committed YAML."""
    return yaml.safe_load(Path(estate_path).read_text(encoding="utf-8"))


def estate_for_pack(pack: dict) -> dict:
    from tools.connection_context import load_estate_document

    declared_file = pack.get("estate_file")
    estate_path = REPO_ROOT / declared_file if declared_file else None
    if estate_path is not None and estate_path.is_file():
        on_disk = load_estate(estate_path)
        # The registry is the authority when it knows this estate; the
        # committed file is the fallback for an offline checkout.
        try:
            return load_estate_document(on_disk["estate_id"])
        except Exception:  # noqa: BLE001
            return on_disk
    raise PackValidationError(
        f"Pack {pack.get('pack_id')!r} names estate_file {declared_file!r}, which does not exist."
    )


def adapter_type_for(pack: dict) -> str:
    from tools.connection_context import find_source

    return find_source(estate_for_pack(pack), pack["source_id"])["adapter"]


def binding_for_pack(pack: dict):
    """The SourceBinding this pack targets."""
    from tools.connection_context import binding_from_estate

    return binding_from_estate(estate_for_pack(pack), pack["source_id"])


def build_adapter_for_pack(pack: dict) -> SourceAdapter:
    estate = estate_for_pack(pack)
    from tools.connection_context import find_source

    source = find_source(estate, pack["source_id"])
    return build_adapter(source["adapter"], **(source.get("config") or {}))
