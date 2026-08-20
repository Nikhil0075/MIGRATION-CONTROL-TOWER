"""The orphan purge, and the rule that keeps orphans from coming back.

Deleting a Firestore document does not delete its subcollections.
`delete_run` relied on that being harmless; it was not, because every
console aggregate streams whole collection groups unfiltered (no
composite index exists for group + order_by, per CLAUDE.md) and so paid
to read 9,891 dead documents on every uncached request.

These tests cover the two things that can go wrong with a tool whose job
is irreversible deletion: deleting something it should not have, and
deleting without a rescue copy.
"""

from __future__ import annotations

import json
import types

import pytest

from tools import purge_orphans


class FakeRef:
    def __init__(self, path: str, parent=None, data: dict | None = None):
        self.path = path
        self.id = path.rsplit("/", 1)[-1]
        self.parent = parent
        self._data = data if data is not None else {"field": "value"}

    def get(self):
        return types.SimpleNamespace(
            exists=True, reference=self, to_dict=lambda: dict(self._data)
        )


class FakeCollection:
    """A collection whose `.parent` is the document holding it."""

    def __init__(self, name: str, parent_document=None):
        self.id = name
        self.parent = parent_document


def _doc_in(run_id: str, group: str, index: int, *, run_collection="migration_runs"):
    """A subcollection document, wired with the parent chain the tool walks."""
    runs = FakeCollection(run_collection)
    run_document = FakeRef(f"{run_collection}/{run_id}", parent=runs)
    group_collection = FakeCollection(group, parent_document=run_document)
    ref = FakeRef(f"{run_collection}/{run_id}/{group}/{group}-{index}", parent=group_collection)
    return types.SimpleNamespace(reference=ref)


def _top_level_doc(group: str, index: int):
    """A document in a TOP-LEVEL collection of the same name.

    `policy_decisions` and `containment_events` exist both as run
    subcollections and as global collections. A global one has no parent
    document at all, and a tool whose premise is "the parent is missing"
    would treat every one of them as an orphan.
    """
    ref = FakeRef(f"{group}/{group}-{index}", parent=FakeCollection(group, parent_document=None))
    return types.SimpleNamespace(reference=ref)


class FakeClient:
    def __init__(self, groups: dict[str, list]):
        self._groups = groups
        self.deleted: list[str] = []

    def collection_group(self, name: str):
        docs = self._groups.get(name, [])
        return types.SimpleNamespace(
            select=lambda _fields: types.SimpleNamespace(stream=lambda: iter(docs)),
            stream=lambda: iter(docs),
        )

    def get_all(self, references):
        # Firestore returns missing documents too, and in no guaranteed
        # order. Reversing the chunk here is not gratuitous: it is the
        # cheapest way to prove the export takes each path from the
        # SNAPSHOT rather than pairing results back up positionally.
        return [ref.get() for ref in reversed(list(references))]

    def batch(self):
        client = self

        class Batch:
            def __init__(self):
                self._pending: list[str] = []

            def delete(self, ref):
                self._pending.append(ref.path)

            def commit(self):
                client.deleted.extend(self._pending)

        return Batch()


def test_a_document_whose_run_still_exists_is_never_touched():
    client = FakeClient({"catalog": [_doc_in("alive", "catalog", 1)]})
    assert purge_orphans.find_orphans(client, {"alive"}) == {}


def test_a_document_whose_run_is_gone_is_found():
    client = FakeClient({"catalog": [_doc_in("dead", "catalog", 1)]})
    orphans = purge_orphans.find_orphans(client, {"alive"})
    assert list(orphans) == ["dead"]
    assert len(orphans["dead"]["catalog"]) == 1


def test_a_top_level_collection_of_the_same_name_is_left_alone():
    """The failure that would have made this tool destructive.

    268 `policy_decisions` and 828 `containment_events` documents live at
    the top level of this project. They have no parent run by design, so
    a naive "no parent means orphan" rule would have deleted all 1,096
    of them — real governance records, not debris.
    """
    client = FakeClient(
        {"policy_decisions": [_top_level_doc("policy_decisions", i) for i in range(3)]}
    )
    assert purge_orphans.find_orphans(client, set()) == {}


def test_a_subcollection_nested_under_something_other_than_a_run_is_left_alone():
    client = FakeClient(
        {"catalog": [_doc_in("x", "catalog", 1, run_collection="estates")]}
    )
    assert purge_orphans.find_orphans(client, set()) == {}


def test_every_document_is_exported_before_it_is_deleted(tmp_path):
    docs = [_doc_in("dead", "catalog", i) for i in range(3)]
    destination = tmp_path / "export.jsonl"
    client = FakeClient({})
    written = purge_orphans.export(client, [doc.reference for doc in docs], destination)

    assert written == 3
    lines = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
    assert sorted(line["path"] for line in lines) == sorted(
        doc.reference.path for doc in docs
    )
    # The DATA, not just the path — a rescue copy that cannot restore the
    # contents is not a rescue copy.
    assert all(line["data"] == {"field": "value"} for line in lines)


