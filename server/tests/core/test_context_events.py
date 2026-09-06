"""Tests for context_events: add_context_event MCP tool and _fetch_context_events helper.

Story 4.3, AC8 (T9.1).

Covers:
  - add_context_event: valid call inserts row + returns French ack text.
  - add_context_event: label >120 chars raises ToolError.
  - add_context_event: invalid date raises ToolError.
  - _fetch_context_events: returns filtered rows from DB.
  - _fetch_context_events: raises ContextEventsUnavailable when no store can serve
    (AI-344) -- never [].
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


def test_add_context_event_acknowledges_by_naming_what_it_wrote(monkeypatch):
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
    # AI-103. This used to assert `"ajout" in text.lower()` under a test named
    # `..._returns_french_ack`, and it went red on main when AD-34 made every
    # visible string English. The product was right and the test was wrong -- but
    # replacing "ajout" with "added" would be the same mistake with a new word:
    # the language belongs to AD-34 and will change again.
    #
    # What the acknowledgement must actually guarantee is that it NAMES what was
    # written -- the label and the date, asserted above -- and that it confirms a
    # write rather than echoing the request. That is asserted on the durable
    # side: the audit row.
    assert text.strip(), "the tool answered with an empty acknowledgement"
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


def test_fetch_context_events_says_unavailable_when_no_store_can_serve(monkeypatch):
    """No mirror and no caller to reach the record for: `ContextEventsUnavailable`,
    never `[]` (AI-344, 2026-09-01). An empty list now means a store was read."""
    from core.context_events import ContextEventsUnavailable
    from core.main import _fetch_context_events

    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    with pytest.raises(ContextEventsUnavailable) as excinfo:
        _fetch_context_events("proj_test", "2026-07-01", "2026-07-31")
    assert excinfo.value.payload["repair"]


# ---------------------------------------------------------------------------
# Migration 322 -- the MCP door names the metric, and says which scope it wrote
# ---------------------------------------------------------------------------


def _mcp_write_context(monkeypatch):
    """The mocked write path `add_context_event` runs on, in one place."""
    from core import main as main_module

    monkeypatch.setattr(main_module, "get_access_token", lambda: None)

    cursor = _make_mock_cursor()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    conn = _make_mock_conn(cursor=cursor)
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cursor

    class FakeConnCtx:
        def __enter__(self):
            return conn

        def __exit__(self, *args):
            return False

    return main_module, cursor, FakeConnCtx


def test_the_mcp_door_writes_the_metric_and_says_which_scope_it_wrote(monkeypatch):
    """Naming a metric NARROWS where the event is ever read as a cause.

    So the acknowledgement says which of the two scopes was written: a caller
    that meant "every metric" and got one learns it here, not from a "Why"
    section that stays silent for a week.
    """
    main_module, cursor, ctx = _mcp_write_context(monkeypatch)

    import core.db as db_module

    with (
        patch.object(db_module, "get_connection", return_value=ctx()),
        patch.object(db_module, "request_connection", return_value=ctx()),
        patch("core.audit.write_audit_row"),
        patch("core.context_events.assert_metric_is_governed", return_value=None),
    ):
        result = main_module.add_context_event(
            project_id="proj_test",
            event_date="2026-07-04",
            type="business",
            label="Bid raised",
            metric="cost",
        )

    insert_sql, insert_params = cursor.execute.call_args_list[-1].args
    assert "metric" in insert_sql
    assert insert_params[-1] == "cost"
    assert "cost" in result.content[0].text


def test_the_mcp_door_says_so_when_the_event_is_about_every_metric(monkeypatch):
    main_module, _cursor, ctx = _mcp_write_context(monkeypatch)

    import core.db as db_module

    with (
        patch.object(db_module, "get_connection", return_value=ctx()),
        patch.object(db_module, "request_connection", return_value=ctx()),
        patch("core.audit.write_audit_row"),
    ):
        result = main_module.add_context_event(
            project_id="proj_test",
            event_date="2026-07-04",
            type="incident",
            label="Site outage",
        )

    assert "every metric" in result.content[0].text


def test_the_mcp_door_refuses_an_ungoverned_metric_with_the_gesture(monkeypatch):
    """This door speaks ToolError; the refusal keeps its code and its sentence."""
    import json as _json

    from core.context_events import ContextEventRefusal
    from fastmcp.exceptions import ToolError

    main_module, _cursor, ctx = _mcp_write_context(monkeypatch)
    refusal = ContextEventRefusal(
        "metric_not_governed",
        "'Ad spend' is not a metric this Project governs. Pick one of its "
        "governed metrics, leave the metric empty when the event concerns every "
        "metric, or declare it as a Concept in the Semantic Model.",
        status=422,
    )

    import core.db as db_module

    with (
        patch.object(db_module, "get_connection", return_value=ctx()),
        patch.object(db_module, "request_connection", return_value=ctx()),
        patch("core.audit.write_audit_row"),
        patch("core.context_events.assert_metric_is_governed", side_effect=refusal),
        pytest.raises(ToolError) as raised,
    ):
        main_module.add_context_event(
            project_id="proj_test",
            event_date="2026-07-04",
            type="business",
            label="Bid raised",
            metric="Ad spend",
        )

    payload = _json.loads(str(raised.value))
    assert payload["code"] == "metric_not_governed"
    assert "Semantic Model" in payload["message"]


# ---------------------------------------------------------------------------
# The symmetric doors: correct_context_event / withdraw_context_event
#
# Migration 328, Jean's decision of 2026-08-31. Until they existed, an annotation
# dictated in chat could be WRITTEN here and repaired only in a console the
# caller may not have: the surface that accepted the wrong date was not the
# surface that could correct it.
# ---------------------------------------------------------------------------


class _ArmedConnection:
    def __enter__(self):
        return MagicMock()

    def __exit__(self, *args):
        return False


def _armed_doors(monkeypatch):
    """Both gates the three annotation doors share, answered as allowed."""
    from core import main as main_module

    monkeypatch.setattr(main_module, "get_access_token", lambda: None)
    monkeypatch.setattr(
        main_module, "_resolve_project", lambda project_id, _identity: project_id
    )
    monkeypatch.setattr(
        main_module, "_refuse_unless_project_scope", lambda *a, **k: None
    )


def test_the_mcp_door_corrects_the_annotation_and_names_what_it_now_says(monkeypatch):
    from core import context_events as events_module
    from core import context_hub_mcp
    from core import db as db_module

    _armed_doors(monkeypatch)
    seen: dict = {}

    def _update(**kwargs):
        seen.update(kwargs)
        return {
            "label": "Price increased on the main plan",
            "event_date": "2026-08-14",
            "metric": "cost",
        }

    with (
        patch.object(db_module, "request_connection", return_value=_ArmedConnection()),
        patch.object(events_module, "update_manual_event", _update),
    ):
        result = context_hub_mcp.correct_context_event(
            project_id="proj_test",
            event_id="evt_one",
            label="Price increased on the main plan",
        )

    assert seen["event_id"] == "evt_one"
    assert seen["label"] == "Price increased on the main plan"
    # An omitted parameter is NOT a correction: only what the caller named
    # travels, or a correction of the label would blank the description.
    assert "description" not in seen
    text = result.content[0].text
    assert "Price increased on the main plan" in text
    assert "cost" in text


def test_the_mcp_door_clears_a_metric_with_an_explicit_empty_string(monkeypatch):
    """The one shape a JSON tool call can tell apart from an omission."""
    from core import context_events as events_module
    from core import context_hub_mcp
    from core import db as db_module

    _armed_doors(monkeypatch)
    seen: dict = {}

    def _update(**kwargs):
        seen.update(kwargs)
        return {"label": "Outage", "event_date": "2026-08-14", "metric": None}

    with (
        patch.object(db_module, "request_connection", return_value=_ArmedConnection()),
        patch.object(events_module, "update_manual_event", _update),
    ):
        result = context_hub_mcp.correct_context_event(
            project_id="proj_test", event_id="evt_one", metric=""
        )

    assert seen["metric"] is None
    assert "every metric" in result.content[0].text


def test_the_mcp_door_refuses_a_correction_that_names_nothing(monkeypatch):
    from core import context_hub_mcp
    from fastmcp.exceptions import ToolError

    _armed_doors(monkeypatch)
    with pytest.raises(ToolError) as refused:
        context_hub_mcp.correct_context_event(project_id="proj_test", event_id="evt_one")
    assert "no_change" in str(refused.value)


def test_the_mcp_door_carries_the_service_refusal_with_its_gesture(monkeypatch):
    """One refusal, two doors: the message names the gesture, not the cause."""
    from core import context_events as events_module
    from core import context_hub_mcp
    from core import db as db_module
    from fastmcp.exceptions import ToolError

    _armed_doors(monkeypatch)

    def _refuse(**_kwargs):
        raise events_module.ContextEventRefusal(
            "connector_owned",
            "This event was emitted by the youtube connector. Stop it at the source.",
        )

    with (
        patch.object(db_module, "request_connection", return_value=_ArmedConnection()),
        patch.object(events_module, "retire_manual_event", _refuse),
        pytest.raises(ToolError) as refused,
    ):
        context_hub_mcp.withdraw_context_event(
            project_id="proj_test", event_id="evt_one", reason="wrong"
        )

    assert "Stop it at the source" in str(refused.value)


def test_the_mcp_withdrawal_says_the_row_stays(monkeypatch):
    from core import context_events as events_module
    from core import context_hub_mcp
    from core import db as db_module

    _armed_doors(monkeypatch)
    seen: dict = {}

    def _retire(**kwargs):
        seen.update(kwargs)
        return {"label": "Wrong entry", "event_date": "2026-08-14"}

    with (
        patch.object(db_module, "request_connection", return_value=_ArmedConnection()),
        patch.object(events_module, "retire_manual_event", _retire),
    ):
        result = context_hub_mcp.withdraw_context_event(
            project_id="proj_test", event_id="evt_one", reason="the date was wrong"
        )

    assert seen["reason"] == "the date was wrong"
    text = result.content[0].text
    # A withdrawal is a supersede: the acknowledgement says so, because a caller
    # who reads "withdrawn" and assumes "deleted" will look for it in the wrong
    # place forever.
    assert "no longer read as a cause" in text
    assert "The row stays" in text


def test_the_three_annotation_doors_are_declared_alike(monkeypatch):
    """A withdrawal is not a lesser act than a creation."""
    from core import context_hub_mcp
    from core.mcp_profiles import _REGISTRY, registered_declarations

    class _FakeMcp:
        def tool(self, *_args, **_kwargs):
            def _decorate(handler):
                return handler

            return _decorate

    # The registry is process-wide: emptying it would leave every later test in
    # this session reading a catalog nobody registered. Snapshot, then restore.
    before = dict(_REGISTRY.declarations)
    try:
        context_hub_mcp.register(_FakeMcp())
        declared = {
            d.name: d
            for d in registered_declarations()
            if d.name.endswith("_context_event")
        }
    finally:
        _REGISTRY.declarations.clear()
        _REGISTRY.declarations.update(before)

    assert set(declared) == {
        "add_context_event",
        "correct_context_event",
        "withdraw_context_event",
    }, sorted(declared)
    profiles = {(d.profile, d.effect, d.confirmation_mode) for d in declared.values()}
    assert profiles == {("governance", "confirmed_write", "human")}
