"""Tests for context_events: add_context_event MCP tool and _fetch_context_events helper.

Story 4.3, AC8 (T9.1).

Covers:
  - add_context_event: valid call inserts row + returns French ack text.
  - add_context_event: label >120 chars raises ToolError.
  - add_context_event: invalid date raises ToolError.
  - _fetch_context_events: returns filtered rows from DB.
  - _fetch_context_events: returns [] when DB unavailable.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _validate_event_input tests
# ---------------------------------------------------------------------------


def test_validate_event_input_valid():
    """Valid label and date pass without raising."""
    from core.main import _validate_event_input

    # Should not raise
    _validate_event_input("Lancement campagne ete", "2026-07-04")


def test_validate_event_input_label_too_long():
    """Label exceeding 120 chars raises ToolError with French message."""
    from core.main import _validate_event_input
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    long_label = "a" * 121
    with pytest.raises(ToolError) as exc_info:
        _validate_event_input(long_label, "2026-07-04")
    assert "120" in str(exc_info.value)


def test_validate_event_input_invalid_date():
    """Invalid ISO date raises ToolError."""
    from core.main import _validate_event_input
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    with pytest.raises(ToolError) as exc_info:
        _validate_event_input("Bon label", "not-a-date")
    assert "event_date" in str(exc_info.value)


def test_validate_event_input_date_wrong_format():
    """Date in wrong format (DD/MM/YYYY) raises ToolError."""
    from core.main import _validate_event_input
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    with pytest.raises(ToolError):
        _validate_event_input("Bon label", "04/07/2026")


def test_validate_event_input_label_exactly_120():
    """Label of exactly 120 chars passes validation."""
    from core.main import _validate_event_input

    label = "a" * 120
    _validate_event_input(label, "2026-07-04")  # should not raise


# ---------------------------------------------------------------------------
# add_context_event tool tests
# ---------------------------------------------------------------------------


def _make_mock_cursor(fetchall_result=None, description=None):
    """Build a mock psycopg cursor."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    if fetchall_result is not None:
        cur.fetchall.return_value = fetchall_result
    if description is not None:
        cur.description = description
    return cur


def _make_mock_conn(cursor=None):
    """Build a mock psycopg connection with a context-manager cursor."""
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    if cursor is not None:
        conn.cursor.return_value = cursor
    return conn


def test_add_context_event_valid_call_returns_french_ack(monkeypatch):
    """Valid call inserts a row and returns French-first ack text."""
    from core import main as main_module

    # Mock get_access_token to return anonymous
    monkeypatch.setattr(main_module, "get_access_token", lambda: None)

    mock_cursor = _make_mock_cursor()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)

    mock_conn = _make_mock_conn(cursor=mock_cursor)
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor.return_value = mock_cursor

    import core.db as db_module

    class FakeConnCtx:
        def __enter__(self):
            return mock_conn

        def __exit__(self, *args):
            return False

    with (
        patch.object(db_module, "get_connection", return_value=FakeConnCtx()),
        patch("core.audit.write_audit_row") as mock_audit,
    ):
        result = main_module.add_context_event(
            project_id="proj_test",
            event_date="2026-07-04",
            type="business",
            label="Lancement campagne ete",
            description="Details campagne",
        )

    # ToolResult has a content list with TextContent
    assert result is not None
    text = result.content[0].text
    assert "Lancement campagne ete" in text
    assert "2026-07-04" in text
    # French ack (accented chars are in the source)
    assert "ajout" in text.lower()
    # Audit was called
    mock_audit.assert_called_once()


def test_add_context_event_label_too_long(monkeypatch):
    """Label >120 chars raises ToolError (not a DB call)."""
    from core import main as main_module
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    monkeypatch.setattr(main_module, "get_access_token", lambda: None)

    long_label = "x" * 121

    with pytest.raises(ToolError):
        main_module.add_context_event(
            project_id="proj_test",
            event_date="2026-07-04",
            type="business",
            label=long_label,
        )


def test_add_context_event_invalid_date(monkeypatch):
    """Invalid date raises ToolError."""
    from core import main as main_module
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    monkeypatch.setattr(main_module, "get_access_token", lambda: None)

    with pytest.raises(ToolError):
        main_module.add_context_event(
            project_id="proj_test",
            event_date="invalid-date",
            type="business",
            label="Valid label",
        )


# ---------------------------------------------------------------------------
# _fetch_context_events tests
# ---------------------------------------------------------------------------


def test_fetch_context_events_returns_rows(tmp_path):
    """_fetch_context_events returns rows from the DuckDB mirror (Story 4.4 upgraded path).

    AC6: _fetch_context_events now reads from mirror.context_events (DuckDB) instead
    of Postgres directly (AD-12 single analytical read path).
    """
    from datetime import date

    import duckdb
    from core.main import _fetch_context_events

    db_path = str(tmp_path / "mirror_ctx.duckdb")
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
        ["evt_01", "proj_test", date(2026, 7, 4), "business", "Campagne ete"],
    )
    conn.close()

    with patch.dict("os.environ", {"TOOROW_DUCKDB_PATH": db_path}):
        result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")

    assert len(result) == 1
    assert result[0]["id"] == "evt_01"
    assert result[0]["project_id"] == "proj_test"
    assert result[0]["type"] == "business"
    assert result[0]["label"] == "Campagne ete"
    assert "event_date" in result[0]


def test_fetch_context_events_returns_empty_on_db_unavailable(monkeypatch):
    """_fetch_context_events returns [] gracefully when DuckDB path is not set."""
    from core.main import _fetch_context_events

    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    result = _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")

    assert result == []
