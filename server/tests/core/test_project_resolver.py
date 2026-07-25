"""Tests for core.project_resolver.resolve_project_id (Story 7.1, AC5, AC8).

Covers the three AC8 cases:
  - explicit real project_id -> returned unchanged (validated active).
  - absent / 'default' -> falls back to the seeded slug='default' row.
  - archived project -> raises ToolError code=project_not_found.

The resolver takes an open psycopg connection; here we pass a MagicMock cursor
so the SQL branch selection (fallback vs explicit) is what is under test, not the
DB itself. Live-DB constraint behaviour is covered in the integration suite.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from core.project_resolver import PROJECT_NOT_FOUND_CODE, resolve_project_id


def _mock_conn(fetchone_result):
    """Build a MagicMock connection whose cursor.fetchone() returns the given value."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetchone_result
    # conn.cursor() is used as a context manager: `with conn.cursor() as cur:`
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_resolve_explicit_project_id():
    """An explicit, active project_id is returned unchanged."""
    conn, cur = _mock_conn(("active",))  # SELECT status ... -> active
    result = resolve_project_id("proj_abc", conn)
    assert result == "proj_abc"
    # The explicit path queries by id, not by slug.
    sql = cur.execute.call_args[0][0]
    assert "WHERE id = %s" in sql


def test_resolve_default_falls_back_to_slug():
    """No project_id -> resolves the seeded slug='default' row's id."""
    conn, cur = _mock_conn(("default",))  # SELECT id WHERE slug='default' -> 'default'
    result = resolve_project_id(None, conn)
    assert result == "default"
    sql = cur.execute.call_args[0][0]
    assert "slug = %s" in sql


def test_resolve_literal_default_falls_back_to_slug():
    """The legacy sentinel 'default' also takes the fallback (slug) path."""
    conn, cur = _mock_conn(("default",))
    result = resolve_project_id("default", conn)
    assert result == "default"
    sql = cur.execute.call_args[0][0]
    assert "slug = %s" in sql


def test_resolve_archived_project_raises_tool_error():
    """An archived explicit project raises ToolError code=project_not_found."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    conn, _ = _mock_conn(("archived",))  # status = archived
    with pytest.raises(ToolError) as exc:
        resolve_project_id("proj_archived", conn)
    assert PROJECT_NOT_FOUND_CODE in str(exc.value)


def test_resolve_missing_explicit_project_raises_tool_error():
    """An explicit id with no matching row raises ToolError."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    conn, _ = _mock_conn(None)  # no row
    with pytest.raises(ToolError) as exc:
        resolve_project_id("proj_gone", conn)
    assert PROJECT_NOT_FOUND_CODE in str(exc.value)


def test_resolve_missing_default_seed_raises_tool_error():
    """If the seeded default row is absent, the fallback path raises."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    conn, _ = _mock_conn(None)
    with pytest.raises(ToolError) as exc:
        resolve_project_id(None, conn)
    assert PROJECT_NOT_FOUND_CODE in str(exc.value)
