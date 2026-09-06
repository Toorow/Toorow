"""The organization owner was a stranger on the MCP.

THE MEASUREMENT THAT OPENED THIS FILE, production, 2026-08-12, same project, same
instant, through `core.project_access.resolve_strict_resource_access`:

    117505563874619937900              -> allowed=False  reason=not_found
    person_01KYHQX4RK24Z6QJCYWX6DYVSQ  -> allowed=True   reason=owner_floor

MCP tools passed the first -- `token.claims["sub"]` -- while
`app.org_members.identity` and the RLS floor (`app.epic36_has_resource_access`)
only know the second. So `get_skills` answered `project_not_found` for an ACTIVE
project its caller owns, and the Skill -> View -> render -> AI path assembly was
out of reach of its own owner.

WHAT THESE TESTS PIN, and why each is a rule rather than an example: the bridge
TRANSLATES (it never creates a person), it does NOT touch what is already
canonical, and an AMBIGUOUS match is not a match. The third one matters most: two
issuers may carry the same subject, and a bridge that guesses opens somebody
else's project.
"""

from __future__ import annotations

import pytest
from core import identity_bridge

SUBJECT = "117505563874619937900"
PERSON = "person_01EXAMPLE0000000000000"
OTHER_PERSON = "person_01EXAMPLE0000000001"


class FakeCursor:
    def __init__(self, rows: list[tuple] | Exception):
        self._rows = rows
        self.queries: list[tuple] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql, params=None) -> None:
        if isinstance(self._rows, Exception):
            raise self._rows
        self.queries.append((sql, params))

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self, rows: list[tuple] | Exception):
        self._rows = rows
        self.cursors: list[FakeCursor] = []

    def cursor(self) -> FakeCursor:
        cur = FakeCursor(self._rows)
        self.cursors.append(cur)
        return cur


@pytest.fixture(autouse=True)
def _clean_bridge():
    identity_bridge.reset_cache()
    yield
    identity_bridge.reset_cache()


def test_an_oidc_subject_becomes_its_canonical_person():
    """The defect itself, written where it lives."""
    assert identity_bridge.canonical_identity(SUBJECT, FakeConnection([(PERSON,)])) == PERSON


def test_a_canonical_identity_is_never_looked_up():
    """The HTTP path already resolves: it pays nothing and does not change."""
    conn = FakeConnection([(OTHER_PERSON,)])

    assert identity_bridge.canonical_identity(PERSON, conn) == PERSON
    assert conn.cursors == [], "an already-canonical identity triggered a read"


def test_an_unknown_subject_passes_through_unchanged():
    """The bridge TRANSLATES; it does not create the person whose membership it checks."""
    assert identity_bridge.canonical_identity(SUBJECT, FakeConnection([])) == SUBJECT


def test_an_ambiguous_subject_is_refused_rather_than_guessed():
    """Two issuers, one subject: guessing would open somebody else's project."""
    conn = FakeConnection([(PERSON,), (OTHER_PERSON,)])

    assert identity_bridge.canonical_identity(SUBJECT, conn) == SUBJECT


def test_anonymous_and_empty_are_left_alone():
    """`anonymous` is the single self-host carve-out, and it belongs to `core.db`."""
    conn = FakeConnection([(PERSON,)])

    assert identity_bridge.canonical_identity("anonymous", conn) == "anonymous"
    assert identity_bridge.canonical_identity("", conn) == ""
    assert identity_bridge.canonical_identity("   ", conn) == ""
    assert conn.cursors == []


def test_a_database_failure_leaves_the_identity_unchanged():
    """A mute bridge REFUSES: access then resolves exactly as it did before."""
    assert identity_bridge.canonical_identity(SUBJECT, FakeConnection(RuntimeError("down"))) == (
        SUBJECT
    )


def test_the_translation_is_remembered_so_a_tool_call_costs_one_read():
    conn = FakeConnection([(PERSON,)])

    identity_bridge.canonical_identity(SUBJECT, conn)
    identity_bridge.canonical_identity(SUBJECT, conn)

    assert len(conn.cursors) == 1, "the bridge re-reads an append-only mapping"


def test_the_cache_is_bounded():
    """A flood of unknown subjects must not grow the bridge without end."""
    for index in range(identity_bridge._CACHE_LIMIT + 5):
        identity_bridge.canonical_identity(
            f"subject-{index}", FakeConnection([(f"person_{index:026d}",)])
        )

    assert len(identity_bridge._CACHE) <= identity_bridge._CACHE_LIMIT


# ---------------------------------------------------------------------------
# The two seams that must translate, and did not.
# ---------------------------------------------------------------------------


def test_request_connection_arms_the_floor_with_the_canonical_identity(monkeypatch):
    """Arming the floor on a raw subject arms it for a person who does not exist."""
    import contextlib

    from core import db as core_db

    armed: list[str] = []
    conn = FakeConnection([(PERSON,)])

    @contextlib.contextmanager
    def _fake_get_connection():
        yield conn

    monkeypatch.setattr(core_db, "get_connection", _fake_get_connection)
    monkeypatch.setattr(
        core_db, "install_access_context", lambda _conn, identity: armed.append(identity)
    )

    with core_db.request_connection(SUBJECT):
        pass

    assert armed == [PERSON]


def test_the_mcp_scope_gate_decides_on_the_canonical_identity(monkeypatch):
    """The gate handed the raw subject to the access decision -- and refused a member."""
    import contextlib

    from core import mcp_scope

    seen: list[str] = []

    monkeypatch.setattr(
        "core.identity_bridge.canonical_identity_standalone", lambda _identity: PERSON
    )
    monkeypatch.setattr("core.db._auth_is_disabled", lambda: False)

    @contextlib.contextmanager
    def _fake_request_connection(identity):
        seen.append(identity)
        yield FakeConnection([])

    monkeypatch.setattr("core.db.request_connection", _fake_request_connection)

    class _Allowed:
        allowed = True

    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda identity, _conn, **_kw: seen.append(identity) or _Allowed(),
    )

    mcp_scope.refuse_unless_project_scope("proj_EXAMPLE", SUBJECT)

    assert seen == [PERSON, PERSON], "acquisition and decision must speak of the same human"


def test_the_access_decision_translates_the_identity_it_compares(monkeypatch):
    """Thirty MCP modules hand it a raw subject; one place is where they all arrive."""
    from core import project_access

    compared: list[str] = []

    class _MemberCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def execute(self, sql, params=None):
            #  The membership comparison is the one that joins `org_members` on
            #  the identity: that parameter is what this test is about.
            if params and "org_members" in sql:
                compared.extend(
                    str(p) for p in params if str(p).startswith(("person_", "1175"))
                )
            self._row = ("org_01EXAMPLE0000000000000", "active", "owner", "active")

        def fetchone(self):
            return self._row

        def fetchall(self):
            return []

    class _Conn:
        def cursor(self):
            return _MemberCursor()

    monkeypatch.setattr("core.identity_bridge.canonical_identity", lambda _i, _c: PERSON)
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")

    project_access.resolve_strict_resource_access(
        SUBJECT, _Conn(), project_id="proj_EXAMPLE", minimum_capability="view"
    )

    assert compared, "no membership comparison happened, so the test proves nothing"
    assert set(compared) == {PERSON}, "the raw subject reached the membership comparison"
