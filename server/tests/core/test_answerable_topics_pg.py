"""Migrations 170, 172 and 173, REPLAYED against a live PostgreSQL.

WHY THIS FILE EXISTS. The four Epic 52 stories each shipped with the same named
reserve: "the pg-gated half is applied in production and verified structurally,
never replayed". Structurally means `information_schema` was asked whether the
constraints exist. That is not the same question as whether they REFUSE, and a
mocked cursor cannot refuse anything -- so every immutability and isolation claim
in the offline suites is a claim about Python, not about the database that is
supposed to be the last line.

WHAT THIS FILE PROVES, AND WHAT IT DOES NOT. Every assertion here is a REFUSAL by
a CHECK, a foreign key or a trigger -- and those are enforced for every role,
owner included. So this file carries no role marker.

It deliberately does NOT assert RLS BEHAVIOUR. A cross-tenant `SELECT` that comes
back empty proves nothing when the connected role owns the tables, and on the
disposable cluster `connector` does own them (it applied the migrations). The
last test below reads the `pg_class` flags instead -- it checks that the floor is
ARMED, which is honest, and stops short of claiming it was exercised. Exercising
it needs a DSN pointing at the deployed application role, and that is named as
remaining rather than faked here.

TWO TRAPS THIS FILE ENCODES, both already paid for by a session that got a green
run proving nothing:

1. **A refused statement aborts the whole transaction** in psycopg 3. Two
   `pytest.raises` in a row without a SAVEPOINT makes the second fail with
   `InFailedSqlTransaction` -- still an exception, so the assertion passes and
   proves nothing about the constraint it names. Every refusal below runs inside
   `conn.transaction()`, which is a savepoint.
2. **`live_postgres` rolls back on teardown**, so nothing here may depend on
   another test's rows. Each test builds its own chain.
"""

from __future__ import annotations

import pytest
from ulid import ULID

psycopg = pytest.importorskip("psycopg")


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _project(conn) -> tuple[str, str]:
    """Create an org + project to hang the chain on, inside the test transaction."""
    org_id = _uid("org")
    project_id = _uid("proj")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s)",
            (org_id, "Epic 52 pg", org_id.lower(), "tester"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, %s, %s, %s)",
            (project_id, org_id, "Epic 52 pg", project_id.lower(), "tester"),
        )
    return org_id, project_id


def _topic(conn, org_id: str, project_id: str, *, key: str = "pacing") -> tuple[str, str]:
    topic_id = _uid("atp")
    version_id = _uid("atv")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.answerable_topics
                (id, org_id, project_id, topic_key, lifecycle_state,
                 base_template_id, origin, created_by)
            VALUES (%s, %s, %s, %s, 'active', 'kpi', 'project', 'tester')
            """,
            (topic_id, org_id, project_id, key),
        )
        cur.execute(
            """
            INSERT INTO app.answerable_topic_versions
                (id, answerable_topic_id, org_id, project_id, version_number, title,
                 answers_question, kind, fallback_rank, base_template_id,
                 content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, 'Pacing', 'Are we pacing to plan?', 'kpi', 0,
                    'kpi', %s, 'tester')
            """,
            (version_id, topic_id, org_id, project_id, "a" * 64),
        )
    return topic_id, version_id


# ---------------------------------------------------------------------------
# Migration 170 -- a topic version is immutable, and the database says so.
# ---------------------------------------------------------------------------


def test_a_topic_version_cannot_be_updated(live_postgres):
    conn = live_postgres
    org_id, project_id = _project(conn)
    _topic_id, version_id = _topic(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.Error), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE app.answerable_topic_versions SET title = %s WHERE id = %s",
            ("rewritten behind the operator's back", version_id),
        )


def test_a_topic_version_cannot_be_deleted(live_postgres):
    conn = live_postgres
    org_id, project_id = _project(conn)
    _topic_id, version_id = _topic(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.Error), conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM app.answerable_topic_versions WHERE id = %s", (version_id,))


def test_retiring_keeps_the_row(live_postgres):
    """Retiring removes a question from the catalog; it deletes nothing."""
    conn = live_postgres
    org_id, project_id = _project(conn)
    topic_id, _version_id = _topic(conn, org_id, project_id)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.answerable_topics SET lifecycle_state = 'retired' WHERE id = %s",
            (topic_id,),
        )
        cur.execute(
            "SELECT lifecycle_state FROM app.answerable_topics WHERE id = %s", (topic_id,)
        )
        assert cur.fetchone()[0] == "retired"
        cur.execute(
            "SELECT count(*) FROM app.answerable_topic_versions WHERE answerable_topic_id = %s",
            (topic_id,),
        )
        assert cur.fetchone()[0] == 1


@pytest.mark.parametrize("state", ["draft", "deleted", ""])
def test_an_unknown_lifecycle_state_is_refused(live_postgres, state):
    conn = live_postgres
    org_id, project_id = _project(conn)
    topic_id, _v = _topic(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE app.answerable_topics SET lifecycle_state = %s WHERE id = %s",
            (state, topic_id),
        )


@pytest.mark.parametrize("key", ["Weekly Pacing", "-leading", "UPPER"])
def test_a_malformed_topic_key_is_refused_by_the_database(live_postgres, key):
    conn = live_postgres
    org_id, project_id = _project(conn)

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.answerable_topics
                (id, org_id, project_id, topic_key, lifecycle_state, base_template_id,
                 origin, created_by)
            VALUES (%s, %s, %s, %s, 'active', 'kpi', 'project', 'tester')
            """,
            (_uid("atp"), org_id, project_id, key),
        )


