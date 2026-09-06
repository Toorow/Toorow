"""The role guard proves the pair on real rows -- AI-219.

WHY A LIVE DATABASE AND NOT A DOUBLE. The hole was invisible to every mocked
test that existed, and it had to be: a fake cursor answers whatever the reader
asks it, so a guard that authorizes project A and then reads a stream of project
B is indistinguishable from one that refuses. Only a SECOND project, holding a
SECOND stream, in the same database, can tell them apart.

The model is 63.2's `test_a_flux_read_under_the_wrong_project_discloses_nothing`,
one file over: two owners, one read across, and the refusal must be the SAME
answer an absent stream gets -- otherwise comparing two refusals teaches the
caller that somebody else's stream exists.

WHAT THIS FILE DOES NOT PROVE. It exercises the guard, not the 43 handlers that
call it. That the handlers call it, and that no new reader forgets to, is derived
in `tests/conformance/test_datastream_readers_carry_project_scope.py`.
"""

from __future__ import annotations

import os
import uuid

import pytest

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres scope test skipped",
)


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _seed_owner(cur, identity: str) -> dict:
    """One organization the identity belongs to, holding one project, one stream."""
    org_id = _id("org_")
    project_id = _id("proj_")
    ds_id = _id("ds_")
    cur.execute(
        """
        INSERT INTO app.organizations (id, name, slug, created_by, status)
        VALUES (%s, %s, %s, 'ai-219-test', 'active')
        """,
        (org_id, org_id, org_id),
    )
    cur.execute(
        """
        INSERT INTO app.org_members (id, org_id, identity, role, status)
        VALUES (%s, %s, %s, 'owner', 'active')
        """,
        (_id("mem_"), org_id, identity),
    )
    cur.execute(
        """
        INSERT INTO app.projects (id, name, slug, created_by, org_id, status)
        VALUES (%s, %s, %s, 'ai-219-test', %s, 'active')
        """,
        (project_id, project_id, project_id, org_id),
    )
    cur.execute(
        """
        INSERT INTO app.datastreams
            (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
        VALUES (%s, %s, 'DS', 'generic', 'connector_pull', FALSE, 'ai-219-test', %s)
        """,
        (ds_id, project_id, org_id),
    )
    return {"org_id": org_id, "project_id": project_id, "ds_id": ds_id}


@pytest.fixture
def two_owners(live_postgres, monkeypatch):
    """Two organizations, one member each, one Datastream each -- rolled back after.

    `TOOROW_AUTH_MODE` must be armed: under `disabled`,
    `resolve_strict_resource_access` denies everything and the guard would refuse
    for a reason that has nothing to do with the pair.
    """
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    conn = live_postgres
    with conn.cursor() as cur:
        first = _seed_owner(cur, "first@example.com")
        second = _seed_owner(cur, "second@example.com")
    try:
        yield conn, first, second
    finally:
        conn.rollback()


@_skip_without_dsn
def test_a_member_reaches_the_stream_of_the_project_they_named(two_owners) -> None:
    from core.admin_api import _require_datastream_role

    conn, first, _second = two_owners

    refusal = _require_datastream_role(
        first["project_id"],
        "first@example.com",
        "viewer",
        conn,
        datastream_id=first["ds_id"],
    )

    assert refusal is None


@_skip_without_dsn
def test_a_stream_of_another_project_is_refused_to_an_authorized_member(
    two_owners,
) -> None:
    """The identity IS authorized on the project it named -- and still refused.

    This is the whole of AI-219 in one call: before the repair, the role check
    passed, the `datastream_id` went to the audit metadata, and the handler read
    somebody else's stream on the strength of a project the caller did own.
    """
    from core.admin_api import _require_datastream_role

    conn, first, second = two_owners

    refusal = _require_datastream_role(
        first["project_id"],
        "first@example.com",
        "viewer",
        conn,
        datastream_id=second["ds_id"],
    )

    assert refusal is not None
    assert refusal.status_code == 404


@_skip_without_dsn
def test_the_foreign_stream_and_the_absent_stream_answer_identically(
    two_owners,
) -> None:
    """Two refusals that differ are an enumeration oracle for stream ids."""
    from core.admin_api import _require_datastream_role

    conn, first, second = two_owners

    foreign = _require_datastream_role(
        first["project_id"], "first@example.com", "viewer", conn,
        datastream_id=second["ds_id"],
    )
    absent = _require_datastream_role(
        first["project_id"], "first@example.com", "viewer", conn,
        datastream_id="ds_000000000000",
    )
    unauthorized = _require_datastream_role(
        second["project_id"], "first@example.com", "viewer", conn,
        datastream_id=second["ds_id"],
    )

    assert foreign is not None and absent is not None and unauthorized is not None
    assert foreign.status_code == absent.status_code == unauthorized.status_code == 404
    assert foreign.body == absent.body == unauthorized.body


@_skip_without_dsn
def test_a_project_level_call_without_a_stream_is_unaffected(two_owners) -> None:
    """List and create name no stream yet; demanding one would break them."""
    from core.admin_api import _require_datastream_role

    conn, first, _second = two_owners

    assert (
        _require_datastream_role(
            first["project_id"], "first@example.com", "viewer", conn
        )
        is None
    )
