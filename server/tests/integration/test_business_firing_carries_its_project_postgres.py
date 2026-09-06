"""Live-Postgres proof that a business firing carries the project it is about.

`test_business_alerts.py` proves what `evaluate_business_alerts` DECIDES: its
cursor is a fake that accepts whatever SQL it is handed, so an INSERT that
dropped two columns passed every one of those tests for as long as they existed.
It did drop two: `project_id` and `metric` were never in the column list, so
every business firing took the column DEFAULTS of migration 013 --
`project_id = 'default'` and `metric = ''`.

That was invisible from the one reader anybody looked at.
`fetch_recent_alert_firings` JOINs `app.alert_definitions` and reads the project
and the metric OFF THE DEFINITION, so it answered correctly about rows that were
filed wrong. The readers that trust the row did not:

* `alert_destinations` joins destinations on `d.project_id = f.project_id`, so a
  breach of project X only ever routed to a destination declared on a project
  called `default` -- a customer who configured their own email destination
  received none of their own business alerts;
* `alert_destinations.firing_count_since(project)` -- the number the empty state
  shows -- answered 0 for a project whose firings were all filed elsewhere.

Only a real database can tell those two apart, because the difference is a
column value, not a code path. This file writes one firing through the real
evaluator and asks the project-scoped readers whether they can see it.

The evaluator COMMITS on a connection of its own, so `live_postgres`' rollback
cannot undo it: every row this module creates is deleted by name in a `finally`.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

import pytest

FIXTURE_AUTHOR = "owner@example.com"


@contextmanager
def _second_connection():
    """A connection to the same test database, independent of `live_postgres`.

    The evaluator opens its own connection through `core.db.get_connection`; in a
    run that has a live Postgres but a `PLATFORM_DB_URL` pointing elsewhere, that
    would evaluate against the wrong database. Patching it is what pins the proof
    to the database this test set up.
    """
    import psycopg  # noqa: PLC0415

    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=5)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def definition(live_postgres, test_org):
    """One enabled alert definition on a project of its own, committed."""
    conn = live_postgres
    suffix = uuid.uuid4().hex[:12]
    project_id = f"proj_ba_{suffix}"
    definition_id = f"alrt_ba_{suffix}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (project_id, test_org, "Business alert scope", project_id.lower(), FIXTURE_AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.alert_definitions "
            "(id, project_id, metric, operator, threshold, enabled, created_by) "
            "VALUES (%s, %s, 'cost', '>', 100.0, TRUE, %s)",
            (definition_id, project_id, FIXTURE_AUTHOR),
        )
    conn.commit()
    try:
        yield project_id, definition_id
    finally:
        # `purge_fixture_project` rather than a hand-written DELETE list: AI-291
        # measured 25 teardowns each keeping their own, and a hand-written list
        # loses the race with the next migration that hangs a table off projects.
        from tests.conftest import purge_fixture_project  # noqa: PLC0415

        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.alert_firings WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.alert_definitions WHERE id = %s", (definition_id,))
        conn.commit()
        purge_fixture_project(conn, project_id)


def _evaluate(project_id: str, window: date) -> list[dict]:
    from core import business_alerts  # noqa: PLC0415

    with (
        patch("core.db.get_connection", _second_connection),
        patch.object(business_alerts, "_get_semantic_metrics", return_value=set()),
        patch.object(
            business_alerts, "_query_additive_metric", return_value=(150.0, ["pull_A"])
        ),
        patch.dict(os.environ, {"BUSINESS_ALERTS_ENABLED": "true"}),
    ):
        return business_alerts.evaluate_business_alerts(
            evaluation_date=window, project_id=project_id
        )


def test_the_firing_row_names_the_project_and_the_metric(live_postgres, definition):
    project_id, definition_id = definition
    window = date(2026, 7, 10)

    firings = _evaluate(project_id, window)
    assert len(firings) == 1, firings

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT project_id, metric FROM app.alert_firings WHERE definition_id = %s",
            (definition_id,),
        )
        rows = cur.fetchall()

    assert rows == [(project_id, "cost")], (
        "the firing must be filed under its own project and metric, not under the "
        "'default'/'' column defaults of migration 013"
    )


def test_the_project_scoped_count_can_see_its_own_firing(live_postgres, definition):
    """The number the empty state shows, asked of the project that fired."""
    from core.alert_destinations import firing_count_since  # noqa: PLC0415

    project_id, _definition_id = definition
    before = firing_count_since(live_postgres, project_id, hours=24)

    _evaluate(project_id, date(2026, 7, 10))

    assert firing_count_since(live_postgres, project_id, hours=24) == before + 1


def test_the_project_destination_is_reachable_from_the_firing(live_postgres, definition):
    """The delivery join is `d.project_id = f.project_id` -- prove it now meets.

    This is the whole cost of the defect: a destination declared on the project
    that breached could never be paired with the breach, because the two sides of
    that equality named different projects.
    """
    project_id, definition_id = definition
    destination_id = f"adst_{uuid.uuid4().hex[:12]}"
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.alert_destinations "
            "(id, project_id, kind, label, target, alert_types, enabled) "
            "VALUES (%s, %s, 'email', 'Ops', %s, '{}', TRUE)",
            (destination_id, project_id, FIXTURE_AUTHOR),
        )
    live_postgres.commit()
    try:
        _evaluate(project_id, date(2026, 7, 10))

        with live_postgres.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM app.alert_firings f
                JOIN app.alert_destinations d
                  ON d.project_id = f.project_id AND d.enabled
                WHERE f.definition_id = %s AND d.id = %s
                """,
                (definition_id, destination_id),
            )
            paired = cur.fetchone()[0]
    finally:
        with live_postgres.cursor() as cur:
            cur.execute("DELETE FROM app.alert_destinations WHERE id = %s", (destination_id,))
        live_postgres.commit()

    assert paired == 1, "the breach and the destination of the same project must pair"
