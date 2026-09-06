"""The index itself, against a real Postgres -- AI-226, migration 229.

The Python half (`test_dq_firing_dedup.py`) proves the writer resolves the subject
and yields on a known fact. Neither of those means anything if the index does not
exist or does not cover what it claims: a mocked cursor accepts every INSERT.

What must hold, and what must NOT:
  * the same finding, re-observed, produces ONE row -- that is the whole point;
  * two Datastreams of the same project, same day, stay TWO findings -- suppressing
    those would hide 589 real ones to remove 1831 duplicates;
  * an infra event (no Datastream) is never touched by this index;
  * a DQ firing whose subject could not be resolved is still written.
"""

from __future__ import annotations

import os
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("TEST_POSTGRES_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_POSTGRES_DSN not set -- skipping live Postgres test"
)

_INSERT = """
    INSERT INTO app.alert_firings
        (id, definition_id, type, project_id, metric, fired_at, observed_value,
         threshold, pull_ids, window_date, severity, message, datastream_id)
    VALUES (%s, NULL, %s, %s, 'timeliness', NOW(), 0, 0, '{}', %s, 'warning', '', %s)
    ON CONFLICT DO NOTHING
    RETURNING id
"""


@pytest.fixture
def conn():
    with psycopg.connect(DSN) as connection:
        yield connection
        connection.rollback()


def _fire(conn, *, kind, project, day, datastream):
    with conn.cursor() as cur:
        cur.execute(
            _INSERT,
            (f"fire_{uuid.uuid4().hex[:20]}", kind, project, day, datastream),
        )
        return cur.fetchone() is not None


@pytest.fixture
def project(conn):
    """A project nothing else uses, so the counts below are this test's.

    Seeded rather than invented: `alert_firings.project_id` carries
    `fk_alert_firings_project`, and a fixture that ignored it would prove the
    index against rows the schema refuses. Rolled back with the connection.
    """
    suffix = uuid.uuid4().hex[:12]
    org_id, project_id = f"org_{suffix}", f"proj_{suffix}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'owner@example.com')",
            (org_id, f"Org {suffix}", f"org-{suffix}"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'owner@example.com', %s)",
            (project_id, f"Project {suffix}", f"project-{suffix}", org_id),
        )
    return project_id


def test_the_index_exists_and_is_partial(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = 'app' AND indexname = 'alert_firings_dedup_dq'"
        )
        row = cur.fetchone()
    assert row, "migration 229 was not applied to this database"
    definition = row[0]
    assert "datastream_id" in definition
    assert "WHERE" in definition, "a total index would collapse the infra events too"


def test_the_same_finding_restated_is_one_row(conn, project):
    assert _fire(conn, kind="dq_timeliness", project=project, day="2026-08-04", datastream="flux_A")
    # Ninety-six ticks a day observed the same fact; it is stated once.
    for _ in range(5):
        assert not _fire(
            conn, kind="dq_timeliness", project=project, day="2026-08-04", datastream="flux_A"
        )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.alert_firings WHERE project_id = %s", (project,)
        )
        assert cur.fetchone()[0] == 1


def test_two_datastreams_two_findings(conn, project):
    assert _fire(conn, kind="dq_timeliness", project=project, day="2026-08-04", datastream="flux_A")
    assert _fire(conn, kind="dq_timeliness", project=project, day="2026-08-04", datastream="flux_B")


def test_two_days_two_findings(conn, project):
    assert _fire(conn, kind="dq_timeliness", project=project, day="2026-08-04", datastream="flux_A")
    assert _fire(conn, kind="dq_timeliness", project=project, day="2026-08-05", datastream="flux_A")


def test_two_kinds_two_findings(conn, project):
    """A stale Datastream that is ALSO returning nothing has two things wrong."""
    assert _fire(conn, kind="dq_timeliness", project=project, day="2026-08-04", datastream="flux_A")
    assert _fire(conn, kind="dq_volume", project=project, day="2026-08-04", datastream="flux_A")


def test_an_unresolved_subject_is_still_written_every_time(conn, project):
    """An alert refused for want of provenance is an alert deleted by its metadata."""
    assert _fire(conn, kind="dq_timeliness", project=project, day="2026-08-04", datastream=None)
    assert _fire(conn, kind="dq_timeliness", project=project, day="2026-08-04", datastream=None)


def test_an_infra_event_is_outside_this_index(conn, project):
    """`meta_alert` has no Datastream and no daily identity; 229 does not touch it."""
    assert _fire(conn, kind="meta_alert", project=project, day="2026-08-04", datastream=None)
    assert _fire(conn, kind="meta_alert", project=project, day="2026-08-04", datastream=None)
