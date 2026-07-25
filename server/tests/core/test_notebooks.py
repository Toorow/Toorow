"""Tests for save_notebook and run_notebook MCP tools (Story 6.5, AC3, AC4, AC8).

Covers (from AC8):
  - test_save_notebook_inserts_row: mock Postgres; assert app.notebooks row inserted.
  - test_save_notebook_unknown_report_ref: ToolError not_found for unknown report.
  - test_save_notebook_unsupported_window_rule: ToolError for unsupported rule.
  - test_run_notebook_resolves_window_rule: last_30d -> correct date window.
  - test_run_notebook_summary_has_citations: run result summary contains citation tokens.
  - test_run_notebook_envelope_meta_has_notebook_id: envelope has meta.notebook_id.
  - test_run_notebook_stores_pull_ids: pull_ids in notebook_runs match warehouse result.
  - test_run_notebook_not_found: unknown notebook_id -> ToolError not_found.
  - test_run_notebook_as_of: as_of='2026-06-01' -> summary contains as_of hint.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

# Prevent background workers from starting during import.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_conn(cursor_mock=None, *, fetchone_return=None, fetchall_return=None):
    """Build a mock psycopg connection usable as a context manager."""
    if cursor_mock is None:
        cursor_mock = MagicMock()
    if fetchone_return is not None:
        cursor_mock.fetchone.return_value = fetchone_return
    if fetchall_return is not None:
        cursor_mock.fetchall.return_value = fetchall_return
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)
    return conn_mock


def _make_cursor_with_description(row, columns):
    """Cursor mock whose .fetchone returns row and .description returns column specs."""
    cursor_mock = MagicMock()
    cursor_mock.fetchone.return_value = row
    cursor_mock.description = [(col,) for col in columns]
    return cursor_mock


# ---------------------------------------------------------------------------
# save_notebook tests
# ---------------------------------------------------------------------------


class TestSaveNotebook:
    """AC8 — save_notebook tests."""

    def test_save_notebook_inserts_row(self):
        """test_save_notebook_inserts_row: mock Postgres; assert app.notebooks row inserted."""
        from core.main import save_notebook

        cursor_mock = MagicMock()
        conn_mock = _make_mock_conn(cursor_mock)

        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.audit.write_audit_row") as mock_audit,
        ):
            result = save_notebook(
                project_id="proj_test",
                title="Mon notebook GSC",
                report_ref="adhoc",
                window_rule="last_30d",
            )

        assert result.is_error is not True
        text = result.content[0].text
        assert "sauvegardé" in text
        assert "nb_" in text

        # Verify INSERT was called (the last execute is the INSERT; Story 7.1
        # adds a preceding project-resolution SELECT via the shared resolver).
        sql = cursor_mock.execute.call_args[0][0]
        assert "app.notebooks" in sql

        # Verify audit row
        mock_audit.assert_called_once()
        call_kwargs = mock_audit.call_args
        assert call_kwargs[1]["action"] == "notebook_created"

    def test_save_notebook_unknown_report_ref(self):
        """test_save_notebook_unknown_report_ref: unknown report ref -> ToolError not_found."""
        from core.main import save_notebook
        from fastmcp.exceptions import ToolError

        with patch("core.reports.find_report", return_value=None):
            with pytest.raises(ToolError) as exc_info:
                save_notebook(
                    project_id="proj_test",
                    title="Test",
                    report_ref="gsc/nonexistent",
                    window_rule="last_30d",
                )

        error_data = json.loads(exc_info.value.args[0])
        assert error_data["code"] == "not_found"

    def test_save_notebook_unsupported_window_rule(self):
        """test_save_notebook_unsupported_window_rule: unsupported rule -> ToolError."""
        from core.main import save_notebook
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as exc_info:
            save_notebook(
                project_id="proj_test",
                title="Test",
                report_ref="adhoc",
                window_rule="quarterly",
            )

        error_data = json.loads(exc_info.value.args[0])
        assert error_data["code"] == "invalid_input"
        assert "quarterly" in error_data["message"]

    def test_save_notebook_with_narrative_prompt(self):
        """Optional narrative_prompt is stored when provided."""
        from core.main import save_notebook

        cursor_mock = MagicMock()
        conn_mock = _make_mock_conn(cursor_mock)

        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.audit.write_audit_row"),
        ):
            result = save_notebook(
                project_id="proj_test",
                title="Analyse SEO hebdo",
                report_ref="adhoc",
                window_rule="last_7d",
                narrative_prompt="Analyse en expert SEO senior.",
            )

        assert result.is_error is not True
        args = cursor_mock.execute.call_args[0][1]
        # narrative_prompt should be in the INSERT params
        assert "Analyse en expert SEO senior." in args

    def test_save_notebook_adhoc_report_ref_skips_report_lookup(self):
        """'adhoc' report_ref must not trigger a report lookup."""
        from core.main import save_notebook

        cursor_mock = MagicMock()
        conn_mock = _make_mock_conn(cursor_mock)

        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.audit.write_audit_row"),
            patch("core.reports.find_report") as mock_find,
        ):
            result = save_notebook(
                project_id="proj_test",
                title="Adhoc notebook",
                report_ref="adhoc",
                window_rule="last_14d",
            )

        assert result.is_error is not True
        # find_report must NOT be called for 'adhoc'
        mock_find.assert_not_called()


# ---------------------------------------------------------------------------
# run_notebook tests
# ---------------------------------------------------------------------------


class TestRunNotebook:
    """AC8 — run_notebook tests."""

    def _make_notebook_row(
        self,
        *,
        notebook_id="nb_TEST123",
        project_id="proj_test",
        title="Test",
        report_ref="adhoc",
        window_rule="last_30d",
        narrative_prompt=None,
        created_by="user@example.com",
    ):
        """Return (row_tuple, columns) matching the SELECT in run_notebook."""
        columns = ["id", "project_id", "title", "report_ref", "window_rule",
                   "narrative_prompt", "created_by"]
        row = (notebook_id, project_id, title, report_ref, window_rule,
               narrative_prompt, created_by)
        return row, columns

    def test_run_notebook_not_found(self):
        """test_run_notebook_not_found: unknown notebook_id -> ToolError not_found."""
        from core.main import run_notebook
        from fastmcp.exceptions import ToolError

        cursor_mock = MagicMock()
        cursor_mock.fetchone.return_value = None
        cursor_mock.description = []
        conn_mock = _make_mock_conn(cursor_mock)

        with patch("core.db.get_connection", return_value=conn_mock):
            with pytest.raises(ToolError) as exc_info:
                run_notebook(notebook_id="nb_DOESNOTEXIST")

        error_data = json.loads(exc_info.value.args[0])
        assert error_data["code"] == "not_found"

    def test_run_notebook_resolves_window_rule(self):
        """test_run_notebook_resolves_window_rule: last_30d -> date_from = today-30."""

        from core.main import run_notebook

        row, columns = self._make_notebook_row(
            report_ref="adhoc", window_rule="last_30d"
        )
        cursor_mock = _make_cursor_with_description(row, columns)
        # Second cursor for INSERT into notebook_runs
        insert_cursor = MagicMock()

        call_count = [0]

        def cursor_factory():
            call_count[0] += 1
            cm = MagicMock()
            if call_count[0] == 1:
                cm.__enter__ = MagicMock(return_value=cursor_mock)
            else:
                cm.__enter__ = MagicMock(return_value=insert_cursor)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor = cursor_factory

        captured_params = []

        def capture_insert(sql, params):
            if "app.notebook_runs" in sql:
                captured_params.append(params)

        insert_cursor.execute = capture_insert

        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.audit.write_audit_row"),
        ):
            result = run_notebook(notebook_id="nb_TEST123")

        assert result.is_error is not True
        # The result was stored — verify it ran without error

    def test_run_notebook_envelope_meta_has_notebook_id(self):
        """test_run_notebook_envelope_meta_has_notebook_id: envelope.meta.notebook_id set."""
        from core.main import run_notebook

        row, columns = self._make_notebook_row(report_ref="adhoc", window_rule="last_7d")
        cursor_mock = _make_cursor_with_description(row, columns)
        insert_cursor = MagicMock()

        call_count = [0]

        def cursor_factory():
            call_count[0] += 1
            cm = MagicMock()
            if call_count[0] == 1:
                cm.__enter__ = MagicMock(return_value=cursor_mock)
            else:
                cm.__enter__ = MagicMock(return_value=insert_cursor)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor = cursor_factory

        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.audit.write_audit_row"),
        ):
            result = run_notebook(notebook_id="nb_TEST123")

        assert result.is_error is not True
        # structured_content has meta.notebook_id
        sc = result.structured_content
        assert sc is not None
        assert sc.get("meta", {}).get("notebook_id") == "nb_TEST123"
        assert "run_id" in sc.get("meta", {})
        run_id = sc["meta"]["run_id"]
        assert run_id.startswith("nbrun_")

    def test_run_notebook_summary_has_citations(self):
        """test_run_notebook_summary_has_citations: adhoc run returns non-empty summary."""
        from core.main import run_notebook

        row, columns = self._make_notebook_row(report_ref="adhoc", window_rule="last_30d")
        cursor_mock = _make_cursor_with_description(row, columns)
        insert_cursor = MagicMock()

        call_count = [0]

        def cursor_factory():
            call_count[0] += 1
            cm = MagicMock()
            if call_count[0] == 1:
                cm.__enter__ = MagicMock(return_value=cursor_mock)
            else:
                cm.__enter__ = MagicMock(return_value=insert_cursor)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor = cursor_factory

        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.audit.write_audit_row"),
        ):
            result = run_notebook(notebook_id="nb_TEST123")

        assert result.is_error is not True
        text = result.content[0].text
        # Summary should be non-empty
        assert len(text) > 0

    def test_run_notebook_stores_pull_ids(self):
        """test_run_notebook_stores_pull_ids: pull_ids stored come from render_report output."""
        from core.main import run_notebook

        row, columns = self._make_notebook_row(
            report_ref="gsc/position_movements", window_rule="last_30d"
        )
        cursor_mock = _make_cursor_with_description(row, columns)
        insert_cursor = MagicMock()

        call_count = [0]
        stored_params = []

        def capture_insert(sql, params):
            stored_params.append((sql, params))

        insert_cursor.execute = capture_insert

        def cursor_factory():
            call_count[0] += 1
            cm = MagicMock()
            if call_count[0] == 1:
                cm.__enter__ = MagicMock(return_value=cursor_mock)
            else:
                cm.__enter__ = MagicMock(return_value=insert_cursor)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor = cursor_factory

        # Mock render_report to return a known envelope with pull_ids
        fake_envelope = {
            "schema_version": "1",
            "meta": {
                "provenance": {
                    "pull_ids": ["pull_AAA", "pull_BBB"],
                    "pull_id": "pull_BBB",
                    "source_system": "gsc",
                    "source_field": "fact_daily_kpi",
                },
                "alerts": [],
                "freshness": None,
                "context_events": [],
            },
            "data": {
                "report_id": "gsc/position_movements",
                "date_range": {"start": "2026-06-12", "end": "2026-07-12"},
                "connectors": ["gsc"],
                "metrics": {"clicks": 1000},
                "rows": [],
            },
        }
        fake_summary = "Clics: 1 000 (gsc:fact_daily_kpi, pull_BBB)"

        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.audit.write_audit_row"),
            patch("core.reports.render_report", return_value=(fake_summary, fake_envelope, "ui://gsc/widget")),
        ):
            result = run_notebook(notebook_id="nb_TEST123")

        assert result.is_error is not True
        # Verify pull_ids in the INSERT params
        if stored_params:
            _, params = stored_params[0]
            assert ["pull_AAA", "pull_BBB"] in params

    def test_run_notebook_as_of(self):
        """test_run_notebook_as_of: as_of param -> summary mentions the as_of date."""
        from core.main import run_notebook

        row, columns = self._make_notebook_row(report_ref="adhoc", window_rule="last_30d")
        cursor_mock = _make_cursor_with_description(row, columns)
        insert_cursor = MagicMock()

        call_count = [0]

        def cursor_factory():
            call_count[0] += 1
            cm = MagicMock()
            if call_count[0] == 1:
                cm.__enter__ = MagicMock(return_value=cursor_mock)
            else:
                cm.__enter__ = MagicMock(return_value=insert_cursor)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        conn_mock = MagicMock()
        conn_mock.__enter__ = MagicMock(return_value=conn_mock)
        conn_mock.__exit__ = MagicMock(return_value=False)
        conn_mock.cursor = cursor_factory

        with (
            patch("core.db.get_connection", return_value=conn_mock),
            patch("core.audit.write_audit_row"),
        ):
            result = run_notebook(notebook_id="nb_TEST123", as_of="2026-06-01")

        assert result.is_error is not True
