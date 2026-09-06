"""Tests for server/core/mirror_sync.py (Story 4.4, AC11; Story 11.3).

Covers:
  - test_sync_context_events: full sync to DuckDB; rows match Postgres source.
  - test_sync_returns_lag: lag_seconds in result, value is float >= 0.
  - test_sync_missing_postgres: Postgres unavailable -> returns error dict, no crash.
  - test_sync_connection_ref_dim: only project_id, connector_name, display_name columns.
  - test_sync_returns_synced_at: synced_at key present in successful result.
  - test_last_sync_result_updated: _last_sync_result module var is set after sync.

Story 11.3 additions:
  - test_context_tables_in_allowlist: all four context tables are in _ALLOWED_TABLES.
  - test_context_tables_in_defaults: all four context tables are in _DEFAULT_TABLES.
  - test_sync_context_topics: context_topics rows mirror to DuckDB correctly.
  - test_sync_procedures: procedures rows mirror to DuckDB correctly.
  - test_sync_context_graph: context_graph rows mirror to DuckDB correctly.
  - test_sync_schema_context: schema_context rows mirror to DuckDB correctly.
  - test_sync_all_context_tables_together: all four sync in one call, lag surfaced.
  - test_warehouse_mirror_is_read_only: DuckDB mirror exposes no write path to callers.
  - test_postgres_is_sole_writer: warehouse write is only triggered by sync_tables (AD-8).
  - test_unknown_context_table_rejected: a typo or unlisted name is still rejected.
  - test_existing_tables_not_broken: pre-11.3 tables still sync successfully.

All tests mock both Postgres and DuckDB connections (no real DB required).
Real-Postgres tests are guarded with skipif (same pattern as existing tests).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers — mock DB infrastructure
# ---------------------------------------------------------------------------

def _make_pg_cursor(rows: list[tuple], col_names: list[str]) -> MagicMock:
    """Build a mock psycopg cursor that returns rows with column descriptors."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall.return_value = rows
    cur.description = [(c,) for c in col_names]
    return cur


def _make_pg_conn(cursor: MagicMock) -> MagicMock:
    """Build a mock psycopg connection."""
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor
    return conn


@contextmanager
def _fake_pg_get_connection(cursor: MagicMock):
    """Context manager yielding a mock psycopg connection."""
    yield _make_pg_conn(cursor)


def _fake_duckdb_conn() -> MagicMock:
    """Mock DuckDB connection that records SQL calls without executing."""
    duck = MagicMock()
    duck.execute = MagicMock()
    duck.register = MagicMock()
    duck.unregister = MagicMock()
    duck.close = MagicMock()
    return duck


# ---------------------------------------------------------------------------
# test_sync_context_events
# ---------------------------------------------------------------------------


