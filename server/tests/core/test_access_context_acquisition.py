"""Story 21.6 -- the access context is installed at ACQUISITION, or not at all.

WHAT THESE TESTS ARE FOR. The Epic-36 policies were never decorative by design:
all 70 of them read

    current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(...)

so an unset GUC makes the first disjunct TRUE and the policy returns every row
without checking anything. Arming it was left to each caller, and 113 of the 118
modules that read an RLS-forced table never did. This file pins the seam that
removes the choice: a request-scoped connection arrives armed.

These are pure-function tests on a fake connection -- they assert WHAT IS SENT to
the database, not what the database does with it. The row-level proof (insert
into project A, fail to read it as a subject granted only project B) runs in
`test_request_connection_rls_pg.py` against real Postgres, where a policy can
actually bite.
"""

from __future__ import annotations

import pytest
from core import db


class FakeCursor:
    def __init__(self, statements: list[tuple[str, tuple | None]]):
        self._statements = statements

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, params: tuple | None = None) -> None:
        self._statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        """Repondre CE QU'ON A REELLEMENT DEMANDE DE POSER, pas un bouchon.

        L'acquisition relit le plancher apres son commit pour refuser une
        connexion qui l'a oublie -- ce qu'un pooler en mode transaction ferait
        (`core.db._refuse_a_connection_that_forgets_its_context`). Une doublure
        qui rendrait une valeur figee ferait passer ce test quel que soit le code,
        et une qui ne rend rien le ferait echouer sans rien mesurer. Celle-ci
        rejoue les `set_config` enregistres, dans l'ordre : elle se comporte comme
        une session qui se souvient, donc le cas nominal passe pour la bonne
        raison et le cas « la session oublie » reste falsifiable ailleurs
        (test_request_connection_rls_pg.py, contre un vrai Postgres).
        """
        current: dict[str, str | None] = {}
        for sql, params in self._statements:
            if "set_config" not in sql:
                continue
            name = sql.split("'")[1]
            current[name] = params[0] if params else sql.split("'")[3]
        return (
            current.get("toorow.enforce_epic36"),
            current.get("toorow.identity"),
        )


class FakeConnection:
    """Records statements and commits, so the ORDER of both can be asserted."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple | None]] = []
        self.commits = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.statements)

    def commit(self) -> None:
        self.commits += 1
        self.statements.append(("COMMIT", None))

    def close(self) -> None:
        self.closed = True


@pytest.fixture()
def fake_conn(monkeypatch) -> FakeConnection:
    """Make `get_connection` hand out a fake, so no Postgres is needed."""
    conn = FakeConnection()

    class _FakePsycopg:
        @staticmethod
        def connect(*_args, **_kwargs) -> FakeConnection:
            return conn

    monkeypatch.setattr(db, "_psycopg", _FakePsycopg)
    return conn


def _settings(conn: FakeConnection) -> dict[str, str]:
    """The GUCs the connection was actually told to set, name -> value."""
    found: dict[str, str] = {}
    for sql, params in conn.statements:
        if "set_config" not in sql:
            continue
        name = sql.split("'")[1]
        found[name] = params[0] if params else sql.split("'")[3]
    return found


def _is_session_scoped(conn: FakeConnection, guc: str) -> bool:
    """`set_config(name, value, is_local)` -- False means it survives the txn."""
    for sql, _params in conn.statements:
        if "set_config" in sql and f"'{guc}'" in sql:
            return sql.rstrip(")").rstrip().endswith("false")
    raise AssertionError(f"{guc} was never set")


# --------------------------------------------------------------------------
# AC1 -- the caller does not have to remember
# --------------------------------------------------------------------------


def test_request_connection_arms_the_connection_it_yields(fake_conn, monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    with db.request_connection("person_alice") as conn:
        assert conn is fake_conn
    settings = _settings(fake_conn)
    assert settings["toorow.identity"] == "person_alice"
    assert settings["toorow.enforce_epic36"] == "on"


def test_the_connection_is_still_closed_on_exit(fake_conn, monkeypatch):
    """The seam wraps `get_connection`; it must not lose its `finally`."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    with db.request_connection("person_alice"):
        pass
    assert fake_conn.closed


# --------------------------------------------------------------------------
# The trap named in the story's Dev Notes: a commit resets a transaction-local
# GUC, and command handlers commit in the middle of their work.
# --------------------------------------------------------------------------


