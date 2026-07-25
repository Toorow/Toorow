"""Tests for _fetch_context_events using mirror read path (Story 4.4, AC6, AC11).

Covers:
  - test_fetch_from_mirror: mirror table populated -> returns events.
  - test_fetch_mirror_not_synced: mirror table absent -> returns [].
  - test_fetch_duckdb_path_not_set: no TOOROW_DUCKDB_PATH -> returns [].
  - test_fetch_generic_duckdb_error: unexpected DuckDB error -> returns [].

The _fetch_context_events helper was upgraded from Postgres direct read (Story 4.3)
to mirror read (Story 4.4, AC6) so there is ONE analytical read path (AD-12/NFR6).
"""

from __future__ import annotations

from datetime import date


def test_fetch_from_mirror(tmp_path, monkeypatch):
    """mirror.context_events populated -> _fetch_context_events returns events."""
    import duckdb

    db_path = str(tmp_path / "mirror_test.duckdb")

    # Pre-populate mirror.context_events in DuckDB
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR,
            project_id VARCHAR,
            event_date DATE,
            type VARCHAR,
            label VARCHAR
        )
        """
    )
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?)",
        ["evt_001", "proj_test", date(2026, 7, 4), "business", "Lancement"],
    )
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")

    assert len(result) == 1
    assert result[0]["id"] == "evt_001"
    assert result[0]["project_id"] == "proj_test"
    assert result[0]["type"] == "business"
    assert result[0]["label"] == "Lancement"
    assert "event_date" in result[0]


def test_fetch_mirror_not_synced(tmp_path, monkeypatch):
    """mirror.context_events absent -> _fetch_context_events returns []."""
    import duckdb

    db_path = str(tmp_path / "empty_mirror.duckdb")

    # Create the DuckDB file but do NOT create mirror schema/table
    conn = duckdb.connect(db_path)
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")

    assert result == []


def test_fetch_duckdb_path_not_set(monkeypatch):
    """TOOROW_DUCKDB_PATH unset -> _fetch_context_events returns []."""
    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")

    assert result == []


def test_fetch_filters_by_project_id(tmp_path, monkeypatch):
    """Only events for the requested project_id are returned."""
    import duckdb

    db_path = str(tmp_path / "multi_proj.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR, label VARCHAR
        )
        """
    )
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?)",
        ["evt_A", "proj_A", date(2026, 7, 1), "business", "Event A"],
    )
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?)",
        ["evt_B", "proj_B", date(2026, 7, 2), "incident", "Event B"],
    )
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_A", "2026-07-01", "2026-07-31")

    assert len(result) == 1
    assert result[0]["id"] == "evt_A"


def test_fetch_filters_by_date_range(tmp_path, monkeypatch):
    """Events outside the date range are excluded."""
    import duckdb

    db_path = str(tmp_path / "date_filter.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR, label VARCHAR
        )
        """
    )
    for i, d in enumerate(["2026-06-30", "2026-07-01", "2026-07-15", "2026-08-01"]):
        conn.execute(
            "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?)",
            [f"evt_{i}", "proj_test", date.fromisoformat(d), "business", f"Event {i}"],
        )
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")

    dates = [r["event_date"] for r in result]
    # Only July 1 and July 15 should appear, not June 30 or Aug 1
    assert "2026-06-30" not in dates
    assert "2026-08-01" not in dates
    assert "2026-07-01" in dates
    assert "2026-07-15" in dates


# ---------------------------------------------------------------------------
# Epic 31.2 -- the read path exposes platform/value/source (overlay + MMM)
# ---------------------------------------------------------------------------


def test_fetch_exposes_mmm_columns_when_present(tmp_path, monkeypatch):
    """migration-055 columns (platform/value/source) reach the analytical read path.

    Epic 31.2: the single read path feeds BOTH the annotation overlay AND the MMM
    feature-builder, so platform (identity level 1), value (regressor magnitude) and
    source (provenance) must be selected when the mirror has them.
    """
    import duckdb

    db_path = str(tmp_path / "mmm_cols.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR,
            label VARCHAR, platform VARCHAR, value DOUBLE, source VARCHAR
        )
        """
    )
    # A connector event with a magnitude (weighted regressor)...
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["evt_p", "proj_test", date(2026, 7, 4), "promotion", "Promo -20",
         "shopify", -20.0, "shopify"],
    )
    # ...and a unit pulse (value NULL) from a manual/legacy event.
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ["evt_u", "proj_test", date(2026, 7, 5), "video_upload", "New video",
         "youtube", None, "youtube-analytics"],
    )
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")
    by_id = {r["id"]: r for r in result}

    assert by_id["evt_p"]["platform"] == "shopify"
    assert by_id["evt_p"]["value"] == -20.0  # NUMERIC -> float
    assert by_id["evt_p"]["source"] == "shopify"
    # NULL value stays None -> the default unit pulse (AD-9: no black box).
    assert by_id["evt_u"]["value"] is None
    assert by_id["evt_u"]["platform"] == "youtube"
    assert by_id["evt_u"]["source"] == "youtube-analytics"


def test_fetch_backward_compatible_with_legacy_5col_mirror(tmp_path, monkeypatch):
    """A pre-055 mirror (no platform/value/source) still returns the base columns.

    Defensive column selection: the read path intersects its wanted columns with the
    table's actual columns, so an un-migrated mirror does not raise on absent columns.
    """
    import duckdb

    db_path = str(tmp_path / "legacy_5col.duckdb")
    conn = duckdb.connect(db_path)
    conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    conn.execute(
        """
        CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR, label VARCHAR
        )
        """
    )
    conn.execute(
        "INSERT INTO mirror.context_events VALUES (?, ?, ?, ?, ?)",
        ["evt_legacy", "proj_test", date(2026, 7, 4), "business", "Legacy"],
    )
    conn.close()

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    from core.main import _fetch_context_events

    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")

    assert len(result) == 1
    row = result[0]
    assert row["id"] == "evt_legacy"
    assert row["label"] == "Legacy"
    # MMM columns simply absent (not present in the legacy mirror) -- no crash.
    assert "platform" not in row
    assert "value" not in row
    assert "source" not in row