def test_sync_context_events(tmp_path):
    """sync_tables(['context_events']) reads Postgres and writes to DuckDB mirror."""
    from datetime import date, datetime, timezone

    db_path = str(tmp_path / "test.duckdb")
    col_names = [
        "id", "project_id", "event_date", "type", "label",
        "description", "created_by", "created_at",
    ]
    pg_rows = [
        ("evt_01", "proj_test", date(2026, 7, 1), "business", "Launch", None, "user1",
         datetime(2026, 7, 1, tzinfo=timezone.utc)),
        ("evt_02", "proj_test", date(2026, 7, 5), "incident", "Outage", None, "user2",
         datetime(2026, 7, 5, tzinfo=timezone.utc)),
    ]
    mock_cursor = _make_pg_cursor(pg_rows, col_names)

    import duckdb
    real_duck = duckdb.connect(db_path)
    real_duck.close()

    with (
        patch("core.db.get_connection",
              side_effect=lambda: _fake_pg_get_connection(mock_cursor)),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        # Patch duckdb.connect to use a real DuckDB file (tmp_path)
        result = mirror_sync.sync_tables(["context_events"])

    assert "synced" in result
    assert "context_events" in result["synced"]
    assert result["synced"]["context_events"] == 2


def test_sync_returns_lag(tmp_path):
    """sync_tables result includes lag_seconds as float >= 0."""
    db_path = str(tmp_path / "lag_test.duckdb")
    col_names = ["id", "project_id", "event_date", "type", "label"]
    mock_cursor = _make_pg_cursor([], col_names)

    with (
        patch("core.db.get_connection",
              side_effect=lambda: _fake_pg_get_connection(mock_cursor)),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        result = mirror_sync.sync_tables(["context_events"])

    assert "lag_seconds" in result
    assert isinstance(result["lag_seconds"], float)
    assert result["lag_seconds"] >= 0.0


def test_sync_missing_postgres():
    """When Postgres is unavailable, returns error dict without crashing."""
    with patch("core.db.get_connection", side_effect=Exception("Connection refused")):
        from core import mirror_sync
        result = mirror_sync.sync_tables(["context_events"])

    assert "error" in result
    assert "synced" in result
    # synced may be empty or partial — no crash
    assert isinstance(result["synced"], dict)


def test_sync_connection_ref_dim_columns(tmp_path):
    """mirror.connection_ref_dim only contains project_id, connector_name, display_name."""
    db_path = str(tmp_path / "col_test.duckdb")
    # connection_ref_dim uses explicit column select (AD-3 no token leakage)
    col_names = ["project_id", "connector_name", "display_name"]
    pg_rows = [
        ("proj_test", "google-analytics", "Google Analytics"),
    ]
    mock_cursor = _make_pg_cursor(pg_rows, col_names)

    with (
        patch("core.db.get_connection",
              side_effect=lambda: _fake_pg_get_connection(mock_cursor)),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        result = mirror_sync.sync_tables(["connection_ref_dim"])

    assert result["synced"]["connection_ref_dim"] == 1


def test_sync_returns_synced_at(tmp_path):
    """Successful sync includes synced_at as an ISO-8601 timestamp string."""
    db_path = str(tmp_path / "ts_test.duckdb")
    col_names = ["project_id", "canonical_currency", "reporting_timezone"]
    pg_rows = [("default", "EUR", "Europe/Paris")]
    mock_cursor = _make_pg_cursor(pg_rows, col_names)

    with (
        patch("core.db.get_connection",
              side_effect=lambda: _fake_pg_get_connection(mock_cursor)),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        result = mirror_sync.sync_tables(["project_preferences"])

    assert "synced_at" in result
    # ISO-8601 has 'T' separator
    assert "T" in result["synced_at"]


def test_last_sync_result_updated(tmp_path):
    """_last_sync_result module-level var is set after a sync (AC9)."""
    db_path = str(tmp_path / "health_test.duckdb")
    col_names = ["id", "project_id", "event_date", "type", "label"]
    mock_cursor = _make_pg_cursor([], col_names)

    with (
        patch("core.db.get_connection",
              side_effect=lambda: _fake_pg_get_connection(mock_cursor)),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        mirror_sync._last_sync_result = None  # reset
        mirror_sync.sync_tables(["context_events"])

    assert mirror_sync._last_sync_result is not None
    assert "synced" in mirror_sync._last_sync_result


# ---------------------------------------------------------------------------
# test_sync_enabled_guard — HG-3
# ---------------------------------------------------------------------------


def test_sync_enabled_false_skips_sync_in_scheduler():
    """SYNC_ENABLED=false -> dispatch_nightly does NOT call sync_tables (HG-3)."""
    from contextlib import contextmanager
    from datetime import date

    @contextmanager
    def _no_op_conn():
        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchall.return_value = []
        mock_cur.description = [(c,) for c in ["id", "provider", "project_id"]]
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor.return_value = mock_cur
        yield mock_conn

    with (
        patch.dict(os.environ, {"SYNC_ENABLED": "false"}),
        patch("core.db.get_connection", side_effect=_no_op_conn),
        patch("core.queue.enqueue_pull", return_value={}),
        patch("core.mirror_sync.sync_tables") as mock_sync,
    ):
        from core.scheduler import dispatch_nightly
        dispatch_nightly(as_of_date=date(2026, 7, 11))

    mock_sync.assert_not_called()


# ---------------------------------------------------------------------------
# Story 11.3: Knowledge warehouse mirror tests
# ---------------------------------------------------------------------------

_CONTEXT_TABLES = ["context_topics", "procedures", "context_graph", "schema_context"]


def test_context_tables_in_allowlist():
    """All four knowledge-context tables must appear in _ALLOWED_TABLES (11.3)."""
    from core import mirror_sync

    for table in _CONTEXT_TABLES:
        assert table in mirror_sync._ALLOWED_TABLES, (
            f"mirror_sync._ALLOWED_TABLES is missing '{table}' (Story 11.3)"
        )


def test_context_tables_in_defaults():
    """All four knowledge-context tables must appear in _DEFAULT_TABLES (11.3)."""
    from core import mirror_sync

    for table in _CONTEXT_TABLES:
        assert table in mirror_sync._DEFAULT_TABLES, (
            f"mirror_sync._DEFAULT_TABLES is missing '{table}' (Story 11.3)"
        )


def test_sync_context_topics(tmp_path):
    """context_topics rows are mirrored from Postgres to DuckDB (Story 11.3)."""
    from datetime import datetime, timezone

    db_path = str(tmp_path / "ctx_topics.duckdb")
    col_names = [
        "id", "project_id", "title", "body_md", "status",
        "created_by", "created_at", "updated_at",
    ]
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    pg_rows = [
        ("top_01", None, "Revenue", "Revenue is GMV - refunds.", "active", "alice", now, now),
        ("top_02", "proj_a", "ROAS", "Return on ad spend.", "active", "bob", now, now),
    ]
    mock_cursor = _make_pg_cursor(pg_rows, col_names)

    with (
        patch("core.db.get_connection",
              side_effect=lambda: _fake_pg_get_connection(mock_cursor)),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        result = mirror_sync.sync_tables(["context_topics"])

    assert result["synced"]["context_topics"] == 2
    assert "lag_seconds" in result
    assert result["lag_seconds"] >= 0.0


def test_sync_procedures(tmp_path):
    """procedures rows are mirrored from Postgres to DuckDB (Story 11.3)."""
    from datetime import datetime, timezone

    db_path = str(tmp_path / "procedures.duckdb")
    col_names = [
        "id", "project_id", "name", "description",
        "frontmatter_yaml", "body_md", "status",
        "created_by", "created_at", "updated_at",
    ]
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    pg_rows = [
        (
            "proc_01", None, "weekly-review",
            "Weekly analytics review procedure.",
            "name: weekly-review\ndescription: Weekly analytics review procedure.",
            "## Steps\n1. Check revenue.\n2. Check ROAS.",
            "active", "alice", now, now,
        ),
    ]
    mock_cursor = _make_pg_cursor(pg_rows, col_names)

    with (
        patch("core.db.get_connection",
              side_effect=lambda: _fake_pg_get_connection(mock_cursor)),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        result = mirror_sync.sync_tables(["procedures"])

    assert result["synced"]["procedures"] == 1
    assert "lag_seconds" in result
    assert result["lag_seconds"] >= 0.0


def test_sync_context_graph(tmp_path):
    """context_graph rows are mirrored from Postgres to DuckDB (Story 11.3)."""
    from datetime import datetime, timezone

    db_path = str(tmp_path / "ctx_graph.duckdb")
    col_names = [
        "id", "from_id", "from_type", "to_id", "to_type",
        "edge_type", "project_id", "created_by", "created_at",
    ]
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    pg_rows = [
        ("edge_01", "top_01", "topic", "proc_01", "procedure", "related", None, "alice", now),
        ("edge_02", "top_02", "topic", "top_01", "topic", "child_of", "proj_a", "bob", now),
    ]
    mock_cursor = _make_pg_cursor(pg_rows, col_names)

    with (
        patch("core.db.get_connection",
              side_effect=lambda: _fake_pg_get_connection(mock_cursor)),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        result = mirror_sync.sync_tables(["context_graph"])

    assert result["synced"]["context_graph"] == 2
    assert "lag_seconds" in result
    assert result["lag_seconds"] >= 0.0


def test_sync_schema_context(tmp_path):
    """schema_context rows are mirrored from Postgres to DuckDB (Story 11.3)."""
    from datetime import datetime, timezone

    db_path = str(tmp_path / "schema_ctx.duckdb")
    col_names = [
        "id", "project_id", "relation", "doc_kind",
        "body_md", "generated_at", "created_at",
    ]
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    pg_rows = [
        ("sctx_01", "proj_a", "mart_revenue", "columns",
         "| col | type |\n|-----|------|\n| revenue | FLOAT |", now, now),
        ("sctx_02", "proj_a", "mart_revenue", "description",
         "Daily revenue mart.", now, now),
    ]
    mock_cursor = _make_pg_cursor(pg_rows, col_names)

    with (
        patch("core.db.get_connection",
              side_effect=lambda: _fake_pg_get_connection(mock_cursor)),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        result = mirror_sync.sync_tables(["schema_context"])

    assert result["synced"]["schema_context"] == 2
    assert "lag_seconds" in result
    assert result["lag_seconds"] >= 0.0


def test_sync_all_context_tables_together(tmp_path):
    """All four context tables sync together in one call; lag is surfaced (Story 11.3)."""
    from datetime import datetime, timezone

    db_path = str(tmp_path / "all_ctx.duckdb")
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

    # Each call to get_connection must return a different cursor depending on
    # which table is being fetched. We cycle through predetermined returns.
    table_rows = {
        "context_topics": (
            ["id", "project_id", "title", "body_md", "status",
             "created_by", "created_at", "updated_at"],
            [("top_01", None, "Revenue", "body", "active", "alice", now, now)],
        ),
        "procedures": (
            ["id", "project_id", "name", "description",
             "frontmatter_yaml", "body_md", "status",
             "created_by", "created_at", "updated_at"],
            [("proc_01", None, "weekly-review", "desc",
              "name: weekly-review\ndescription: desc", "body", "active", "alice", now, now)],
        ),
        "context_graph": (
            ["id", "from_id", "from_type", "to_id", "to_type",
             "edge_type", "project_id", "created_by", "created_at"],
            [("edge_01", "top_01", "topic", "proc_01", "procedure", "related", None, "alice", now)],
        ),
        "schema_context": (
            ["id", "project_id", "relation", "doc_kind",
             "body_md", "generated_at", "created_at"],
            [("sctx_01", "proj_a", "mart_revenue", "columns", "| col |", now, now)],
        ),
    }

    call_order: list[str] = []

    def _make_cursor_for_table(table: str) -> MagicMock:
        col_names, pg_rows = table_rows[table]
        call_order.append(table)
        return _make_pg_cursor(pg_rows, col_names)

    # We intercept at the _fetch_from_postgres level to route by table name.
    def _patched_fetch(table: str):
        col_names, pg_rows = table_rows[table]
        call_order.append(table)
        cols = col_names
        rows = [dict(zip(col_names, row)) for row in pg_rows]
        # Third element: the DECLARED Postgres types. An empty map means "no
        # declaration available", which keeps the old inference path -- exactly
        # what a mock that knows no types should ask for.
        return cols, rows, {}

    with (
        patch("core.mirror_sync._fetch_from_postgres", side_effect=_patched_fetch),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        result = mirror_sync.sync_tables(_CONTEXT_TABLES)

    # All four tables synced exactly one row each.
    assert set(result["synced"].keys()) == set(_CONTEXT_TABLES)
    for table in _CONTEXT_TABLES:
        assert result["synced"][table] == 1, f"Expected 1 row for {table}"

    # Lag is a non-negative float (AD-8 single-path surface).
    assert "lag_seconds" in result
    assert isinstance(result["lag_seconds"], float)
    assert result["lag_seconds"] >= 0.0

    # Each table was fetched exactly once (no duplicate reads).
    assert sorted(call_order) == sorted(_CONTEXT_TABLES)


def test_warehouse_mirror_is_read_only():
    """The mirror.* schema exposes no write API to callers — only sync_tables writes (AD-8).

    Proof: mirror_sync exports no function that writes to DuckDB except sync_tables
    (and its private helper _write_to_duckdb). All public names are inspected; none
    other than sync_tables accepts warehouse write parameters.
    """
    import inspect

    from core import mirror_sync

    public_functions = {
        name: obj
        for name, obj in inspect.getmembers(mirror_sync, inspect.isfunction)
        if not name.startswith("_")
    }

    # The only public write surface is sync_tables.
    assert "sync_tables" in public_functions, "sync_tables must be exported"

    # No other public function writes to DuckDB (i.e., accepts db_path or
    # executes DuckDB writes). We verify that _write_to_duckdb is private.
    assert "_write_to_duckdb" not in public_functions, (
        "_write_to_duckdb must remain private — no direct write access for callers"
    )

    # No public function other than sync_tables takes a 'db_path' parameter
    # (which is the DuckDB write gate).
    for name, fn in public_functions.items():
        if name == "sync_tables":
            continue
        sig = inspect.signature(fn)
        assert "db_path" not in sig.parameters, (
            f"Public function '{name}' has 'db_path' param — must not expose DuckDB writes"
        )


def test_postgres_is_sole_writer(tmp_path):
    """Warehouse mirror rows originate exclusively from Postgres (AD-8).

    Proof: patching _fetch_from_postgres to return controlled data and verifying
    that DuckDB receives exactly those rows — no other write path exists.
    """
    db_path = str(tmp_path / "sole_writer.duckdb")

    sentinel_rows = [{"id": "top_SENTINEL", "project_id": None,
                      "title": "Sentinel", "body_md": "sole-writer-proof",
                      "status": "active", "created_by": "test",
                      "created_at": "2026-07-20", "updated_at": "2026-07-20"}]
    sentinel_cols = list(sentinel_rows[0].keys())

    with (
        patch("core.mirror_sync._fetch_from_postgres",
              return_value=(sentinel_cols, sentinel_rows, {})),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        result = mirror_sync.sync_tables(["context_topics"])

    # One row mirrored — the exact sentinel from Postgres.
    assert result["synced"]["context_topics"] == 1

    # Confirm DuckDB received the sentinel (not some other source).
    import duckdb
    conn = duckdb.connect(db_path, read_only=True)
    try:
        rows = conn.execute("SELECT id FROM mirror.context_topics").fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0][0] == "top_SENTINEL", (
        "DuckDB mirror must contain exactly the row from Postgres — no other write path (AD-8)"
    )


def test_unknown_context_table_rejected(caplog):
    """A table name not in _ALLOWED_TABLES is rejected with a warning, not synced (11.3)."""
    import logging

    with (
        patch("core.db.get_connection", side_effect=Exception("should not be called")),
        caplog.at_level(logging.WARNING, logger="core.mirror_sync"),
    ):
        from core import mirror_sync
        # 'context_topics_versions' is the versions table — not in the allowlist.
        result = mirror_sync.sync_tables(["context_topics_versions"])

    # No rows synced (table was rejected before any Postgres call).
    assert result["synced"] == {}
    # Warning emitted.
    assert any("unknown_table_ignored" in rec.message for rec in caplog.records), (
        "Expected 'unknown_table_ignored' warning for unlisted table"
    )


def test_existing_tables_not_broken(tmp_path):
    """Pre-11.3 tables (context_events, project_preferences, alert_definitions) still sync."""
    db_path = str(tmp_path / "pre113.duckdb")

    pre_tables = ["context_events", "project_preferences", "alert_definitions"]
    col_names = ["id", "project_id"]
    pg_rows = [("row_01", "proj_x")]
    mock_cursor = _make_pg_cursor(pg_rows, col_names)

    with (
        patch("core.db.get_connection",
              side_effect=lambda: _fake_pg_get_connection(mock_cursor)),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        for table in pre_tables:
            result = mirror_sync.sync_tables([table])
            assert table in result["synced"], f"Pre-11.3 table '{table}' must still sync"
            assert result["synced"][table] == 1, f"Expected 1 row for pre-11.3 table '{table}'"


def test_an_all_null_text_column_stays_text_in_the_mirror(tmp_path):
    """The mirror must carry the DECLARED type, not one inferred from the values.

    Before this, `_write_to_duckdb` built a pandas DataFrame from the rows and
    let DuckDB infer. A nullable TEXT column that happens to hold only NULLs for
    a tenant became INTEGER in the warehouse, and every reader and fixture that
    put a string in it failed with "Could not convert string 'EUR' to INT32".

    Reachable in production, not theoretical: Story 48.3 dropped the 'EUR' and
    'Europe/Paris' defaults on app.project_preferences on purpose -- a reporting
    currency is chosen, never defaulted -- so a Project that had not chosen yet
    made both columns all-NULL. The class is every nullable column empty for a
    tenant, and the failure surfaces in the warehouse, far from its cause.
    """
    import duckdb
    from core import mirror_sync

    db_path = str(tmp_path / "typed.duckdb")
    cols = ["project_id", "canonical_currency", "reporting_timezone"]
    rows = [{"project_id": "proj_EXAMPLE", "canonical_currency": None, "reporting_timezone": None}]
    types = {
        "project_id": "VARCHAR",
        "canonical_currency": "VARCHAR",
        "reporting_timezone": "VARCHAR",
    }

    mirror_sync._write_to_duckdb("project_preferences", cols, rows, db_path, types)

    conn = duckdb.connect(db_path)
    observed = dict(
        conn.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'mirror' AND table_name = 'project_preferences'"
        ).fetchall()
    )
    conn.close()
    assert observed["canonical_currency"] == "VARCHAR", observed
    assert observed["reporting_timezone"] == "VARCHAR", observed

    # And the value a caller would then write must land, which is the whole
    # point: the type assertion above is only interesting because of this.
    conn = duckdb.connect(db_path)
    conn.execute(
        "UPDATE mirror.project_preferences SET canonical_currency = 'EUR' "
        "WHERE project_id = 'proj_EXAMPLE'"
    )
    stored = conn.execute("SELECT canonical_currency FROM mirror.project_preferences").fetchone()
    conn.close()
    assert stored == ("EUR",)


# ---------------------------------------------------------------------------
# Story 64.9 (AI-232): the governed node joins the warehouse.
#
# The gap these tests pin: before 64.9 the mirror carried the CLIENT's own
# correspondence table (`reference_tables`) and not the GOVERNED node behind it,
# so a dbt model could resolve a client vocabulary but never a governed identity.
# ---------------------------------------------------------------------------

_MASTER_DATA_MIRROR = ["master_data_nodes_dim", "master_data_aliases_dim"]


def test_master_data_entries_are_registered_everywhere():
    """An entry absent from any one of the four registries never syncs (64.9).

    Four, not one: `_DEFAULT_TABLES` selects it, `_ALLOWED_TABLES` admits it,
    `_CURATED_SQL` gives it its projection, `_GUARDED_RELATIONS` lets the sync
    survive a deploy that precedes migration 233.
    """
    from core import mirror_sync

    for table in _MASTER_DATA_MIRROR:
        assert table in mirror_sync._DEFAULT_TABLES, f"_DEFAULT_TABLES misses {table}"
        assert table in mirror_sync._ALLOWED_TABLES, f"_ALLOWED_TABLES misses {table}"
        assert table in mirror_sync._CURATED_SQL, f"_CURATED_SQL misses {table}"
        assert table in mirror_sync._GUARDED_RELATIONS, f"_GUARDED_RELATIONS misses {table}"


def test_master_data_mirror_names_carry_no_view_suffix():
    """The mirror name is the clean one; only the SQL behind it names the view.

    The file's own rule -- "the mirror entry names stay clean (no _v suffix
    leaking into dbt)". A `_v` in the entry name would put the projection's
    implementation into every dbt `source()`.
    """
    from core import mirror_sync

    for table in _MASTER_DATA_MIRROR:
        assert not table.endswith("_v"), f"{table} leaks the view suffix into dbt"
        assert mirror_sync._GUARDED_RELATIONS[table].endswith("_v")
        assert mirror_sync._CURATED_SQL[table] == (
            f"SELECT * FROM {mirror_sync._GUARDED_RELATIONS[table]}"
        )


def test_master_data_views_project_scalars_only():
    """Migration 233 must not project a JSONB column (64.9).

    `master_data_aliases.evidence` is JSONB. The fee/tax precedent mirrors
    RELATIONALLY, never a raw object or array, and a JSONB column reaching DuckDB
    is the drift that rule exists to stop. Read from the migration text so the
    assertion cannot pass against a view that was later widened.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    sql = (
        root / "infra" / "nango" / "migrations"
        / "233_a_governed_node_is_joinable_in_the_warehouse.sql"
    ).read_text(encoding="utf-8")

    body = sql[sql.index("CREATE OR REPLACE VIEW app.master_data_aliases_dim_v"):]
    select_list = body[: body.index("FROM app.master_data_aliases")]
    assert "evidence" not in select_list, "JSONB `evidence` must not be mirrored"


def test_master_data_views_exclude_retired_and_archived_rows():
    """An archived node or a retired alias must not resolve a value (64.9).

    Nothing is deleted upstream -- an old Result pins a version and must stay
    reproducible -- so the WHERE clause is the ONLY thing that retires an
    identity. Without it the mirror would keep resolving values through nodes
    their owner has withdrawn.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    sql = (
        root / "infra" / "nango" / "migrations"
        / "233_a_governed_node_is_joinable_in_the_warehouse.sql"
    ).read_text(encoding="utf-8")

    nodes = sql[sql.index("CREATE OR REPLACE VIEW app.master_data_nodes_dim_v"):]
    assert "WHERE archived_at IS NULL" in nodes[: nodes.index("COMMENT ON VIEW")]

    aliases = sql[sql.index("CREATE OR REPLACE VIEW app.master_data_aliases_dim_v"):]
    assert "WHERE retired_at IS NULL" in aliases[: aliases.index("COMMENT ON VIEW")]


def test_master_data_alias_view_carries_relation_and_conflict_unresolved():
    """The matching policy belongs to 64.10, at build time -- not to this view.

    `relation` distinguishes exact from close, and SKOS is explicit that a close
    match is not transitive; `conflict_state` records a contradiction the server
    refuses to resolve by write order. A view that dropped either would force the
    macro to guess, or to treat every alias as an equality.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    sql = (
        root / "infra" / "nango" / "migrations"
        / "233_a_governed_node_is_joinable_in_the_warehouse.sql"
    ).read_text(encoding="utf-8")

    body = sql[sql.index("CREATE OR REPLACE VIEW app.master_data_aliases_dim_v"):]
    select_list = body[: body.index("FROM app.master_data_aliases")]
    for column in ("relation", "conflict_state", "effective_from", "effective_to",
                   "confidence", "namespace", "normalized_value", "raw_value"):
        assert column in select_list, f"the resolver needs {column}"
