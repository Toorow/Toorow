"""Tests for submit_feedback MCP tool (Story 5.5, AC3, AC4, AC8).

Covers:
  - Valid thumbs-up (rating=1) writes feedback row + audit row.
  - Valid thumbs-down (rating=-1) writes feedback row.
  - Invalid rating (0, 2) raises ToolError with code="invalid_input".
  - Null trace_id writes row with trace_id=None; no Langfuse score attempt.
  - Langfuse score called when tracing enabled + trace_id present.
  - Langfuse unreachable does not fail the tool call.
  - TRACING_ENABLED=false: Langfuse client never instantiated.
  - Ack text is exactly "Thanks for your feedback." (English, no extra chars).
  - No structuredContent in tool result.
  - THE GRANT (2026-08-30): the rating is refused without a server-minted
    handle -- accepted, missing, another Result's, no longer live.

WHY THE CONNECTION FAKE GREW A GRANT TABLE. Every test here used to answer the
whole session with one `MagicMock`, which answers `app.result_app_grants` too --
so `load_grant`, `grant_is_live` and `touch_handle` would have been "green"
against a mock of themselves. The two grant statements are answered from real
rows instead, so the product's own SQL decides; every other statement of the
acquisition seam keeps the pre-existing `MagicMock` posture, which is not what
these tests are about.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Prevent background workers from starting during import.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: A handle is `rh_` + 26 Crockford base32 characters; `result_slices._HANDLE_RE`
#: refuses anything else before a row is loaded, so the fixtures spell real ones.
HANDLE = "rh_01234567890123456789ABCDEF"
OTHER_RESULT_HANDLE = "rh_01234567890123456789ABCDEG"
RESULT_ID = "res_EXAMPLE"


def _grant_row(handle_id: str, result_id: str, project_id: str) -> tuple:
    """The eleven columns `result_app_grants.load_grant` projects, in its order."""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return (
        handle_id,
        "org_EXAMPLE",
        project_id,
        result_id,
        "sha256:EXAMPLE",
        "anonymous",
        ["day", "clicks"],
        now,
        now,
        now + timedelta(hours=8),
        None,
    )


class _Cur:
    """Answers the two grant statements for real; delegates the rest to a mock.

    `description`, `fetchall` and every unknown statement keep the behaviour the
    file already had -- this fake narrows exactly one thing, the grant.
    """

    def __init__(self, conn):
        self.conn = conn
        self._fallback = conn.fallback_cursor
        self._known = False
        self._result = None
        self.rowcount = -1

    @property
    def description(self):
        return self._fallback.description

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split()).lower()
        self.conn.statements.append(text)
        self._known = True
        self._result = None
        if "update app.result_app_grants" in text:
            handle_id = params[1]
            live = handle_id in self.conn.grants and handle_id not in self.conn.revoked
            self.rowcount = 1 if live else 0
            if live:
                self.conn.touched.append(handle_id)
        elif "from app.result_app_grants" in text:
            grant = self.conn.grants.get(params[0])
            # `load_grant` scopes by project in its own WHERE clause.
            if grant is not None and grant[2] == params[1]:
                self._result = grant
        elif "insert into app.feedback" in text:
            self.conn.store.append(params)
        else:
            self._known = False
            self._fallback.execute(sql, params)

    def fetchone(self):
        return self._result if self._known else self._fallback.fetchone()

    def fetchall(self):
        return self._fallback.fetchall()

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Conn:
    def __init__(self, grants=None, revoked=()):
        self.grants: dict[str, tuple] = dict(grants or {})
        self.revoked: set[str] = set(revoked)
        self.touched: list[str] = []
        #: The parameter tuples of every `app.feedback` INSERT that ran.
        self.store: list[tuple] = []
        self.statements: list[str] = []
        self.commits: list[int] = []
        self.fallback_cursor = MagicMock()

    @property
    def committed_a_row(self) -> bool:
        return any(count > 0 for count in self.commits)

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits.append(len(self.store))

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


@pytest.fixture(autouse=True)
def _caller_may_view_the_project(monkeypatch):
    """The caller's access, granted -- so what these tests measure is the GRANT.

    `result_slices.resolve_handle` resolves the caller's Project access BEFORE it
    loads a grant (its stated order). Against the `MagicMock` connection this
    file uses, that resolution is meaningless noise, and leaving it unpatched
    would make every case below fail for a reason that is not the handle. The
    access decision itself is proved where it belongs:
    `tests/isolation/test_mcp_tool_scope_refusal.py`.
    """
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda *_a, **_k: SimpleNamespace(allowed=True, org_id="org_EXAMPLE", reason=None),
    )


def _make_mock_conn(project_id: str = "proj_test", **kwargs):
    """A connection carrying one live grant over RESULT_ID for *project_id*."""
    grants = {
        HANDLE: _grant_row(HANDLE, RESULT_ID, project_id),
        OTHER_RESULT_HANDLE: _grant_row(OTHER_RESULT_HANDLE, "res_OTHER", project_id),
    }
    grants.update(kwargs.pop("grants", {}))
    return _Conn(grants=grants, **kwargs)


# ---------------------------------------------------------------------------
# test_valid_thumbs_up
# ---------------------------------------------------------------------------


def test_valid_thumbs_up():
    """rating=1 writes feedback row and audit row; returns ack text."""
    from core.main import submit_feedback

    conn_mock = _make_mock_conn()

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
            # 2026-08-30: `report_ref` names the RESULT the grant was issued
            # over. It used to be free text ("get_daily_report:2026-07-11"),
            # which the server could neither check nor attribute.
            report_ref=RESULT_ID,
            # The parameter is `connector`, not `module`. Connector is the
            # canonical noun (docs/product-architecture/glossary.md; Module and
            # Extension were retired, Tool reserved for MCP) and
            # `server/core/feedback_mcp.py#submit_feedback` documents the rename in
            # its own docstring.
            connector="google-analytics",
            handle=HANDLE,
        )

    assert result.is_error is not True
    text = result.content[0].text
    assert text == "Thanks for your feedback."
    # Feedback row written -- read from the fake's own store rather than from
    # `call_args`, which since the grant landed names the last statement of the
    # transaction (the `touch_handle` UPDATE), not the INSERT.
    assert len(conn_mock.store) == 1
    assert RESULT_ID in conn_mock.store[0]
    assert conn_mock.touched == [HANDLE], "the grant is consumed with the append"
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
        result = submit_feedback(project_id="proj_test", rating=-1, handle=HANDLE)

    assert result.is_error is not True
    assert result.content[0].text == "Thanks for your feedback."


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

    conn_mock = _make_mock_conn()

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=True),
        patch("core.main.tracing.is_enabled", return_value=True),
    ):
        # Ensure Langfuse is NOT imported/called when trace_id is None
        with patch.dict("sys.modules", {"langfuse": None}):
            result = submit_feedback(project_id="proj_test", rating=1, trace_id=None, handle=HANDLE)

    assert result.content[0].text == "Thanks for your feedback."
    # The insert should have been called with None for trace_id
    insert_args = conn_mock.store[0]
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
            handle=HANDLE,
        )

    assert result.content[0].text == "Thanks for your feedback."
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
            handle=HANDLE,
        )

    # Tool must NOT raise — ack text returned despite Langfuse failure.
    assert result.content[0].text == "Thanks for your feedback."
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
            handle=HANDLE,
        )

    # Langfuse class never instantiated when tracing disabled
    mock_lf_class.assert_not_called()
    assert result.content[0].text == "Thanks for your feedback."


# ---------------------------------------------------------------------------
# test_ack_text_french
# ---------------------------------------------------------------------------


def test_ack_text_french():
    """Return text is exactly 'Thanks for your feedback.' (English, no extra chars)."""
    from core.main import submit_feedback

    conn_mock = _make_mock_conn()

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=False),
        patch("core.main.tracing.is_enabled", return_value=False),
    ):
        result = submit_feedback(project_id="proj_test", rating=1, handle=HANDLE)

    assert result.content[0].text == "Thanks for your feedback."


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
        result = submit_feedback(project_id="proj_test", rating=-1, handle=HANDLE)

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

    project_id = "proj_rate_limit_test"
    conn_mock = _make_mock_conn(project_id)

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
            result = submit_feedback(project_id=project_id, rating=1, handle=HANDLE)
            assert result.content[0].text == "Thanks for your feedback."

        # 61st call is rate-limited.
        with pytest.raises(ToolError) as exc_info:
            submit_feedback(project_id=project_id, rating=1, handle=HANDLE)

    err = json.loads(str(exc_info.value))
    assert err["code"] == "rate_limited"
    # AD-34 is ratified (SPEC.md:159, directive Jean 2026-07-24): all visible
    # application copy is English. The product copy was translated in `d3ea695d`
    # / `43e7c57c`; this assertion kept matching the French it replaced. The
    # PRODUCT is right and the test was wrong -- same shape as AI-103.
    assert "Try again" in err["message"]

    # Cleanup shared module state.
    main_mod._feedback_calls.pop(project_id, None)


def test_feedback_rate_limit_is_per_project():
    """A different project is not affected by another project's exhausted limit."""
    import core.main as main_mod
    from core.main import submit_feedback

    exhausted = "proj_exhausted"
    fresh = "proj_fresh"
    conn_mock = _make_mock_conn(fresh)
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
        result = submit_feedback(project_id=fresh, rating=1, handle=HANDLE)
        assert result.content[0].text == "Thanks for your feedback."

    main_mod._feedback_calls.pop(exhausted, None)
    main_mod._feedback_calls.pop(fresh, None)


