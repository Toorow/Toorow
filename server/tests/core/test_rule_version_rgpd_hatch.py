"""Story 60.5 -- the two ledgers of migration 242 do not block an org erasure.

WHY THIS FILE EXISTS AT ALL. A new append-only ledger with a protective DELETE
trigger blocks `DELETE FROM app.organizations` unless the trigger carries the
escape hatch migrations 098/099 established:
``current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on'``.
`grep -rln "rgpd_erasure" server/tests` found eight files and NOT ONE of them
demands the hatch of a trigger written after them, so a migration that forgot it
would ship and be discovered by a client's right-to-erasure request. This file is
that missing demand, for the two triggers 242 adds.

It asserts two different things and neither substitutes for the other:

  1. the trigger definitions carry the hatch -- a schema fact, readable without
     erasing anything;
  2. a real erasure really removes the version rows -- the behaviour, run through
     `core.org_purge.purge_org_tree` and the final DELETE, which is the pair
     `admin_api` performs.
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import cleanup_rules as rules  # noqa: E402
from core import value_mapping_tables as tables  # noqa: E402

IDENTITY = "owner@example.com"

LEDGER_TABLES = ("value_mapping_table_versions", "cleanup_rule_versions")


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pg_available
def test_both_immutability_triggers_carry_the_erasure_hatch():
    """The schema half. A trigger without the WHEN clause is a locked tenant."""
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname, pg_get_triggerdef(t.oid) ILIKE '%%rgpd_erasure%%'
                  FROM pg_trigger t
                  JOIN pg_class c ON c.oid = t.tgrelid
                 WHERE c.relnamespace = 'app'::regnamespace
                   AND NOT t.tgisinternal
                   AND (t.tgtype & 8) > 0                     -- fires on DELETE
                   AND c.relname = ANY (%s)
                """,
                (list(LEDGER_TABLES),),
            )
            guards = dict(cur.fetchall())

    assert set(guards) == set(LEDGER_TABLES), guards
    assert all(guards.values()), guards


@pg_available
def test_an_org_erasure_removes_every_recorded_version():
    """The behaviour half, through the eraser the endpoint actually calls.

    Without the hatch this test does not fail with a wrong count -- it raises,
    which is precisely how a forgotten `WHEN` clause presents itself in
    production: not as missing data, but as a tenant nobody can delete.
    """
    from core.db import get_connection
    from core.org_purge import purge_org_tree

    org_id, project_id = _uid("org"), _uid("proj")
    datastream_id = _uid("ds")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 60.5 erasure fixture', %s, 'active', %s)",
                (org_id, org_id.replace("_", "-"), IDENTITY),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 60.5 erasure fixture', %s, %s)",
                (project_id, org_id, project_id.replace("_", "-"), IDENTITY),
            )
            cur.execute(
                "INSERT INTO app.datastreams "
                "(id, project_id, org_id, name, module_name, enabled) "
                "VALUES (%s, %s, %s, 'Story 60.5 erasure stream', 'example_connector', TRUE)",
                (datastream_id, project_id, org_id),
            )
        conn.commit()

        table = tables.create_table(
            conn,
            org_id=org_id,
            project_id=project_id,
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity=IDENTITY,
        )
        entry = tables.add_entry(
            conn,
            table_id=table["id"],
            org_id=org_id,
            source_value="raw-value-1",
            canonical_value="Line A",
            identity=IDENTITY,
        )
        tables.update_entry(
            conn,
            table_id=table["id"],
            org_id=org_id,
            entry_id=entry["id"],
            canonical_value="Line B",
            identity=IDENTITY,
        )
        rule = rules.create_rule(
            conn,
            org_id=org_id,
            project_id=project_id,
            datastream_id=None,
            name="Drop test campaigns",
            source_field="campaign_name",
            rule_kind="exclude_row",
            pattern="_TEST_",
            identity=IDENTITY,
            dry_run_state="not_attempted",
        )
        rules.update_rule(
            conn,
            rule_id=rule["id"],
            project_id=project_id,
            identity=IDENTITY,
            pattern="_TEST_|_STAGING_",
        )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.value_mapping_table_versions WHERE org_id = %s",
                (org_id,),
            )
            table_versions = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM app.cleanup_rule_versions WHERE org_id = %s", (org_id,)
            )
            rule_versions = cur.fetchone()[0]
        assert table_versions == 3, "creation, the added pair, the edited pair"
        assert rule_versions == 2, "creation and the edited pattern"

        # The pair the endpoint performs: the tree, then the org row itself.
        purge_org_tree(conn, org_id)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        conn.commit()

        with conn.cursor() as cur:
            for relation in LEDGER_TABLES:
                cur.execute(
                    f"SELECT count(*) FROM app.{relation} WHERE org_id = %s",  # noqa: S608 -- LEDGER_TABLES is a literal tuple of this module
                    (org_id,),
                )
                assert cur.fetchone()[0] == 0, relation
            cur.execute("SELECT count(*) FROM app.organizations WHERE id = %s", (org_id,))
            assert cur.fetchone()[0] == 0
