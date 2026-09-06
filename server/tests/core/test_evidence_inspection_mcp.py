"""The `mcp_app` surface has a writer, and it cannot lie about being one.

Migration 175 left `surface = 'mcp_app'` as a value nothing wrote. These tests
drive `core.evidence_inspection_mcp` without a FastMCP server, so the surface is
provable offline: the row lands, it carries `mcp_app`, the caller cannot choose
otherwise, and access is resolved before anything is written.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from core import evidence_inspection_mcp as mod

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

# EVERY STATEMENT THIS PATH ISSUES, NAMED. Six of the seven were never modelled:
# the fake answered the INSERT and let the whole acquisition seam fall into a
# mute `if` that returned `None` with `description` left at `None` too. Three of
# those six are read by the product, and one of them is a GUARD -- so the silence
# was not neutral, it was disabling a check (AI-317):
#
#   * `identity_bridge.py:82` resolves the caller's canonical identity, and its
#     `except Exception` swallowed the `AttributeError` the missing `fetchall`
#     raised. The bridge "failed open" on a fixture defect, not on a decision.
#   * `db.py:201` asks `current_user` before `SET ROLE` (`db.py:204`), and read
#     `None` -- so the fixture took the production branch by accident.
#   * `db.py:285` reads the two session settings BACK after the commit
#     (`_refuse_a_connection_that_forgets_its_context`). Answering nothing hit
#     the "a doublure that does not script this query gets no verdict" escape,
#     which is precisely how a pooled connection that forgets its context would
#     have looked. The fake now keeps the settings it was told to set, so the
#     guard is actually exercised.
#
# `identity_setting` is declared BEFORE `enforce_setting`: both are `set_config`,
# and declaration order is first match wins.
_ACQUISITION = StatementInventory(
    "_Cur (record_inspection_from_app)",
    person_identity="from app.person_identities",
    current_user="select current_user",
    set_role="set role",
    identity_setting="set_config('toorow.identity'",
    enforce_setting="set_config('toorow.enforce_epic36'",
    session_settings="select current_setting('toorow.enforce_epic36'",
    inspection_insert="insert into app.evidence_inspections",
    # The GRANT (2026-08-30). The inspection is refused without a server-minted
    # handle, so the two statements `core.result_app_grants` issues are part of
    # this path now and are named here rather than answered by accident.
    grant_touch="update app.result_app_grants",
    grant_select="from app.result_app_grants",
)

#: The role the session opens as. NOT `db.APP_ROLE`, which is what makes the
#: product issue `SET ROLE` -- the production shape `db.py:177` documents.
_SESSION_ROLE = "postgres"


class _Cur:
    """Loud on anything it was never taught. See `_ACQUISITION` above."""

    def __init__(self, conn):
        self.conn = conn
        self.store = conn.store
        self.description = None
        self._result = None
        self._rows: list[tuple] = []
        self.rowcount = -1

    def execute(self, sql, params=None):
        statement = _ACQUISITION.match(sql)
        self.description = None
        self._result = None
        self._rows = []
        self.rowcount = -1
        match statement:
            case "person_identity":
                # No `app.person_identities` row for this subject, so the bridge
                # returns the identity unchanged -- the honest answer here, and
                # the one every row below is written under.
                self.description = describe(sql)
            case "current_user":
                # `describe` refuses a projection with no FROM; psycopg names
                # this column `current_user`.
                self.description = [("current_user",)]
                self._result = (_SESSION_ROLE,)
            case "set_role":
                pass  # no result set
            case "identity_setting" | "enforce_setting":
                # `set_config` returns the value it set, in one row.
                self.description = [("set_config",)]
                value = params[0] if params else "on"
                key = (
                    "toorow.identity"
                    if statement == "identity_setting"
                    else "toorow.enforce_epic36"
                )
                self.conn.settings[key] = value
                self._result = (value,)
            case "session_settings":
                self.description = [("current_setting",), ("current_setting",)]
                self._result = (
                    self.conn.settings.get("toorow.enforce_epic36"),
                    self.conn.settings.get("toorow.identity"),
                )
            case "inspection_insert":
                self.store.append(params)
            case "grant_select":
                self.description = describe(sql)
                grant = self.conn.grants.get(params[0])
                # `load_grant` scopes by project: a grant of another Project is
                # absent, exactly as the product's WHERE clause makes it.
                if grant is not None and grant[2] == params[1]:
                    self._result = grant
            case "grant_touch":
                # `touch_handle` returns rowcount == 1 for a live grant only.
                handle_id = params[1]
                grant = self.conn.grants.get(handle_id)
                live = grant is not None and handle_id not in self.conn.revoked
                self.rowcount = 1 if live else 0
                if live:
                    self.conn.touched.append(handle_id)
            case _:  # pragma: no cover - a name added to the inventory, unanswered
                raise _ACQUISITION.unknown(sql)

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _Conn:
    def __init__(self, store, grants=None, revoked=()):
        self.store = store
        #: `handle_id -> the eleven columns `load_grant` projects`. A dict, not a
        #: single row, so "the handle of another Result" is a real second grant
        #: rather than a flag the fake interprets.
        self.grants: dict[str, tuple] = dict(grants or {})
        #: Handles whose grant is no longer live at consumption time.
        self.revoked: set[str] = set(revoked)
        #: Every handle `touch_handle` recorded a use against.
        self.touched: list[str] = []
        #: The session settings `install_access_context` committed, kept so the
        #: read-back guard (`db.py:285`) reads what was actually set rather than
        #: falling into its "no verdict" escape.
        self.settings: dict[str, str] = {}
        #: How many rows existed at each commit. Story 21.6 made the acquisition
        #: seam commit the access context, so "did it commit at all" stopped
        #: being a proxy for "did it write" -- an empty commit now always
        #: happens, before the caller does anything. What the tests below mean
        #: is that no commit ever carried a row, which this records directly.
        self.commits: list[int] = []

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


#: A handle is `rh_` + 26 Crockford base32 characters -- `result_slices._HANDLE_RE`
#: refuses anything else before a row is loaded, so the fixture must spell real ones.
HANDLE = "rh_01234567890123456789ABCDEF"
OTHER_RESULT_HANDLE = "rh_01234567890123456789ABCDEG"


def _grant(handle_id: str, result_id: str) -> tuple:
    """The eleven columns `result_app_grants.load_grant` projects, in order.

    Written from the product's own SELECT rather than invented: handle_id,
    org_id, project_id, result_id, content_hash, issued_to_identity,
    allowed_columns, issued_at, last_read_at, expires_at, revoked_at.
    """
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    return (
        handle_id,
        "org_EXAMPLE",
        "proj_EXAMPLE",
        result_id,
        "sha256:EXAMPLE",
        "owner@example.com",
        ["day", "clicks"],
        now,
        now,
        now + timedelta(hours=8),
        None,
    )


@pytest.fixture
def wired(monkeypatch):
    """A caller with `view` on proj_EXAMPLE, and a connection that keeps the row."""
    store: list[tuple] = []
    conn = _Conn(
        store,
        grants={
            HANDLE: _grant(HANDLE, "res_EXAMPLE"),
            OTHER_RESULT_HANDLE: _grant(OTHER_RESULT_HANDLE, "res_OTHER"),
        },
    )

    monkeypatch.setattr("core.db.get_connection", lambda *_a, **_k: conn)
    monkeypatch.setattr("core.db.set_local_access_context", lambda *_a, **_k: None)
    monkeypatch.setattr("core.mcp_profiles._identity", lambda: "owner@example.com")
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda *_a, **_k: SimpleNamespace(allowed=True, org_id="org_EXAMPLE", reason=None),
    )
    return SimpleNamespace(store=store, conn=conn)


def _record(**overrides):
    fields = {
        "project_id": "proj_EXAMPLE",
        "kind": "branch_subtree_expanded",
        "displayed_state": "branches_listed",
        "handle": HANDLE,
        "branches_listed": 3,
        "ai_path_id": "aip_0123456789ABCDEFGHJKMNPQRS",
        "step_ordinal": 2,
    }
    fields.update(overrides)
    return mod.record_inspection_from_app(**fields)


def test_the_mcp_app_surface_finally_writes(wired):
    result = _record()

    assert result["surface"] == "mcp_app"
    assert result["inspection_id"].startswith("evi_")
    assert len(wired.store) == 1, "exactly one row, for one inspection"
    assert wired.conn.committed_a_row is True


def test_the_row_carries_mcp_app_and_the_caller_cannot_choose(wired):
    """`surface` is pinned. A widget that could name `console` would let the
    evidence say an inspection happened somewhere it did not."""
    _record()
    assert "mcp_app" in wired.store[0]
    assert "console" not in wired.store[0]

    with pytest.raises(TypeError):
        mod.record_inspection_from_app(
            project_id="proj_EXAMPLE",
            kind="branch_subtree_expanded",
            displayed_state="branches_listed",
            branches_listed=1,
            handle=HANDLE,
            surface="console",  # not a parameter, and must not become one
        )


def test_branches_not_recorded_survives_the_seam(wired):
    """The state that is NOT a zero must be recordable, or 175's fourth value is moot."""
    _record(displayed_state="branches_not_recorded", branches_listed=None)
    row = wired.store[0]
    assert "branches_not_recorded" in row
    assert None in row


