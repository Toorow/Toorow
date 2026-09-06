"""What migration 243 changes about a real row, measured against a real Postgres.

This is the only test that can prove the failure mode story 61.5 exists to close,
because the failure is a statement that touches nothing and says nothing:

    grep -rn "INSERT INTO app.project_capabilities" server/core   # no match

The whole activation path is UPDATE. `prepare_change_set` sets
`pending_change_set_id`, `confirm_change_set` sets `state` and
`active_version_id`, and neither reads `cur.rowcount` for the capability -- only
for the Change Set row itself. So on a Project whose sixth row does not exist,
turning Placement Mapping on updates zero rows, raises nothing, mints its
Configuration Version and reports success. A mocked cursor cannot see that: it
returns whatever rowcount it was told to.
"""

from __future__ import annotations

import ulid
from core.project_capability_states import PROJECT_CAPABILITY_KEYS

from tests.conftest import purge_fixture_project

# The exact statement `confirm_change_set` runs for each capability it advances
# (`server/core/project_settings.py`, the confirm mutation). Copied here rather
# than imported because what is under test is the row count it produces, and the
# function that issues it does not return one.
CONFIRM_CAPABILITY_UPDATE = """
    UPDATE app.project_capabilities
    SET state = %s,
        active_version_id = %s,
        pending_change_set_id = NULL,
        updated_at = NOW()
    WHERE project_id = %s AND capability_key = %s
"""


def _project_at_five(conn, org_id: str) -> str:
    """A Project in the state migration 243 finds: five rows, no sixth.

    Built by deleting the row the seed now writes rather than by disabling the
    trigger, so the test needs no ownership and the other five rows keep the
    exact states the control plane gave them.
    """
    suffix = str(ulid.ULID())
    project_id = f"proj_test_pm_{suffix}"[:40]
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, 'Sixth capability fixture', %s, 'active', 'system')",
            (project_id, org_id, f"sixth-capability-{suffix}"[:60]),
        )
        cur.execute(
            "DELETE FROM app.project_capabilities "
            "WHERE project_id = %s AND capability_key = 'placement_mapping'",
            (project_id,),
        )
    conn.commit()
    return project_id


def _drop_project(conn, project_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.project_capabilities WHERE project_id = %s", (project_id,))
        # AI-291: le graphe prend le relais si une table gouvernee
        # ajoutee depuis retient le projet en ON DELETE RESTRICT.
        purge_fixture_project(cur.connection, project_id)
    conn.commit()


def test_activation_on_a_missing_row_touches_nothing_and_the_reprise_is_what_fixes_it(
    pg_conn, test_org
) -> None:
    project_id = _project_at_five(pg_conn, test_org)
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT capability_key FROM app.project_capabilities "
                "WHERE project_id = %s ORDER BY capability_key",
                (project_id,),
            )
            before = [row[0] for row in cur.fetchall()]
            assert "placement_mapping" not in before
            # DERIVED: the fixture removes exactly ONE row, whatever the number
            # of declared capabilities is. A literal `5` was exact while six
            # existed and became a false failure the day a seventh was declared
            # -- and it would have said the missing-row defect had come back.
            assert len(before) == len(PROJECT_CAPABILITY_KEYS) - 1

            # THE SILENCE. No exception, no row, and a caller that only reads the
            # Change Set's own rowcount would report this change as applied.
            cur.execute(
                CONFIRM_CAPABILITY_UPDATE,
                ("ready", None, project_id, "placement_mapping"),
            )
            assert cur.rowcount == 0

            # THE REPRISE, called exactly as migration 243 calls it.
            cur.execute("SELECT app.seed_project_capabilities(%s)", (project_id,))
            cur.execute(
                "SELECT capability_key, availability, state FROM app.project_capabilities "
                "WHERE project_id = %s ORDER BY capability_key",
                (project_id,),
            )
            rows = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
            assert set(rows) == set(PROJECT_CAPABILITY_KEYS)
            # `optional` and `disabled`: an absence is not a decision, so the row
            # is born off rather than born on.
            assert rows["placement_mapping"] == ("optional", "disabled")
            # ON CONFLICT DO NOTHING: the rows that existed are not reset.
            assert rows["currency_fx"][1] == "draft"

            cur.execute(
                CONFIRM_CAPABILITY_UPDATE,
                ("ready", None, project_id, "placement_mapping"),
            )
            assert cur.rowcount == 1
        pg_conn.commit()
    finally:
        _drop_project(pg_conn, project_id)


def test_the_database_refuses_the_sixth_capability_as_always_present(pg_conn, test_org) -> None:
    """`optional` is not a Python opinion: the pairing CHECK holds it.

    `project-settings.md`, ratified 2026-08-05: "Placement Mapping | Optional and
    Disabled by default". Migration 243 puts the key in the `optional` member of
    `project_capabilities_check1`, so `always_present` -- which would remove the
    switch from every screen and forbid the disabled posture -- is unwritable.
    """
    import psycopg

    project_id = _project_at_five(pg_conn, test_org)
    try:
        with pg_conn.cursor() as cur:
            try:
                cur.execute(
                    "INSERT INTO app.project_capabilities "
                    "(project_id, capability_key, availability, state) "
                    "VALUES (%s, 'placement_mapping', 'always_present', 'draft')",
                    (project_id,),
                )
            except psycopg.errors.CheckViolation as exc:
                assert "project_capabilities_check1" in str(exc)
            else:  # pragma: no cover -- the CHECK is missing
                raise AssertionError("the pairing CHECK accepted always_present")
        pg_conn.rollback()
    finally:
        _drop_project(pg_conn, project_id)