def test_a_version_cannot_point_at_a_topic_of_another_project(live_postgres):
    """The composite foreign key, doing what a bare id could not."""
    conn = live_postgres
    org_a, project_a = _project(conn)
    _org_b, project_b = _project(conn)
    topic_a, _v = _topic(conn, org_a, project_a)

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction(), conn.cursor() as cur:  # noqa: E501
        cur.execute(
            """
            INSERT INTO app.answerable_topic_versions
                (id, answerable_topic_id, org_id, project_id, version_number, title,
                 answers_question, kind, fallback_rank, base_template_id,
                 content_hash, created_by)
            VALUES (%s, %s, %s, %s, 2, 'Smuggled', 'Whose question is this?', 'kpi', 0,
                    'kpi', %s, 'tester')
            """,
            (_uid("atv"), topic_a, org_a, project_b, "b" * 64),
        )


# ---------------------------------------------------------------------------
# Migration 172 -- the query pin is exact, and `latest` is unstorable.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pin", ["latest", "LATEST", "current"])
def test_latest_is_unstorable_as_a_query_pin(live_postgres, pin):
    """Story 52.2 says a pin is exact. A CHECK is what makes that true."""
    conn = live_postgres
    org_id, project_id = _project(conn)
    topic_id, _v = _topic(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.Error), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.answerable_topic_query_bindings
                (id, answerable_topic_id, org_id, project_id, query_spec_id,
                 query_spec_version_id, role, position, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'headline', 1, 'tester')
            """,
            (_uid("atq"), topic_id, org_id, project_id, _uid("qs"), pin),
        )


def test_a_query_pin_must_name_a_version_that_exists(live_postgres):
    """The foreign key refuses a citation to a version nobody ever wrote."""
    conn = live_postgres
    org_id, project_id = _project(conn)
    topic_id, _v = _topic(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction(), conn.cursor() as cur:  # noqa: E501
        cur.execute(
            """
            INSERT INTO app.answerable_topic_query_bindings
                (id, answerable_topic_id, org_id, project_id, query_spec_id,
                 query_spec_version_id, role, position, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'headline', 1, 'tester')
            """,
            (_uid("atq"), topic_id, org_id, project_id, _uid("qs"), _uid("qsv")),
        )


def test_an_empty_role_is_refused(live_postgres):
    conn = live_postgres
    org_id, project_id = _project(conn)
    topic_id, _v = _topic(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.Error), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.answerable_topic_query_bindings
                (id, answerable_topic_id, org_id, project_id, query_spec_id,
                 query_spec_version_id, role, position, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, '', 1, 'tester')
            """,
            (_uid("atq"), topic_id, org_id, project_id, _uid("qs"), _uid("qsv")),
        )


