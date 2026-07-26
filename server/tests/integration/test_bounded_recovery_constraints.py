"""Live-Postgres contract test for the bounded-recovery kind widening (Story 12.11).

Migration 080 is PURELY additive: it widens the migration-070
``app.operation_preparations.kind`` CHECK from {retry, refetch, reconcile} to also
admit {synchronize, reload, reprocess}. These tests apply the dependency chain
(035 organizations, 023 datastreams, 030 intents, 032 mappings, 042 registry, 060
operations/audit/outbox, 070 operation preparations, then 080) idempotently and
assert the REAL constraint a mocked cursor cannot catch:

  * BEFORE-080 semantics preserved: the 36.13 verbs still insert;
  * AFTER-080: the three Story 12.11 named verbs (synchronize/reload/reprocess)
    insert cleanly;
  * a NON-verb (e.g. 'publish') is STILL rejected by the widened CHECK (the domain
    did not become open-ended);
  * re-applying 080 is a no-op (idempotent).

They SKIP when TEST_POSTGRES_DSN is unset. The migration is applied to Supabase only
under Jean's authorization; these tests apply it to the disposable test database.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"

# The minimal dependency chain the operation_preparations FKs require.
_CHAIN = [
    "035_organizations.sql",
    "023_datastreams.sql",
    "030_versioned_datastream_intents.sql",
    "032_datastream_field_mappings.sql",
    "042_datastream_candidate_registry.sql",
    "060_operation_audit_outbox.sql",
    "070_operations_recovery.sql",
    "080_bounded_recovery.sql",
]

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _apply_chain(conn) -> None:
    with conn.cursor() as cur:
        for name in _CHAIN:
            path = MIGRATIONS / name
            if path.exists():
                cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _seed(conn):
    """Seed the org/project/datastream a preparation row references. Returns ids."""
    org_id = _id("org_")
    project_id = _id("proj_")
    ds_id = _id("ds_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'story-12.11-test') ON CONFLICT DO NOTHING",
            (org_id, org_id, org_id),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, org_id, created_by) "
            "VALUES (%s, %s, %s, %s, 'story-12.11-test') ON CONFLICT DO NOTHING",
            (project_id, project_id, project_id, org_id),
        )
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, name, module_name, source_kind, enabled, created_by, org_id) "
            "VALUES (%s, %s, 'DS', NULL, 'connector', FALSE, 'test', 'org_test_fixture')",
            (ds_id, project_id),
        )
    conn.commit()
    return org_id, project_id, ds_id


def _insert_prep(conn, *, org_id, project_id, ds_id, kind):
    prep_id = _id("prep_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.operation_preparations
                (id, org_id, project_id, datastream_id, kind, actor,
                 target_versions, impact, quota, idempotency_key_hash, expires_at)
            VALUES (%s, %s, %s, %s, %s, 'test',
                    '{}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                    repeat('a', 64), NOW() + INTERVAL '15 minutes')
            """,
            (prep_id, org_id, project_id, ds_id, kind),
        )
    return prep_id


# ---------------------------------------------------------------------------
# Migration text assertions (run without Postgres).
# ---------------------------------------------------------------------------


def test_migration_080_is_additive_text() -> None:
    sql = (MIGRATIONS / "080_bounded_recovery.sql").read_text(encoding="utf-8")
    assert sql.strip().startswith("-- Story 12.11")
    for verb in ("synchronize", "reload", "reprocess", "retry", "refetch", "reconcile"):
        assert f"'{verb}'" in sql
    assert "operation_preparations_kind_check" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql


# ---------------------------------------------------------------------------
# Live-Postgres constraint tests.
# ---------------------------------------------------------------------------


@requires_postgres
def test_bounded_verbs_admitted_after_080(live_postgres) -> None:
    conn = live_postgres
    _apply_chain(conn)
    org_id, project_id, ds_id = _seed(conn)
    # The three Story 12.11 named verbs insert cleanly (widened CHECK).
    for kind in ("synchronize", "reload", "reprocess"):
        prep_id = _insert_prep(conn, org_id=org_id, project_id=project_id,
                               ds_id=ds_id, kind=kind)
        assert prep_id
    conn.commit()


@requires_postgres
def test_operations_verbs_still_admitted_after_080(live_postgres) -> None:
    conn = live_postgres
    _apply_chain(conn)
    org_id, project_id, ds_id = _seed(conn)
    # The 36.13 verbs remain valid (superset, additive -- no regression).
    for kind in ("retry", "refetch", "reconcile"):
        prep_id = _insert_prep(conn, org_id=org_id, project_id=project_id,
                               ds_id=ds_id, kind=kind)
        assert prep_id
    conn.commit()


@requires_postgres
def test_non_verb_kind_still_rejected_after_080(live_postgres) -> None:
    import psycopg

    conn = live_postgres
    _apply_chain(conn)
    org_id, project_id, ds_id = _seed(conn)
    # The domain did NOT become open-ended: an unknown verb is still rejected.
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_prep(conn, org_id=org_id, project_id=project_id,
                     ds_id=ds_id, kind="publish")
    conn.rollback()


@requires_postgres
def test_reapplying_080_is_idempotent(live_postgres) -> None:
    conn = live_postgres
    _apply_chain(conn)
    # A second apply is a no-op (the DO block only swaps when tokens are missing).
    with conn.cursor() as cur:
        cur.execute((MIGRATIONS / "080_bounded_recovery.sql").read_text(encoding="utf-8"))
    conn.commit()
    org_id, project_id, ds_id = _seed(conn)
    prep_id = _insert_prep(conn, org_id=org_id, project_id=project_id,
                           ds_id=ds_id, kind="reload")
    assert prep_id
    conn.commit()
