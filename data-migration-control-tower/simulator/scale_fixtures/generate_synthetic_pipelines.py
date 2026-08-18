"""Generates lightweight, schema-only synthetic Pipeline definitions for
Day 10 Phase 4's bounded control-plane scale demonstration (master doc
§32.12: "Separate metadata/control-plane scale from bulk-data scale so
the experiment remains affordable and interpretable.").

No real data volume, no live SQL Server/BigQuery calls — this exercises
how the control plane (schema validation, Wave Manager scheduling,
policy decisions) behaves at 100-500 pipeline definitions, deliberately
NOT the master doc's 20,000-definition bulk-data claim, which needs a
live estate that large to prove honestly and is out of this phase's
scope (see the Day 10 hardening plan's Phase 4 scope note).
"""

from __future__ import annotations

import datetime as dt
import random

_SOURCES = ["wwi-sqlserver", "oracle-corpus", "synthetic-source-a", "synthetic-source-b", "synthetic-source-c"]
_CRITICALITY = ["CRITICAL", "HIGH", "MEDIUM", "LOW"]
_CRITICALITY_WEIGHTS = [5, 15, 40, 40]  # CRITICAL is deliberately rare, matching a real estate's shape


def generate_synthetic_pipelines(count: int, seed: int = 42) -> list[dict]:
    """Deterministic given the same (count, seed) — reproducible runs,
    not a fresh random shape every invocation."""
    rng = random.Random(seed)
    now = dt.datetime.now(dt.timezone.utc)
    pipelines = []
    for i in range(count):
        pipelines.append(
            {
                "pipeline_id": f"synthetic.pipeline.{i:05d}",
                "source_system": rng.choice(_SOURCES),
                "target_system": "bigquery",
                "schedule": "0 2 * * *",
                "owner": f"team-{i % 10}@example.internal",
                "criticality": rng.choices(_CRITICALITY, weights=_CRITICALITY_WEIGHTS)[0],
                "code_path": f"synthetic/pipeline_{i:05d}.py",
                "status": "ACTIVE",
                "upstream_tables": [f"synthetic.schema.table_{i:05d}_src"],
                "downstream_tables": [f"bigquery.migration_target.table_{i:05d}_tgt"],
                # Spread requests over the last 2 hours so a real run
                # exercises tools/wave_manager.py's backlog-age
                "requested_at": (now - dt.timedelta(minutes=rng.randint(0, 120))).isoformat(),
            }
        )
    return pipelines
