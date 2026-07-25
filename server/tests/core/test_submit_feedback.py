"""Tests for submit_feedback MCP tool (Story 5.5, AC3, AC4, AC8).

Covers:
  - Valid thumbs-up (rating=1) writes feedback row + audit row.
  - Valid thumbs-down (rating=-1) writes feedback row.
  - Invalid rating (0, 2) raises ToolError with code="invalid_input".
  - Null trace_id writes row with trace_id=None; no Langfuse score attempt.
  - Langfuse score called when tracing enabled + trace_id present.
  - Langfuse unreachable does not fail the tool call.
  - TRACING_ENABLED=false: Langfuse client never instantiated.
  - Ack text is exactly "Merci pour votre retour." (French, no extra chars).
  - No structuredContent in tool result.
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


def _make_mock_conn(cursor_mock=None):
    """Build a mock psycopg connection for use as context manager."""
    if cursor_mock is None:
        cursor_mock = MagicMock()
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)
    return conn_mock


# ---------------------------------------------------------------------------
# test_valid_thumbs_up
# ---------------------------------------------------------------------------


def test_valid_thumbs_up():
    """rating=1 writes feedback row and audit row; returns ack text."""
    from core.main import submit_feedback

    cursor_mock = MagicMock()
    conn_mock = _make_mock_conn(cursor_mock)

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row") as mock_audit,
        patch("core.tracing.is_enabled", return_value=False),
    ):
        result = submit_feedback(
            project_id="proj_test",
            rating=1,
            trace_id="abc123",
            comment="great report",
            report_ref="get_daily_report:2026-07-11",
            module="google-analytics",
        )

    assert result.is_error is not True
    text = result.content[0].text
    assert text == "Merci pour votre retour."
    # Feedback row written (the last execute is the INSERT; Story 7.1 adds a
    # preceding project-resolution SELECT via the shared resolver).
    insert_sql = cursor_mock.execute.call_args[0][0]
    assert "app.feedback" in insert_sql
    # Audit row written
    mock_audit.assert_called_once()
    audit_kwargs = mock_audit.call_args[1] if mock_audit.call_args[1] else mock_audit.call_args[0]
    # support both positional and keyword
    if isinstance(audit_kwargs, dict):
        assert audit_kwargs.get("action") == "feedback.submitted"
    else:
        # positional: (identity, action, provider_account, connection_ref, metadata)
        assert audit_kwargs[1] == "feedback.submitted"


# ---------------------------------------------------------------------------
# test_valid_thumbs_down
# ---------------------------------------------------------------------------


def test_valid_thumbs_down():
    """rating=-1 writes feedback row and returns ack text."""
    from core.main import submit_feedback

    conn_mock = _make_mock_conn()

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=False),
    ):
        result = submit_feedback(project_id="proj_test", rating=-1)

    assert result.is_error is not True
    assert result.content[0].text == "Merci pour votre retour."


# ---------------------------------------------------------------------------
# test_invalid_rating (rating=0)
# ---------------------------------------------------------------------------


def test_invalid_rating():
    """rating=0 raises ToolError with code='invalid_input'."""
    from core.main import submit_feedback
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc_info:
        submit_feedback(project_id="proj_test", rating=0)

    err = json.loads(str(exc_info.value))
    assert err["code"] == "invalid_input"
    assert "rating" in err["message"]


# ---------------------------------------------------------------------------
# test_invalid_rating_plus_2 (rating=2)
# ---------------------------------------------------------------------------


def test_invalid_rating_plus_2():
    """rating=2 raises ToolError with code='invalid_input'."""
    from core.main import submit_feedback
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc_info:
        submit_feedback(project_id="proj_test", rating=2)

    err = json.loads(str(exc_info.value))
    assert err["code"] == "invalid_input"


# ---------------------------------------------------------------------------
# test_feedback_with_null_trace_id
# ---------------------------------------------------------------------------


def test_feedback_with_null_trace_id():
    """trace_id=None writes row with NULL trace_id; Langfuse not called."""
    from core.main import submit_feedback

    cursor_mock = MagicMock()
    conn_mock = _make_mock_conn(cursor_mock)

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=True),
        patch("core.main.tracing.is_enabled", return_value=True),
    ):
        # Ensure Langfuse is NOT imported/called when trace_id is None
        with patch.dict("sys.modules", {"langfuse": None}):
            result = submit_feedback(project_id="proj_test", rating=1, trace_id=None)

    assert result.content[0].text == "Merci pour votre retour."
    # The insert should have been called with None for trace_id
    insert_args = cursor_mock.execute.call_args[0][1]
    # index 2 is trace_id
    assert insert_args[2] is None


# ---------------------------------------------------------------------------
# test_langfuse_score_called_when_enabled
# ---------------------------------------------------------------------------


def test_langfuse_score_called_when_enabled():
    """When tracing enabled + trace_id present, Langfuse.score called with value=1.0."""
    from core.main import submit_feedback

    conn_mock = _make_mock_conn()
    mock_lf_instance = MagicMock()
    mock_lf_class = MagicMock(return_value=mock_lf_instance)

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=True),
        patch("core.main.tracing.is_enabled", return_value=True),
        patch.dict("sys.modules", {"langfuse": MagicMock(Langfuse=mock_lf_class)}),
    ):
        result = submit_feedback(
            project_id="proj_test",
            rating=1,
            trace_id="abc123def456abc123def456abc12345",
        )

    assert result.content[0].text == "Merci pour votre retour."
    mock_lf_instance.score.assert_called_once()
    call_args = mock_lf_instance.score.call_args
    score_kwargs = call_args[1] if call_args[1] else {}
    # value should be float 1.0
    if "value" in score_kwargs:
        assert score_kwargs["value"] == 1.0
    mock_lf_instance.flush.assert_called_once()


# ---------------------------------------------------------------------------
# test_langfuse_unreachable_does_not_fail_tool
# ---------------------------------------------------------------------------


def test_langfuse_unreachable_does_not_fail_tool():
    """Langfuse raising exception: tool still returns ack text."""
    from core.main import submit_feedback

    conn_mock = _make_mock_conn()
    mock_lf_instance = MagicMock()
    mock_lf_instance.score.side_effect = ConnectionError("Langfuse unreachable")
    mock_lf_class = MagicMock(return_value=mock_lf_instance)

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=True),
        patch("core.main.tracing.is_enabled", return_value=True),
        patch.dict("sys.modules", {"langfuse": MagicMock(Langfuse=mock_lf_class)}),
    ):
        result = submit_feedback(
            project_id="proj_test",
            rating=-1,
            trace_id="abc123def456abc123def456abc12345",
        )

    # Tool must NOT raise — ack text returned despite Langfuse failure.
    assert result.content[0].text == "Merci pour votre retour."
    assert result.is_error is not True


# ---------------------------------------------------------------------------
# test_feedback_disabled_tracing_no_langfuse
# ---------------------------------------------------------------------------


def test_feedback_disabled_tracing_no_langfuse():
    """TRACING_ENABLED=false: Langfuse client never instantiated."""
    from core.main import submit_feedback

    conn_mock = _make_mock_conn()
    mock_lf_class = MagicMock()

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=False),
        patch("core.main.tracing.is_enabled", return_value=False),
        patch.dict("sys.modules", {"langfuse": MagicMock(Langfuse=mock_lf_class)}),
    ):
        result = submit_feedback(
            project_id="proj_test",
            rating=1,
            trace_id="abc123def456abc123def456abc12345",
        )

    # Langfuse class never instantiated when tracing disabled
    mock_lf_class.assert_not_called()
    assert result.content[0].text == "Merci pour votre retour."


# ---------------------------------------------------------------------------
# test_ack_text_french
# ---------------------------------------------------------------------------


def test_ack_text_french():
    """Return text is exactly 'Merci pour votre retour.' (French, no extra chars)."""
    from core.main import submit_feedback

    conn_mock = _make_mock_conn()

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=False),
        patch("core.main.tracing.is_enabled", return_value=False),
    ):
        result = submit_feedback(project_id="proj_test", rating=1)

    assert result.content[0].text == "Merci pour votre retour."


# ---------------------------------------------------------------------------
# test_nothing_in_structuredContent
# ---------------------------------------------------------------------------


def test_nothing_in_structuredContent():
    """Tool result has no structuredContent — write-ack only (AD-1)."""
    from core.main import submit_feedback

    conn_mock = _make_mock_conn()

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=False),
        patch("core.main.tracing.is_enabled", return_value=False),
    ):
        result = submit_feedback(project_id="proj_test", rating=-1)

    # structured_content should be absent (None or not set)
    sc = getattr(result, "structured_content", None)
    assert sc is None


# ---------------------------------------------------------------------------
# test_submit_feedback_registered_on_core_app
# ---------------------------------------------------------------------------


def test_feedback_rate_limited():
    """AI-36 (AC13): the 61st call for a project within the window is rate-limited."""
    import core.main as main_mod
    from core.main import submit_feedback
    from fastmcp.exceptions import ToolError

    conn_mock = _make_mock_conn()
    project_id = "proj_rate_limit_test"

    # Reset the in-memory limiter state for a clean window; default limit is 60/h.
    main_mod._feedback_calls.pop(project_id, None)

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=False),
        patch("core.main.tracing.is_enabled", return_value=False),
    ):
        # 60 calls succeed.
        for _ in range(60):
            result = submit_feedback(project_id=project_id, rating=1)
            assert result.content[0].text == "Merci pour votre retour."

        # 61st call is rate-limited.
        with pytest.raises(ToolError) as exc_info:
            submit_feedback(project_id=project_id, rating=1)

    err = json.loads(str(exc_info.value))
    assert err["code"] == "rate_limited"
    assert "Réessayez" in err["message"]

    # Cleanup shared module state.
    main_mod._feedback_calls.pop(project_id, None)


def test_feedback_rate_limit_is_per_project():
    """A different project is not affected by another project's exhausted limit."""
    import core.main as main_mod
    from core.main import submit_feedback

    conn_mock = _make_mock_conn()
    exhausted = "proj_exhausted"
    fresh = "proj_fresh"
    import time as _time

    # Pre-fill the exhausted project's window to the limit.
    main_mod._feedback_calls[exhausted] = [_time.monotonic()] * 60
    main_mod._feedback_calls.pop(fresh, None)

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=False),
        patch("core.main.tracing.is_enabled", return_value=False),
    ):
        # Fresh project still works.
        result = submit_feedback(project_id=fresh, rating=1)
        assert result.content[0].text == "Merci pour votre retour."

    main_mod._feedback_calls.pop(exhausted, None)
    main_mod._feedback_calls.pop(fresh, None)


def test_submit_feedback_registered_on_core_app():
    """submit_feedback must be registered on core mcp (no namespace prefix)."""
    import asyncio

    from core.main import mcp

    tool_names = [t.name for t in asyncio.run(mcp.list_tools())]
    assert "submit_feedback" in tool_names, (
        f"submit_feedback not found in core tools: {tool_names}"
    )