def test_a_refused_vocabulary_is_named_not_swallowed(wired):
    """The strict writer, like the console route: a wrong description is told."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as excinfo:
        _record(kind="rummaged_about")

    payload = json.loads(str(excinfo.value))
    assert payload["code"], "a refusal must carry a stable code"
    assert wired.store == [], "a refused inspection must not reach the table"


def test_a_caller_without_access_is_refused_before_anything_is_written(monkeypatch, wired):
    from fastmcp.exceptions import ToolError

    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda *_a, **_k: SimpleNamespace(allowed=False, org_id=None, reason="denied"),
    )

    with pytest.raises(ToolError) as excinfo:
        _record()

    payload = json.loads(str(excinfo.value))
    assert payload["code"] == "evidence_inspection_unavailable"
    assert wired.store == []
    assert wired.conn.committed_a_row is False


def test_a_missing_project_is_named_rather_than_defaulted(wired):
    """The AI-125 class: an auto-attached `default` project is a cross-tenant write."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as excinfo:
        _record(project_id="   ")

    assert json.loads(str(excinfo.value))["code"] == "missing_project"
    assert wired.store == []


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: an unrecognised statement must NAME itself, not answer no rows.

    The old fake answered every statement but the INSERT with `None` and a
    `description` of `None` -- the shape psycopg reserves for a statement that
    returned NO RESULT SET. On this path that silence was load-bearing: it is
    what made `_refuse_a_connection_that_forgets_its_context` decline to form a
    verdict, so the guard never ran in six green tests.
    """
    cursor = _Cur(_Conn([]))
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT kind, displayed_state FROM app.evidence_inspections "
            "WHERE project_id = %s ORDER BY occurred_at DESC"
        )
    message = str(raised.value)
    assert "order by occurred_at desc" in message
    assert "person_identity" in message


# ---------------------------------------------------------------------------
# THE GRANT (2026-08-30) -- `mcp-tool-surface.md`, "Incomplete if" n. 8.
#
# The tool is declared `insights` / `effect="read"` and appends a row. That
# declaration is true only while the append is impossible for a caller who
# merely knows the tool's name, so these four cases are the declaration's proof,
# not a decoration on it: accepted, missing, another Result's, no longer live.
# ---------------------------------------------------------------------------


def test_the_accepted_handle_names_the_result_the_row_records(wired):
    """The grant BINDS: the row carries the Result the server issued it over."""
    _record(result_ref=None)

    row = wired.store[0]
    assert "res_EXAMPLE" in row, "the row must name the granted Result"
    assert wired.conn.touched == [HANDLE], "the handle is consumed, once"


def test_without_a_handle_nothing_is_appended_and_the_gesture_is_named(wired):
    """The whole point of the clause: the discovery filter is not a guard.

    A model that knows the name reaches the same `tools/call`; what stops it is
    a grant it never receives, and the refusal tells a widget what to do rather
    than answering the mute unavailable envelope.
    """
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as excinfo:
        _record(handle="")

    payload = json.loads(str(excinfo.value))
    assert payload["code"] == "result_handle_required"
    assert "result_handle" in payload["message"], "the refusal names the gesture"
    assert wired.store == []
    assert wired.conn.committed_a_row is False


def test_a_handle_issued_over_another_result_is_refused(wired):
    """An append is only possible against the handle of THAT very Result."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as excinfo:
        _record(handle=OTHER_RESULT_HANDLE, result_ref="res_EXAMPLE")

    payload = json.loads(str(excinfo.value))
    assert payload["code"] == "result_handle_names_another_result"
    assert wired.store == []
    assert wired.conn.touched == []