def test_the_context_survives_a_commit_made_by_the_handler(fake_conn, monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    with db.request_connection("person_alice"):
        pass
    assert _is_session_scoped(fake_conn, "toorow.identity")
    assert _is_session_scoped(fake_conn, "toorow.enforce_epic36")


def test_the_context_is_committed_before_the_handler_gets_the_connection(
    fake_conn, monkeypatch
):
    """A session SET rolled back is a floor that was never there.

    Committing at acquisition is what makes the setting outlive a later
    ROLLBACK -- and the commit must land BEFORE the handler runs, not after.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    handler_saw = {}
    with db.request_connection("person_alice") as conn:
        handler_saw["commits_before_first_statement"] = conn.commits
    assert handler_saw["commits_before_first_statement"] == 1

    # L'assertion portait sur « la DERNIERE instruction est un COMMIT ». C'etait
    # une facon indirecte de dire « le commit precede la main du handler », et
    # elle est devenue fausse pour une bonne raison : l'acquisition RELIT
    # desormais le plancher apres son commit, pour refuser une connexion qui
    # l'aurait oublie. Ce qui compte est l'ORDRE, et il est asserte directement.
    kinds = [sql for sql, _params in fake_conn.statements]
    assert kinds.count("COMMIT") == 1
    commit_at = kinds.index("COMMIT")
    allowed = ("set_config", "SET ROLE", "SELECT current_user")
    assert all(any(a in sql for a in allowed) for sql in kinds[:commit_at]), (
        f"quelque chose d'autre que l'armement s'execute avant le commit : {kinds[:commit_at]}"
    )
    assert "current_setting" in kinds[commit_at + 1], (
        "la relecture de verification ne suit pas immediatement le commit"
    )


def test_set_local_access_context_stays_transaction_local(fake_conn):
    """The old helper is unchanged: its callers hand in a connection they borrow.

    Widening ITS scope would leave a session-scoped setting on somebody else's
    connection -- including the shared fixture connection used across tests.
    """
    db.set_local_access_context(fake_conn, "person_alice", enforce_epic36=True)
    assert not _is_session_scoped(fake_conn, "toorow.enforce_epic36")


# --------------------------------------------------------------------------
# AC2 -- no silent unarmed fallback
# --------------------------------------------------------------------------


@pytest.mark.parametrize("identity", ["", "   ", "anonymous", None])
def test_a_request_without_a_resolved_identity_is_refused(fake_conn, monkeypatch, identity):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    with pytest.raises(ValueError, match="identity"):
        with db.request_connection(identity):  # type: ignore[arg-type]
            pass
    assert _settings(fake_conn) == {}, "an unarmed connection was handed out anyway"


def test_the_auth_disabled_local_operator_is_the_only_unarmed_subject(
    fake_conn, monkeypatch
):
    """Self-host keeps working; the carve-out ends the moment auth is enabled."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    with db.request_connection("anonymous") as conn:
        assert conn is fake_conn
    assert _settings(fake_conn) == {}

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    with pytest.raises(ValueError):
        with db.request_connection("anonymous"):
            pass


def test_a_named_identity_is_armed_even_when_auth_is_disabled(fake_conn, monkeypatch):
    """The carve-out is for `anonymous`, not for auth-disabled generally."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    with db.request_connection("person_alice"):
        pass
    assert _settings(fake_conn)["toorow.enforce_epic36"] == "on"


# --------------------------------------------------------------------------
# AC3 -- a background path declares that it does not isolate, and why
# --------------------------------------------------------------------------


def test_background_connection_does_not_arm_the_floor(fake_conn):
    with db.background_connection("nightly dispatch works for every org") as conn:
        assert conn is fake_conn
    assert _settings(fake_conn) == {}


def test_background_connection_refuses_an_unexplained_acquisition(fake_conn):
    """An unisolated path must read as a decision, never as an omission."""
    for empty in ("", "   "):
        with pytest.raises(ValueError, match="reason"):
            with db.background_connection(empty):
                pass
    assert not fake_conn.statements


def test_a_refused_set_role_leaves_a_usable_transaction() -> None:
    """The graceful degradation was fictional: Postgres aborts on the failed statement.

    `_become_the_application_role` catches a refused `SET ROLE`, logs that RLS
    will not bite, and carries on -- "on n'echoue pas". But in Postgres a failed
    statement ABORTS the transaction, so the very next `set_config` raises
    `InFailedSqlTransaction` and the connection is unusable for the rest of the
    request. The degradation it promised never existed.

    MEASURED live 2026-08-07 on `mcp-server`: the deployment connects as a role
    that may not `SET ROLE "connector"`, and EVERY endpoint built on
    `request_connection` answered 500 with "current transaction is aborted,
    commands ignored until end of transaction block" -- among them the inbound
    address issuance, which is the last step of the whole inbound journey.

    Recovering the transaction is what makes the named posture real: the
    connection is fresh here (the acquisition says so), so a rollback discards
    nothing but the failure itself.
    """

    class _RefusingCursor(FakeCursor):
        def execute(self, sql: str, params: tuple | None = None) -> None:
            if sql.strip().upper().startswith("SET ROLE"):
                self._statements.append(("SET ROLE -> refused", None))
                raise RuntimeError('permission denied to set role "connector"')
            super().execute(sql, params)

    class _RefusingConnection(FakeConnection):
        def __init__(self) -> None:
            super().__init__()
            self.rollbacks = 0

        def cursor(self) -> _RefusingCursor:
            return _RefusingCursor(self.statements)

        def rollback(self) -> None:
            self.rollbacks += 1
            self.statements.append(("ROLLBACK", None))

    conn = _RefusingConnection()
    db.install_access_context(conn, "owner@example.com")

    assert conn.rollbacks == 1, "the aborted transaction was never recovered"
    order = [sql for sql, _ in conn.statements]
    assert order.index("ROLLBACK") < next(
        index for index, sql in enumerate(order) if "toorow.identity" in sql
    ), "the context is set inside a transaction that is still aborted"
