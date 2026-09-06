"""A manual context event can be created at all -- migration 287.

THE DEFECT, measured on the disposable cluster at ledger head 285 on 2026-08-17,
before any line of this repair existed:

    INSERT INTO app.context_events
        (id, project_id, event_date, type, label, description, created_by, source)
    VALUES ('evt_REPRO_MANUAL', 'proj_EXAMPLE', DATE '2026-08-17',
            'business', 'Price change', '', 'owner@example.com', 'manual');
    -- ERROR 23514: new event observations require a Datastream-owned
    --              Event Configuration version

Migration 133 posed `app.require_event_observation_binding` BEFORE INSERT on
EVERY row of the table, and `context_events._active_event_binding` returns NULL
for `source = 'manual'` by design -- its own comment says "A manual event has no
Connector and stays unbound." The two have contradicted each other ever since:
both writers of a manual annotation (`add_context_event` over MCP and
`POST /api/context-events`) died on that trigger and surfaced it as an opaque
`db_error`. The audit of 2026-08-17 filed those routes as "orphaned, no console
caller"; they were not merely uncalled, they could not have succeeded.

WHY THIS FILE IS SEPARATE from `test_context_events_lifecycle.py`. That module
disables `trg_context_events_require_binding` for its whole run, because it must
stand up UNBOUND CONNECTOR rows, which migration 287 still refuses (and should).
This file exists to prove the trigger itself, so it must run with the trigger
ARMED -- a test of a guard inside a module that turns the guard off proves
nothing. Hence a module of its own, and no autouse fixture from that one.

    cd server && python -m pytest tests/core/test_manual_event_creation_is_possible.py -q
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.conftest import purge_fixture_project

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- an INSERT trigger needs a live Postgres",
)


@pytest.fixture()
def project(live_postgres):
    """One org, one project. No event: creating one is what is under test."""
    conn = live_postgres
    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_evtcr_{suffix}"
    project_id = f"proj_evtcr_{suffix}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, 'active', 'owner@example.com')",
            (org_id, "Manual event creation", org_id.lower()),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', 'owner@example.com')",
            (project_id, org_id, "Manual event creation", project_id.lower()),
        )
    conn.commit()

    yield conn, project_id

    conn.rollback()
    # No foreign key from `app.context_events` to `app.projects` (migration 009
    # states this and why), so purging the project leaves the events behind.
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.context_events WHERE project_id = %s", (project_id,))
    purge_fixture_project(conn, project_id)
    conn.commit()


@_skip_without_dsn
class TestAManualAnnotationCanBeWritten:
    def test_the_writer_the_product_actually_calls_succeeds(self, project):
        """`persist_context_event` is the function BOTH doors call. It died here.

        Not a hand-written INSERT: the defect was that the product's own writer
        could not write, and only calling it proves the repair reaches the doors
        a person and an agent actually use.
        """
        from core.context_events import persist_context_event

        conn, project_id = project
        event_id = persist_context_event(
            project_id=project_id,
            event_date="2026-08-17",
            type="business",
            label="Price increased on the main plan",
            description="Announced to customers the same morning.",
            created_by="owner@example.com",
            conn=conn,
        )
        assert event_id.startswith("evt_")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT source, binding_state, event_configuration_version_id, datastream_id "
                "  FROM app.context_events WHERE id = %s",
                (event_id,),
            )
            source, binding_state, version_id, datastream_id = cur.fetchone()

        assert source == "manual"
        # 'unavailable', never 'linked': migration 133 forced 'linked' on insert,
        # which was only correct while insert required a binding. A manual row
        # carries neither pointer half, and calling it linked would let an
        # annotation claim a Datastream that does not exist.
        assert binding_state == "unavailable"
        assert version_id is None
        assert datastream_id is None

    def test_an_unbound_connector_observation_is_still_refused(self, project):
        """133's invariant is preserved, not traded away.

        The repair is a SCOPE, not an exception: an observation a Connector
        emitted must still arrive bound to the version that produced it, or the
        product could no longer prove which collection an observation came from.
        """
        import psycopg

        conn, project_id = project
        with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation) as raised:
            cur.execute(
                """
                INSERT INTO app.context_events
                    (id, project_id, event_date, type, label, description,
                     created_by, source)
                VALUES (%s, %s, DATE '2026-08-17', 'business', 'Video published',
                        '', 'owner@example.com', 'youtube')
                """,
                (f"evt_{uuid.uuid4().hex[:20]}", project_id),
            )
        assert "Datastream-owned Event Configuration version" in str(raised.value)
        conn.rollback()

    def test_a_manual_row_that_arrives_bound_is_refused_and_not_silently_cleared(self, project):
        """A caller believing something false about its own write is told so.

        Dropping the pointer instead would let a caller keep attributing a
        person's annotation to a Datastream's collection while the database
        quietly disagreed -- the row would look right and mean something else.
        """
        import psycopg

        conn, project_id = project
        with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation) as raised:
            cur.execute(
                """
                INSERT INTO app.context_events
                    (id, project_id, event_date, type, label, description,
                     created_by, source, event_configuration_version_id, datastream_id)
                VALUES (%s, %s, DATE '2026-08-17', 'business', 'Bound manual',
                        '', 'owner@example.com', 'manual', 'ecfgv_x', 'ds_x')
                """,
                (f"evt_{uuid.uuid4().hex[:20]}", project_id),
            )
        assert "names no Event Configuration" in str(raised.value)
        conn.rollback()
