from __future__ import annotations

import pytest

from tools import backfill_demo_pack as backfill
from tools.estate_registry import EstateConflict


def _estate(pack_id=None):
    source = {
        "source_id": backfill.SOURCE_ID,
        "adapter": "sqlserver",
        "config": {"database": "WideWorldImporters"},
        "connection_profile": {"host_env": "SQLSERVER_HOST"},
        "custom_live_field": "preserve-me",
    }
    if pack_id is not None:
        source["pack_id"] = pack_id
    return {
        "estate_id": backfill.DEFAULT_ESTATE_ID,
        "display_name": "Demo",
        "sources": [source, {"source_id": "dag", "adapter": "dag_artifacts"}],
        "owner": {"team": "migration"},
    }


def test_backfill_is_dry_run_by_default(monkeypatch):
    writes = []
    monkeypatch.setattr(backfill, "get_estate", lambda _id: _estate())
    monkeypatch.setattr(backfill, "update_estate", lambda *args, **kwargs: writes.append((args, kwargs)))
    assert backfill.backfill_demo_pack()["status"] == "dry_run"
    assert writes == []


def test_backfill_changes_only_missing_pack_and_uses_revisioned_update(monkeypatch):
    original = _estate()
    writes = []
    monkeypatch.setattr(backfill, "get_estate", lambda _id: original)
    monkeypatch.setattr(backfill, "update_estate", lambda *args, **kwargs: writes.append((args, kwargs)))

    assert backfill.backfill_demo_pack(apply=True)["status"] == "applied"
    patch = writes[0][0][1]
    assert patch["sources"][0]["pack_id"] == backfill.PACK_ID
    assert patch["sources"][0]["custom_live_field"] == "preserve-me"
    assert patch["sources"][1] == original["sources"][1]
    assert "display_name" not in patch and "owner" not in patch
    assert writes[0][1]["reason"] == "targeted_backfill:missing_demo_pack_id"


def test_backfill_refuses_to_replace_a_conflicting_pack(monkeypatch):
    monkeypatch.setattr(backfill, "get_estate", lambda _id: _estate("different_pack"))
    with pytest.raises(EstateConflict, match="conflicting pack_id"):
        backfill.backfill_demo_pack(apply=True)