def test_a_handle_the_server_never_issued_is_refused(wired):
    """Absent, foreign, revoked and expired share ONE envelope -- `resolve_handle`
    collapses them and this module only re-labels, never splits."""
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as excinfo:
        _record(handle="rh_ZZZZZZZZZZZZZZZZZZZZZZZZZZ")

    assert json.loads(str(excinfo.value))["code"] == "result_handle_not_usable"
    assert wired.store == []


def test_a_handle_that_stopped_being_live_refuses_and_commits_nothing(monkeypatch):
    """Consumption is a check, not a formality.

    The grant resolves and the INSERT runs, then `touch_handle` finds no live row
    -- revoked in another session, or idled out between the two statements. The
    transaction must not commit: an observation whose grant died is an
    observation nobody can attribute.
    """
    from fastmcp.exceptions import ToolError

    conn = _Conn(
        [],
        grants={HANDLE: _grant(HANDLE, "res_EXAMPLE")},
        revoked={HANDLE},
    )
    monkeypatch.setattr("core.db.get_connection", lambda *_a, **_k: conn)
    monkeypatch.setattr("core.db.set_local_access_context", lambda *_a, **_k: None)
    monkeypatch.setattr("core.mcp_profiles._identity", lambda: "owner@example.com")
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda *_a, **_k: SimpleNamespace(allowed=True, org_id="org_EXAMPLE", reason=None),
    )

    with pytest.raises(ToolError) as excinfo:
        _record()

    assert json.loads(str(excinfo.value))["code"] == "result_handle_not_usable"
    assert conn.committed_a_row is False
