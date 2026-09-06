"""A fixture teardown must survive a governed table it has never heard of (AI-291).

Twenty-five teardowns write their own list of `DELETE`s, and a hand-written list
loses the race. Measured on ONE of them the day it was repaired: two tables added
the same day held the Project by `ON DELETE RESTRICT` -- `project_capabilities`
(migration 243, seeded for every Project) and then `mdm_business_domains`. Each
was invisible until the previous one was removed.

The failure mode is a SKIP, which is why it stays hidden: without a Postgres DSN
these fixtures skip, so nobody sees the teardown break. These tests therefore ask
for a DSN and prove both halves of `purge_fixture_project` -- the fast path that
keeps the cost sane, and the fallback that keeps it correct.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.conftest import TEST_ORG_ID, purge_fixture_project

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- the teardown cannot be exercised",
)

OWNER = "owner@example.com"


def _new_project(cur) -> str:
    project = f"proj_{uuid.uuid4().hex[:16]}"
    cur.execute(
        "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
        "VALUES (%s, %s, 'Purge fixture', %s, 'active', %s)",
        (project, TEST_ORG_ID, f"purge-{uuid.uuid4().hex[:8]}", OWNER),
    )
    return project


def _exists(cur, project: str) -> bool:
    cur.execute("SELECT 1 FROM app.projects WHERE id = %s", (project,))
    return cur.fetchone() is not None


def test_a_plain_project_leaves_by_the_fast_path() -> None:
    """The common case: one statement, and the CASCADE edges do the rest."""
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            project = _new_project(cur)
            assert _exists(cur, project)
        purge_fixture_project(conn, project)
        with conn.cursor() as cur:
            assert not _exists(cur, project)
        conn.commit()


def test_a_blocking_child_the_teardown_never_heard_of_still_lets_the_project_go() -> None:
    """The case that breaks a hand-written list, and the reason for the fallback.

    `app.project_capabilities` references the Project by ON DELETE RESTRICT and
    is seeded for every Project, so it is exactly the shape migration 243
    introduced. Nothing here deletes it by name: the fallback walks the same
    foreign-key graph production walks, so the row goes because the graph says
    it must, not because this test remembered it.
    """
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            project = _new_project(cur)
            cur.execute(
                "SELECT count(*) FROM app.project_capabilities WHERE project_id = %s",
                (project,),
            )
            blocking = cur.fetchone()[0]
        # The premise of the test, asserted rather than assumed: if the seeder
        # ever stops running, this test would pass for the wrong reason.
        assert blocking > 0, (
            "app.project_capabilities is no longer seeded for a new Project -- "
            "this test no longer exercises the blocking case it exists for"
        )

        purge_fixture_project(conn, project)

        with conn.cursor() as cur:
            assert not _exists(cur, project)
            cur.execute(
                "SELECT count(*) FROM app.project_capabilities WHERE project_id = %s",
                (project,),
            )
            assert cur.fetchone()[0] == 0
        conn.commit()
