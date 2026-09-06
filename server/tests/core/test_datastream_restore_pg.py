"""Restoring a Datastream undoes the archive, and nothing more -- 2026-08-18.

WHY THIS FILE EXISTS. `DELETE /api/datastreams/{id}` has soft-archived since
story 21.5, and three places tell a person the way back is to RESTORE it:
`SchedulePanel`'s `archived` run state ("Restoring it is what makes it runnable
again"), `schedule_mcp.ARCHIVED_CANNOT_RUN`, and `datastream_dispatch`'s own
refusal ("Restore it before collecting anything for it"). Nothing restored
anything. An archive was terminal in practice while every surface described it
as reversible.

AND WHY IT IS PG-GATED RATHER THAN MOCKED. Two of the three facts below are
enforced by the DATABASE and by nothing in Python:

  * `uq_datastreams_project_name_live` (migration 256) is a PARTIAL unique index
    -- unique over live rows only. Restoring re-enters that set, so a name reused
    by a rebuild is a collision that exists only against a real index. A mocked
    cursor would accept the UPDATE and prove the opposite of the truth;
  * `archived_at` is what the archive writes and `lifecycle_state` is what it
    leaves alone. A double asserting the columns it was told to expect cannot
    catch a restore that writes the wrong one.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("live_postgres")


def _seed(cur, *, ds_id, project_id, org_id, name, archived=False):
    """One Datastream, live or archived exactly as `delete_datastream` leaves it."""
    cur.execute(
        "INSERT INTO app.datastreams "
        "(id, project_id, org_id, name, created_by, source_kind, enabled, "
        " schedule_mode, refetch_days, date_window_days, lifecycle_state, "
        " archived_at, archived_by, config) "
        # `managed_feed` and not `connector_pull`: `ck_datastreams_source_kind`
        # requires a `module_name` for a pull, which would drag a module
        # registration into a fixture about the archive. The restore reads
        # neither column, so the cheapest legal source kind is the honest one.
        "VALUES (%s, %s, %s, %s, 'system', 'managed_feed', %s, "
        "        'nightly', 3, 30, 'active', %s, %s, %s::jsonb)",
        (
            ds_id,
            project_id,
            org_id,
            name,
            # THE SHAPE THE SOFT ARCHIVE REALLY LEAVES. `enabled = FALSE`,
            # `archived_at` stamped, `config.archived` raised -- and
            # `lifecycle_state` still `active`, because `delete_datastream` never
            # touches it. Seeding `'archived'` here would be seeding a row the
            # product does not produce.
            not archived,
            "2026-08-14T10:00:00+00:00" if archived else None,
            "owner@example.com" if archived else None,
            '{"archived": true}' if archived else "{}",
        ),
    )


@pytest.fixture
def restore_scope(pg_conn, test_org):
    """A project of its own, so a name collision here collides with nothing else."""
    import ulid as _ulid

    suffix = str(_ulid.ULID())
    project_id = f"proj_test_rst_{suffix}"[:40]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, 'Restore pg fixture', %s, 'active', 'system') "
            "ON CONFLICT (id) DO NOTHING",
            (project_id, test_org, f"restore-pg-{suffix}"[:60]),
        )
    pg_conn.commit()
    return {"project_id": project_id, "org_id": test_org, "suffix": suffix}


def test_a_restore_clears_the_archive_and_leaves_the_clock_off(pg_conn, restore_scope):
    """The inverse of the archive, and deliberately not the inverse of stopping.

    Restoring answers "this Datastream exists again". Starting it answers "and it
    collects from now on" -- a provider call, a quota, a bill. They are two
    consents, so the restore must NOT re-arm: `PUT …/schedule` is the single
    writer of the armed flag, and a second route setting `enabled` would be the
    second activation authority the Workbench surface forbids by name.
    """
    from core.datastreams import restore_datastream

    project_id, org_id = restore_scope["project_id"], restore_scope["org_id"]
    ds_id = f"ds_test_rst_{restore_scope['suffix']}"[:40]
    with pg_conn.cursor() as cur:
        _seed(cur, ds_id=ds_id, project_id=project_id, org_id=org_id,
              name="Archived orders", archived=True)
    pg_conn.commit()

    restored = restore_datastream(ds_id, project_id, pg_conn)
    pg_conn.commit()

    assert restored is not None
    # The three things the archive wrote are gone...
    assert restored["archived_at"] is None
    assert restored["archived_by"] is None
    assert (restored["config"] or {}).get("archived") is None
    # ...and the one it also wrote is deliberately still there.
    assert restored["enabled"] is False

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT enabled, lifecycle_state, archived_at FROM app.datastreams "
            "WHERE id = %s AND project_id = %s",
            (ds_id, project_id),
        )
        enabled, lifecycle, archived_at = cur.fetchone()
    assert (enabled, archived_at) == (False, None)
    # `lifecycle_state` IS NOT TOUCHED, because the archive did not touch it. A
    # Datastream archived while it was a `draft` must come back a draft; writing
    # `'active'` here would ACTIVATE something that was never published.
    assert lifecycle == "active"


def test_restoring_something_that_is_not_archived_is_refused(pg_conn, restore_scope):
    """A restore is not idempotent, and that is the point.

    Answering "done" to a gesture that did nothing tells a person their click
    landed. `core.context_store.restore_topic` refuses the same way for the same
    reason -- this is that rule applied to the Datastream.
    """
    from core.datastreams import NotArchivedError, restore_datastream

    project_id, org_id = restore_scope["project_id"], restore_scope["org_id"]
    ds_id = f"ds_test_liv_{restore_scope['suffix']}"[:40]
    with pg_conn.cursor() as cur:
        _seed(cur, ds_id=ds_id, project_id=project_id, org_id=org_id,
              name="Live orders", archived=False)
    pg_conn.commit()

    with pytest.raises(NotArchivedError):
        restore_datastream(ds_id, project_id, pg_conn)


def test_a_rebuilt_name_refuses_the_restore_and_names_the_gesture(pg_conn, restore_scope):
    """The cost of migration 256, paid where it lands.

    That migration made the name index PARTIAL so an archived Datastream stops
    reserving its name and the same feed can be rebuilt under the name it
    deserves. The consequence is exactly this: rebuilding it is what makes the
    archived one's name unavailable. The refusal has to be a named one, or the
    restore surfaces as an opaque `500` on a partial index nobody can see.
    """
    from core.datastreams import NameTakenError, restore_datastream

    project_id, org_id = restore_scope["project_id"], restore_scope["org_id"]
    suffix = restore_scope["suffix"]
    archived_id = f"ds_test_arc_{suffix}"[:40]
    rebuilt_id = f"ds_test_reb_{suffix}"[:40]
    with pg_conn.cursor() as cur:
        _seed(cur, ds_id=archived_id, project_id=project_id, org_id=org_id,
              name="Daily spend", archived=True)
        # The rebuild the migration exists to permit -- same name, live.
        _seed(cur, ds_id=rebuilt_id, project_id=project_id, org_id=org_id,
              name="Daily spend", archived=False)
    pg_conn.commit()

    with pytest.raises(NameTakenError) as refusal:
        restore_datastream(archived_id, project_id, pg_conn)
    assert refusal.value.name == "Daily spend"

    # AND THE CONNECTION SURVIVES THE REFUSAL. The UPDATE runs inside a savepoint
    # for this reason: without one, the unique violation aborts the caller's
    # transaction and the route's next statement fails for a reason that has
    # nothing to do with what the person asked.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_two_archived_rows_may_share_a_name_and_both_restore_one_at_a_time(
    pg_conn, restore_scope
):
    """History repeats -- migration 256's own words -- and the index allows it.

    Guarded because it is the premise of the refusal above: if archived rows
    could NOT share a name, the collision would be impossible and the previous
    test would be proving nothing.
    """
    from core.datastreams import restore_datastream

    project_id, org_id = restore_scope["project_id"], restore_scope["org_id"]
    suffix = restore_scope["suffix"]
    first, second = f"ds_test_h1_{suffix}"[:40], f"ds_test_h2_{suffix}"[:40]
    with pg_conn.cursor() as cur:
        _seed(cur, ds_id=first, project_id=project_id, org_id=org_id,
              name="Weekly reach", archived=True)
        _seed(cur, ds_id=second, project_id=project_id, org_id=org_id,
              name="Weekly reach", archived=True)
    pg_conn.commit()

    assert restore_datastream(first, project_id, pg_conn) is not None
    pg_conn.commit()

    # The second one now collides with the first, which has become live.
    from core.datastreams import NameTakenError

    with pytest.raises(NameTakenError):
        restore_datastream(second, project_id, pg_conn)


def test_a_restore_is_scoped_to_its_project(pg_conn, restore_scope):
    """AD-5. Another project's id answers None, never someone else's row."""
    from core.datastreams import restore_datastream

    project_id, org_id = restore_scope["project_id"], restore_scope["org_id"]
    ds_id = f"ds_test_scp_{restore_scope['suffix']}"[:40]
    with pg_conn.cursor() as cur:
        _seed(cur, ds_id=ds_id, project_id=project_id, org_id=org_id,
              name="Scoped stream", archived=True)
    pg_conn.commit()

    assert restore_datastream(ds_id, "proj_SOMEONE_ELSE", pg_conn) is None