def test_submit_feedback_registered_on_core_app():
    """submit_feedback must be registered on core mcp (no namespace prefix).

    The assembled catalog proves registration. The wire catalog proves the host can
    route the app-only tool by its exact name; model-facing projections are filtered
    separately by ``core.mcp_profiles.model_visible_tools``.
    """
    import asyncio

    from core.main import mcp

    tool_names = [t.name for t in asyncio.run(mcp._list_tools())]
    assert tool_names.count("submit_feedback") == 1, tool_names
    wire_names = [t.name for t in asyncio.run(mcp.list_tools())]
    assert wire_names.count("submit_feedback") == 1


# ---------------------------------------------------------------------------
# THE GRANT (2026-08-30) -- `mcp-tool-surface.md`, "Incomplete if" n. 8.
#
# `submit_feedback` is declared `insights` / `effect="read"` and appends a row to
# `app.feedback`. The declaration is defensible only while the append is
# impossible for a caller that merely knows the tool's name: an append-only
# OBSERVATION is a read BEHIND A HANDLE, and without one it is just a write in
# the always-visible profile. These four cases are that condition.
# ---------------------------------------------------------------------------


def test_a_rating_without_a_handle_appends_nothing():
    """The clause, stated: the discovery filter is not what stops a model.

    Refused AFTER the project scope guard, on purpose: `project_not_found` is
    the one envelope this tool owes a stranger (amendment of 2026-08-25), so a
    complaint about the caller's own payload must not be able to answer first.
    """
    from core.main import submit_feedback
    from fastmcp.exceptions import ToolError

    conn_mock = _make_mock_conn()

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row") as mock_audit,
        patch("core.tracing.is_enabled", return_value=False),
    ):
        with pytest.raises(ToolError) as exc_info:
            submit_feedback(project_id="proj_test", rating=1)

    err = json.loads(str(exc_info.value))
    assert err["code"] == "result_handle_required"
    assert "result_handle" in err["message"], "the refusal names the gesture"
    assert conn_mock.store == [], "nothing appended"
    assert conn_mock.touched == [], "and no grant was consumed"
    mock_audit.assert_not_called()


