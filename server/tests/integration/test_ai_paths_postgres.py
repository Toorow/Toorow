"""Live-Postgres guardrails for migration 150 (Story 49.6, the AI Path owner).

Every guarantee asserted here is enforced by a constraint or a trigger, which is
exactly why these tests need a real database: a mocked cursor accepts an UPDATE
against finalized evidence and reports success. Immutability that is only a
convention in the service layer is immutability that ends the first time
somebody writes a second writer.

Two traps this file is written around, both found by Story 49.5 when its own
version of these tests was executed for the first time:

* a bare ``conn.rollback()`` after an expected violation destroys the fixtures,
  so every later violation touches no row and reports DID NOT RAISE while the
  constraint works perfectly. Each expected refusal is bounded by a SAVEPOINT;
* an RLS test run as a superuser or as the table owner passes while proving
  nothing. :func:`test_rls_hides_paths_from_an_identity_without_a_grant` refuses
  to run as either.
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager

import psycopg
import pytest


def _hash(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _crockford(seed: str) -> str:
    """The id CHECKs use Crockford base32, which excludes I, L, O and U."""
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return "".join(alphabet[byte % 32] for byte in digest[:26])


def _ensure_org(conn) -> str:
    org_id = f"org_aip_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, 'active', %s)",
            (org_id, "AI Path test", org_id.lower(), "test@example.com"),
        )
    return org_id


def _insert_project(conn, org_id: str) -> str:
    project_id = f"proj_aip_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (project_id, org_id, "AI Path test", project_id.lower(), "test@example.com"),
        )
    return project_id


def _insert_path(conn, org_id: str, project_id: str, **columns) -> str:
    path_id = "aip_" + _crockford(f"path{uuid.uuid4()}")
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    trailing = f", {names}" if columns else ""
    trailing_values = f", {placeholders}" if columns else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.ai_paths
                (id, org_id, project_id, actor, policy_snapshot, policy_snapshot_hash{trailing})
            VALUES (%s, %s, %s, 'pytest', '{{}}'::jsonb, %s{trailing_values})
            """,
            (path_id, org_id, project_id, _hash(path_id), *columns.values()),
        )
    return path_id


def _insert_step(conn, path_id: str, org_id: str, project_id: str, ordinal: int = 0, **kw):
    step_id = "aps_" + _crockford(f"{path_id}:{ordinal}:{uuid.uuid4()}")
    columns = {"step_kind": "tool_call", "outcome": "succeeded", **kw}
    names = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.ai_path_steps
                (id, path_id, org_id, project_id, ordinal, {names})
            VALUES (%s, %s, %s, %s, %s, {placeholders})
            """,
            (step_id, path_id, org_id, project_id, ordinal, *columns.values()),
        )
    return step_id


def _finalize(conn, path_id: str, project_id: str, outcome: str = "succeeded"):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.ai_paths
               SET lifecycle = 'finalized', outcome = %s, ended_at = NOW(), content_hash = %s
             WHERE id = %s AND project_id = %s
            """,
            (outcome, _hash(path_id + outcome), path_id, project_id),
        )


@contextmanager
def refuses(conn):
    """Assert the next statement is refused, WITHOUT destroying the fixtures.

    The refusal is asserted on the SQLSTATE class, not on a Python class. In
    psycopg 3 the concrete violations do NOT share the base one would expect --
    `UniqueViolation.__mro__` is `IntegrityError`, and `issubclass(UniqueViolation,
    IntegrityConstraintViolation)` is False. Catching
    `IntegrityConstraintViolation` therefore caught only the refusals raised by
    a trigger with an explicit `ERRCODE = '23000'`, and let every CHECK (23514),
    UNIQUE (23505) and FOREIGN KEY (23503) violation escape as an error. Class
    23 is what "integrity constraint violation" means in PostgreSQL, so class 23
    is what this asserts.
    """
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT expected_violation")
    try:
        with pytest.raises(psycopg.Error) as caught:
            yield
    finally:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT expected_violation")
    sqlstate = caught.value.sqlstate or ""
    assert sqlstate.startswith("23"), (
        f"expected an integrity violation (SQLSTATE class 23), got {sqlstate!r}: {caught.value}"
    )


