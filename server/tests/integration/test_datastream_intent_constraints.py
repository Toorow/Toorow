"""Database contract tests for immutable Datastream intent versions (Story 12.2)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra" / "nango" / "migrations" / "030_versioned_datastream_intents.sql"
requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _seed_project(conn, project_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by)
            VALUES (%s, %s, %s, 'story-12.2-test')
            """,
            (project_id, project_id, project_id),
        )


def test_migration_declares_additive_immutable_contract() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS source_kind" in sql
    assert "CREATE TABLE IF NOT EXISTS app.datastream_plan_versions" in sql
    assert "CREATE TABLE IF NOT EXISTS app.datastream_schedule_state" in sql
    assert "CREATE TRIGGER trg_datastream_plan_versions_immutable" in sql
    assert "UNIQUE (datastream_id, version_number)" in sql
    assert "UNIQUE (project_id, idempotency_key_hash)" in sql


@requires_postgres
def test_plan_versions_are_immutable_and_pointer_is_same_datastream(live_postgres) -> None:
    import psycopg

    conn = live_postgres
    project_id = _id("proj_intent_")
    first_ds = _id("ds_")
    second_ds = _id("ds_")
    first_plan = _id("dsp_")
    second_plan = _id("dsp_")
    payload = '{"contract_version":"1"}'
    try:
        _seed_project(conn, project_id)
        with conn.cursor() as cur:
            for ds_id, name in ((first_ds, "First"), (second_ds, "Second")):
                cur.execute(
                    """
                    INSERT INTO app.datastreams
                        (id, project_id, name, module_name, source_kind, enabled, created_by)
                    VALUES (%s, %s, %s, 'generic', 'connector_pull', FALSE, 'test')
                    """,
                    (ds_id, project_id, name),
                )
            for plan_id, ds_id in ((first_plan, first_ds), (second_plan, second_ds)):
                cur.execute(
                    """
                    INSERT INTO app.datastream_plan_versions
                        (id, datastream_id, project_id, version_number, contract_version,
                         source_kind, writer_kind, destination_policy, normalized_payload,
                         content_hash, idempotency_key_hash, created_by)
                    VALUES
                        (%s, %s, %s, 1, '1', 'connector_pull', 'toorow',
                         'managed_raw', %s::jsonb, repeat('a', 64), repeat(%s, 64), 'test')
                    """,
                    (plan_id, ds_id, project_id, payload, "b" if ds_id == first_ds else "c"),
                )
            cur.execute("SAVEPOINT immutable_check")

        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.datastream_plan_versions SET executable = TRUE WHERE id = %s",
                    (first_plan,),
                )
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT immutable_check")
            cur.execute("SAVEPOINT pointer_check")

        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.datastreams SET current_plan_version_id = %s WHERE id = %s",
                    (second_plan, first_ds),
                )
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT pointer_check")
    finally:
        conn.rollback()


@requires_postgres
def test_concurrent_revisions_get_unique_ordinals_and_latest_pointer() -> None:
    """Two real transactions serialize on the Datastream row lock."""
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy

    import psycopg
    from core.datastream_intents import save_datastream_intent

    from tests.core.test_datastream_intents import _intent

    dsn = os.environ["TEST_POSTGRES_DSN"]
    project_id = _id("proj_concurrent_")
    datastream_id = _id("ds_")

    with psycopg.connect(dsn) as setup_conn:
        _seed_project(setup_conn, project_id)
        with setup_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.datastreams
                    (id, project_id, name, module_name, source_kind, enabled, created_by)
                VALUES (%s, %s, 'Concurrent revisions', NULL, 'external_bq', FALSE, 'test')
                """,
                (datastream_id, project_id),
            )
        setup_conn.commit()

    def _save(index: int) -> dict:
        intent = deepcopy(_intent())
        intent["source"] = {
            "kind": "external_bq",
            "writer_kind": "external",
            "external_object": {
                "project": "customer-project",
                "dataset": "analytics",
                "object": f"daily_export_{index}",
                "writer_identity": "customer-pipeline",
            },
            "selection": {
                "selection_mode": "subset",
                "metrics": [],
                "dimensions": [],
                "grain": [],
                "filters": [],
            },
        }
        intent["destination"] = {"policy": "external_read_only"}
        with psycopg.connect(dsn) as conn:
            return save_datastream_intent(
                datastream_id=datastream_id,
                project_id=project_id,
                intent=intent,
                identity="story-12.2-test",
                idempotency_key=f"concurrent-{project_id}-{index}",
                conn=conn,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_save, (1, 2)))

    assert {result["version_number"] for result in results} == {1, 2}
    with psycopg.connect(dsn) as verify_conn:
        with verify_conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, version_number
                FROM app.datastream_plan_versions
                WHERE datastream_id = %s
                ORDER BY version_number
                """,
                (datastream_id,),
            )
            versions = cur.fetchall()
            cur.execute(
                "SELECT current_plan_version_id FROM app.datastreams WHERE id = %s",
                (datastream_id,),
            )
            current_plan_id = cur.fetchone()[0]
            cur.execute(
                "UPDATE app.projects SET status = 'archived' WHERE id = %s",
                (project_id,),
            )
        verify_conn.commit()

    assert [version_number for _, version_number in versions] == [1, 2]
    assert current_plan_id == versions[-1][0]

@requires_postgres
def test_legacy_row_remains_valid(live_postgres) -> None:
    conn = live_postgres
    project_id = _id("proj_legacy_")
    ds_id = _id("ds_")
    try:
        _seed_project(conn, project_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.datastreams
                    (id, project_id, name, module_name, schedule_mode, enabled, created_by)
                VALUES (%s, %s, 'Legacy nightly', 'generic', 'nightly', TRUE, 'test')
                RETURNING source_kind, current_plan_version_id, enabled
                """,
                (ds_id, project_id),
            )
            assert cur.fetchone() == (None, None, True)
    finally:
        conn.rollback()