# ---------------------------------------------------------------------------
# Migration 173 -- a knowledge pin names one kind, completely.
# ---------------------------------------------------------------------------


def test_a_half_filled_knowledge_pin_is_refused(live_postgres):
    """An id without a version names a knowledge item without saying WHICH one."""
    conn = live_postgres
    org_id, project_id = _project(conn)
    topic_id, _v = _topic(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.answerable_topic_knowledge_bindings
                (id, answerable_topic_id, org_id, project_id, knowledge_kind,
                 context_topic_id, context_topic_version, created_by)
            VALUES (%s, %s, %s, %s, 'topic', %s, NULL, 'tester')
            """,
            (_uid("atk"), topic_id, org_id, project_id, _uid("ctx")),
        )


def test_a_pin_cannot_declare_two_kinds_at_once(live_postgres):
    conn = live_postgres
    org_id, project_id = _project(conn)
    topic_id, _v = _topic(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.answerable_topic_knowledge_bindings
                (id, answerable_topic_id, org_id, project_id, knowledge_kind,
                 context_topic_id, context_topic_version, procedure_id,
                 procedure_version, created_by)
            VALUES (%s, %s, %s, %s, 'topic', %s, 1, %s, 1, 'tester')
            """,
            (_uid("atk"), topic_id, org_id, project_id, _uid("ctx"), _uid("proc")),
        )


@pytest.mark.parametrize("kind", ["skill", "schema_doc", ""])
def test_an_unstored_knowledge_kind_is_refused_by_the_database(live_postgres, kind):
    """`skill` is refused HERE too, not only in Python: the spike measured that a
    Skill is a procedure in this system and has no store of its own."""
    conn = live_postgres
    org_id, project_id = _project(conn)
    topic_id, _v = _topic(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction(), conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.answerable_topic_knowledge_bindings
                (id, answerable_topic_id, org_id, project_id, knowledge_kind,
                 context_topic_id, context_topic_version, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, 1, 'tester')
            """,
            (_uid("atk"), topic_id, org_id, project_id, kind, _uid("ctx")),
        )


def test_a_knowledge_pin_must_name_a_version_that_exists(live_postgres):
    conn = live_postgres
    org_id, project_id = _project(conn)
    topic_id, _v = _topic(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction(), conn.cursor() as cur:  # noqa: E501
        cur.execute(
            """
            INSERT INTO app.answerable_topic_knowledge_bindings
                (id, answerable_topic_id, org_id, project_id, knowledge_kind,
                 context_topic_id, context_topic_version, created_by)
            VALUES (%s, %s, %s, %s, 'topic', %s, 7, 'tester')
            """,
            (_uid("atk"), topic_id, org_id, project_id, _uid("ctx")),
        )


def test_requires_knowledge_defaults_to_false(live_postgres):
    """A topic answers from its data unless it SAYS it needs the knowledge."""
    conn = live_postgres
    org_id, project_id = _project(conn)
    topic_id, _v = _topic(conn, org_id, project_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT requires_knowledge FROM app.answerable_topics WHERE id = %s", (topic_id,)
        )
        assert cur.fetchone()[0] is False


# ---------------------------------------------------------------------------
# The floor under the application check: RLS is armed on all three tables.
# ---------------------------------------------------------------------------


def test_row_level_security_is_enabled_and_forced_on_the_three_tables(live_postgres):
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class WHERE relnamespace = 'app'::regnamespace
              AND relname IN ('answerable_topics', 'answerable_topic_versions',
                              'answerable_topic_query_bindings',
                              'answerable_topic_knowledge_bindings')
            ORDER BY relname
            """
        )
        rows = cur.fetchall()

    assert len(rows) == 4, f"expected four tables, got {[r[0] for r in rows]}"
    for name, enabled, forced in rows:
        assert enabled, f"{name}: RLS not enabled"
        assert forced, f"{name}: RLS not FORCED -- the owner would bypass its own floor"