@pytest.fixture()
def scope(live_postgres):
    org_id = _ensure_org(live_postgres)
    project_id = _insert_project(live_postgres, org_id)
    return live_postgres, org_id, project_id


# --- Immutability -----------------------------------------------------------


def test_a_finalized_path_refuses_any_further_update(scope):
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)
    _finalize(conn, path_id, project_id)

    with refuses(conn), conn.cursor() as cur:
        cur.execute("UPDATE app.ai_paths SET outcome = 'failed' WHERE id = %s", (path_id,))


def test_a_path_refuses_delete(scope):
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)

    with refuses(conn), conn.cursor() as cur:
        cur.execute("DELETE FROM app.ai_paths WHERE id = %s", (path_id,))


def test_steps_are_append_only(scope):
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)
    step_id = _insert_step(conn, path_id, org_id, project_id)

    with refuses(conn), conn.cursor() as cur:
        cur.execute("UPDATE app.ai_path_steps SET outcome = 'failed' WHERE id = %s", (step_id,))

    with refuses(conn), conn.cursor() as cur:
        cur.execute("DELETE FROM app.ai_path_steps WHERE id = %s", (step_id,))


def test_a_finalized_path_cannot_grow_a_new_step(scope):
    """Otherwise `content_hash` silently stops describing the path it hashes."""
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)
    _insert_step(conn, path_id, org_id, project_id, ordinal=0)
    _finalize(conn, path_id, project_id)

    with refuses(conn):
        _insert_step(conn, path_id, org_id, project_id, ordinal=1)


