"""Estimated bytes: measured from the source, or honestly absent.

The Overview panel said "Source byte estimates are not recorded" because
nothing recorded them. Adapters now read the size out of the source's own
catalog during discovery — sys.allocation_units on SQL Server,
pg_total_relation_size on Postgres.

The tests that matter here are not about arithmetic. They are about the
difference between "no bytes", "not measured" and "measured in part",
which a volume figure quoted in a migration proposal has to keep straight.
"""

from __future__ import annotations

from frontend.api_v1 import _estimated_bytes


def _table(table_id: str, size: int | None, at: str = "2026-08-20T10:00:00Z") -> dict:
    return {"table_id": table_id, "size_bytes": size, "discovered_at": at}


def test_nothing_discovered_is_not_the_same_as_nothing_measured():
    result = _estimated_bytes([])
    assert result["status"] == "not_configured"
    assert "discovered" in result["reason"]


def test_a_fully_measured_estate_reports_the_total():
    result = _estimated_bytes([_table("a", 1000), _table("b", 2400)])
    assert result["status"] == "available"
    assert result["value"]["bytes"] == 3400
    assert result["value"]["complete"] is True
    assert result["value"]["tables_measured"] == 2


def test_a_partly_measured_estate_says_the_total_is_a_floor():
    """The failure this panel exists to prevent.

    A mixed estate — a live database plus a .sql corpus that has no
    stored bytes to report — produces a number that is real but
    incomplete. Presented bare it reads as the size of the estate, and
    someone sizes a migration against it.
    """
    result = _estimated_bytes([_table("live", 5_000), _table("corpus", None)])
    assert result["status"] == "available"
    assert result["value"]["bytes"] == 5_000
    assert result["value"]["complete"] is False
    assert result["value"]["tables_measured"] == 1
    assert result["value"]["tables_total"] == 2
    assert "floor" in result["reason"]


def test_a_catalogue_with_no_sizes_at_all_is_not_reported_as_zero_bytes():
    """Zero would be a claim. This is an absence.

    Every table discovered before byte measurement existed carries no
    size_bytes, and summing those to 0 would tell an operator their
    estate is empty.
    """
    result = _estimated_bytes([_table("a", None), _table("b", None)])
    assert result["status"] == "not_configured"
    assert result["value"] is None
    assert "Re-run discovery" in result["reason"]


def test_a_genuinely_empty_table_still_counts_as_measured():
    """0 and None mean different things and must not be conflated.

    A table that really holds no bytes was measured; a table that could
    not be asked was not. Treating 0 as missing would make an empty
    estate indistinguishable from an unmeasured one.
    """
    result = _estimated_bytes([_table("empty", 0), _table("full", 128)])
    assert result["status"] == "available"
    assert result["value"]["tables_measured"] == 2
    assert result["value"]["complete"] is True
    assert result["value"]["bytes"] == 128


def test_the_largest_table_is_named_so_the_number_can_be_checked():
    result = _estimated_bytes([_table("small", 10), _table("huge", 9_000), _table("mid", 500)])
    assert result["value"]["largest_table"] == "huge"


def test_the_measurement_carries_when_it_was_taken():
    result = _estimated_bytes(
        [_table("a", 1, at="2026-08-01T00:00:00Z"), _table("b", 2, at="2026-08-19T00:00:00Z")]
    )
    # The newest, because that is when this figure was last true.
    assert result["last_observed_at"] == "2026-08-19T00:00:00Z"