def test_the_export_survives_firestore_types_that_are_not_json(tmp_path):
    import datetime as dt

    ref = FakeRef("migration_runs/dead/catalog/1", data={"at": dt.datetime(2026, 8, 20, 12, 0)})
    purge_orphans.export(FakeClient({}), [ref], tmp_path / "export.jsonl")
    line = json.loads((tmp_path / "export.jsonl").read_text(encoding="utf-8"))
    assert line["data"]["at"].startswith("2026-08-20T12:00")


def test_nothing_is_deleted_when_the_export_cannot_be_written(tmp_path, monkeypatch, capsys):
    """The one outcome this tool must never produce.

    An irreversible delete whose backup silently failed leaves no way
    back. It has to refuse instead.
    """
    client = FakeClient({"catalog": [_doc_in("dead", "catalog", 1)]})
    monkeypatch.setattr(purge_orphans, "get_client", lambda: client)
    monkeypatch.setattr(purge_orphans, "live_run_ids", lambda _client: set())
    monkeypatch.setattr(purge_orphans, "EXPORT_DIR", tmp_path / "exports")

    def refuse(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(purge_orphans, "export", refuse)

    assert purge_orphans.main(["--apply"]) == 1
    assert client.deleted == []
    assert "nothing deleted" in capsys.readouterr().err


def test_a_dry_run_deletes_nothing(tmp_path, monkeypatch, capsys):
    client = FakeClient({"catalog": [_doc_in("dead", "catalog", i) for i in range(2)]})
    monkeypatch.setattr(purge_orphans, "get_client", lambda: client)
    monkeypatch.setattr(purge_orphans, "live_run_ids", lambda _client: set())
    monkeypatch.setattr(purge_orphans, "EXPORT_DIR", tmp_path / "exports")

    assert purge_orphans.main([]) == 0
    assert client.deleted == []
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "orphaned documents: 2" in out


def test_apply_exports_then_deletes(tmp_path, monkeypatch):
    docs = [_doc_in("dead", "catalog", i) for i in range(4)]
    client = FakeClient({"catalog": docs})
    monkeypatch.setattr(purge_orphans, "get_client", lambda: client)
    monkeypatch.setattr(purge_orphans, "live_run_ids", lambda _client: set())
    monkeypatch.setattr(purge_orphans, "EXPORT_DIR", tmp_path / "exports")

    assert purge_orphans.main(["--apply"]) == 0
    assert sorted(client.deleted) == sorted(doc.reference.path for doc in docs)
    exports = list((tmp_path / "exports").glob("orphans-*.jsonl"))
    assert len(exports) == 1
    assert len(exports[0].read_text(encoding="utf-8").splitlines()) == 4


def test_deletes_are_batched_within_the_firestore_write_limit():
    """Firestore rejects a batch over 500 writes.

    With 9,891 documents to remove, an unbatched commit is not a
    performance question — it fails outright.
    """
    client = FakeClient({})
    refs = [FakeRef(f"migration_runs/dead/catalog/{i}") for i in range(1201)]
    assert purge_orphans.delete(client, refs) == 1201
    assert len(client.deleted) == 1201
    assert purge_orphans.BATCH_LIMIT <= 500


def test_deleting_a_run_removes_its_subcollections_too(monkeypatch):
    """The rule that stops the orphans coming back.

    `delete_run` deleted only the run document, which is how 9,891
    orphans accumulated in the first place. Tests DID clean up after
    themselves; the cleanup just did not reach far enough.
    """
    from agents.orchestrator import run_lifecycle

    recursive: list[str] = []
    plain: list[str] = []

    class Ref:
        def __init__(self, path):
            self.path = path

        def delete(self):
            plain.append(self.path)

    class Client:
        def collection(self, name):
            return types.SimpleNamespace(document=lambda doc_id: Ref(f"{name}/{doc_id}"))

        def recursive_delete(self, reference):
            recursive.append(reference.path)

    monkeypatch.setattr(run_lifecycle, "get_client", Client)
    run_lifecycle.delete_run("run-1")

    assert recursive == ["migration_runs/run-1"]
    assert plain == [], "the run document was deleted on its own, orphaning its subcollections"


def test_documents_are_fetched_in_bulk_not_one_at_a_time(tmp_path):
    """The first real run made this a measured problem, not a style point.

    `export` fetched each document with its own `ref.get()`. Those reads
    are latency-bound, so against an off-region project 10,053 documents
    ran at roughly 240 a minute — most of an hour before a single delete.
    """
    calls: list[int] = []

    class CountingClient(FakeClient):
        def get_all(self, references):
            references = list(references)
            calls.append(len(references))
            return [ref.get() for ref in references]

    refs = [FakeRef(f"migration_runs/dead/catalog/{i}") for i in range(750)]
    written = purge_orphans.export(CountingClient({}), refs, tmp_path / "export.jsonl")

    assert written == 750
    assert len(calls) == 3, f"expected 3 bulk reads at chunk 300, got {len(calls)}"
    assert calls == [300, 300, 150]