def test_finalization_may_not_rewrite_the_pinned_policy(scope):
    """The snapshot is what the execution is judged against. A finalization that
    could rewrite it would let a run choose the rules it is measured by."""
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)

    with refuses(conn), conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.ai_paths
               SET lifecycle = 'finalized', outcome = 'succeeded', ended_at = NOW(),
                   content_hash = %s, policy_snapshot_hash = %s
             WHERE id = %s
            """,
            (_hash("a"), _hash("rewritten"), path_id),
        )


def test_a_legitimate_finalization_is_accepted(scope):
    """The counterpart the refusals need: the one legal transition still works.

    Without this, every assertion above would also pass against a trigger that
    refused everything, including the transition the owner depends on.
    """
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)
    _insert_step(conn, path_id, org_id, project_id, ordinal=0)

    _finalize(conn, path_id, project_id, outcome="failed")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT lifecycle, outcome, ended_at IS NOT NULL, content_hash IS NOT NULL "
            "FROM app.ai_paths WHERE id = %s",
            (path_id,),
        )
        assert cur.fetchone() == ("finalized", "failed", True, True)


# --- Lifecycle completeness -------------------------------------------------


def test_a_finalized_path_without_an_outcome_is_refused(scope):
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)

    with refuses(conn), conn.cursor() as cur:
        cur.execute("UPDATE app.ai_paths SET lifecycle = 'finalized' WHERE id = %s", (path_id,))


def test_a_recording_path_cannot_already_carry_an_outcome(scope):
    conn, org_id, project_id = scope

    with refuses(conn):
        _insert_path(conn, org_id, project_id, outcome="succeeded")


# --- Identity and scope -----------------------------------------------------


def test_a_step_cannot_name_a_path_from_another_project(scope):
    """The composite foreign key, not a comment: cross-tenant attachment is
    refused by the database even if a service forgets to check."""
    conn, org_id, project_id = scope
    other_project = _insert_project(conn, org_id)
    path_id = _insert_path(conn, org_id, project_id)

    with refuses(conn):
        _insert_step(conn, path_id, org_id, other_project)


def test_the_all_zero_trace_id_is_refused_by_the_database(scope):
    conn, org_id, project_id = scope

    with refuses(conn):
        _insert_path(conn, org_id, project_id, w3c_trace_id="0" * 32)


def test_a_real_trace_id_is_accepted(scope):
    conn, org_id, project_id = scope

    path_id = _insert_path(conn, org_id, project_id, w3c_trace_id="a1b2" * 8)

    with conn.cursor() as cur:
        cur.execute("SELECT w3c_trace_id FROM app.ai_paths WHERE id = %s", (path_id,))
        assert cur.fetchone()[0] == "a1b2" * 8


def test_half_a_skill_pin_is_refused(scope):
    """A step id without its Skill version resolves to the wrong step the first
    time the Skill is revised."""
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)

    with refuses(conn):
        _insert_step(conn, path_id, org_id, project_id, skill_step_id="step-1")


def test_a_version_pin_without_an_owner_is_refused(scope):
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)

    with refuses(conn):
        _insert_step(conn, path_id, org_id, project_id, owner_version_id="v1")


def test_two_steps_cannot_share_an_ordinal(scope):
    """Order is a fact of the observation, not a rendering preference."""
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)
    _insert_step(conn, path_id, org_id, project_id, ordinal=0)

    with refuses(conn):
        _insert_step(conn, path_id, org_id, project_id, ordinal=0)


# --- Row Level Security -----------------------------------------------------


@pytest.mark.pg_app_role
def test_rls_hides_paths_from_an_identity_without_a_grant(scope):
    """RLS is the floor. With enforcement on and no resource grant, the rows are
    not filtered in the application -- they are not visible at all.

    The `pg_app_role` marker makes the WRONG role a skip rather than a failure.
    The assertions below used to carry that job alone, and they did it correctly
    -- a vacuous green is worse than a red -- but a whole-suite run then reported
    them as failures, which measures the connection, not the code. A skip says
    what is true: this test was not run.

    The assertions stay. The marker decides whether the test runs; they decide
    whether it is allowed to conclude, and a marker that someone removes should
    not silently re-enable a vacuous pass.
    """
    conn, org_id, project_id = scope

    with conn.cursor() as cur:
        cur.execute("SELECT current_user, rolsuper FROM pg_roles WHERE rolname = current_user")
        role, is_superuser = cur.fetchone()
        cur.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid = 'app.ai_paths'::regclass"
        )
        owner = cur.fetchone()[0]
    assert not is_superuser, (
        f"connected as superuser {role!r}: RLS is bypassed, so this test would pass "
        "without proving anything. Point TEST_POSTGRES_DSN at the deployed application role."
    )
    assert role != owner, (
        f"connected as the table owner {role!r}: run as the deployed application role."
    )

    _insert_path(conn, org_id, project_id)

    with conn.cursor() as cur:
        cur.execute("SET LOCAL toorow.enforce_epic36 = 'on'")
        cur.execute("SET LOCAL toorow.identity = 'nobody@example.com'")
        cur.execute("SELECT count(*) FROM app.ai_paths WHERE project_id = %s", (project_id,))
        assert cur.fetchone()[0] == 0


# --- A refused crossing must not end the walk -------------------------------


def test_a_refused_step_leaves_the_connection_usable_for_the_next_one(scope):
    """`record_tool_call` catches a bad crossing and says "one bad crossing is
    not the walk". Catching was never enough, and only a database can show it:
    a refused statement ABORTS the transaction, so every later statement --
    including the `tool_call` step that describes the call itself -- died with
    `InFailedSqlTransaction`.

    Measured 2026-08-04 on a real MCP call: `search_context` recorded ZERO
    steps, header included, while `health` (which crosses nothing) recorded
    one. The fix is a SAVEPOINT per crossing, which is what this asserts.
    """
    conn, org_id, project_id = scope
    path_id = _insert_path(conn, org_id, project_id)

    # A partial owner reference — exactly what a context walk that found
    # nothing produced. `ck_ai_path_steps_owner_complete` refuses it.
    with pytest.raises(psycopg.Error), conn.transaction():
        _insert_step(
            conn, path_id, org_id, project_id, ordinal=0,
            step_kind="knowledge_read", owner_workspace="context-hub",
        )

    # The connection is still usable, and THAT is the whole property: without
    # the SAVEPOINT this insert raised InFailedSqlTransaction and the path
    # stayed empty.
    _insert_step(
        conn, path_id, org_id, project_id, ordinal=0,
        step_kind="tool_call", tool_name="search_context",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.ai_path_steps WHERE path_id = %s", (path_id,))
        assert cur.fetchone()[0] == 1