def test_a_handle_issued_over_another_result_cannot_rate_this_one():
    """The BINDING. A live grant of the same caller, for a different Result."""
    from core.main import submit_feedback
    from fastmcp.exceptions import ToolError

    conn_mock = _make_mock_conn()

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=False),
    ):
        with pytest.raises(ToolError) as exc_info:
            submit_feedback(
                project_id="proj_test",
                rating=1,
                report_ref=RESULT_ID,
                handle=OTHER_RESULT_HANDLE,
            )

    assert json.loads(str(exc_info.value))["code"] == "result_handle_names_another_result"
    assert conn_mock.store == []
    assert conn_mock.touched == []


def test_a_handle_the_server_never_issued_is_one_mute_envelope():
    """Absent, foreign, revoked and expired converge -- `resolve_handle` collapses
    them and this path only re-labels, so no refusal enumerates a Result."""
    from core.main import submit_feedback
    from fastmcp.exceptions import ToolError

    conn_mock = _make_mock_conn()

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row"),
        patch("core.tracing.is_enabled", return_value=False),
    ):
        with pytest.raises(ToolError) as exc_info:
            submit_feedback(
                project_id="proj_test",
                rating=1,
                handle="rh_ZZZZZZZZZZZZZZZZZZZZZZZZZZ",
            )

    assert json.loads(str(exc_info.value))["code"] == "result_handle_not_usable"
    assert conn_mock.store == []


def test_a_grant_that_died_between_resolve_and_consume_commits_nothing():
    """Consumption is a check. Revoked in another session, or idled out: the
    rating and the record of its grant land together or not at all."""
    from core.main import submit_feedback
    from fastmcp.exceptions import ToolError

    conn_mock = _make_mock_conn(revoked={HANDLE})

    with (
        patch("core.db.get_connection", return_value=conn_mock),
        patch("core.audit.write_audit_row") as mock_audit,
        patch("core.tracing.is_enabled", return_value=False),
    ):
        with pytest.raises(ToolError) as exc_info:
            submit_feedback(project_id="proj_test", rating=1, handle=HANDLE)

    assert json.loads(str(exc_info.value))["code"] == "result_handle_not_usable"
    assert conn_mock.committed_a_row is False
    mock_audit.assert_not_called()
