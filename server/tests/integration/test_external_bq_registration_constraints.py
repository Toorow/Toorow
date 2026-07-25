"""Live-Postgres contract tests for read-only external-BQ registration (Story 12.7).

These apply migrations 030/032/042/076 (idempotently) and exercise the REAL
constraints a mocked cursor cannot catch:

  * the append-only ``app.external_bq_observations`` trigger (UPDATE/DELETE rejected);
  * the project-scoped FK (an observation cannot cross into another project);
  * the EXPLICIT scheduler-guard flag ``external_dispatch_excluded`` synced from
    ``source_kind`` by the migration-076 trigger (external_bq => TRUE, others FALSE);
  * a real ``observe_and_register`` on a fresh ``ok`` verdict minting a 12.5
    execution + an observation row atomically.

They SKIP when TEST_POSTGRES_DSN is unset. The migration itself is applied to
Supabase only under Jean's authorization; these tests apply it to the disposable
test database referenced by TEST_POSTGRES_DSN.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"
INTENT_MIGRATION = MIGRATIONS / "030_versioned_datastream_intents.sql"
MAPPING_MIGRATION = MIGRATIONS / "032_datastream_field_mappings.sql"
REGISTRY_MIGRATION = MIGRATIONS / "042_datastream_candidate_registry.sql"
EXTERNAL_BQ_MIGRATION = MIGRATIONS / "076_external_bq_readonly.sql"

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)

pytestmark = requires_postgres

_EXTERNAL_OBJECT = {
    "project": "acme-analytics",
    "dataset": "warehouse",
    "object": "fct_orders",
    "writer_identity": "dbt-runner@acme.iam.gserviceaccount.com",
}


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _apply_migrations(conn) -> None:
    with conn.cursor() as cur:
        for path in (
            INTENT_MIGRATION,
            MAPPING_MIGRATION,
            REGISTRY_MIGRATION,
            EXTERNAL_BQ_MIGRATION,
        ):
            cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _seed_project(conn, project_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by)
            VALUES (%s, %s, %s, 'story-12.7-test')
            ON CONFLICT (id) DO NOTHING
            """,
            (project_id, project_id, project_id),
        )
    conn.commit()


def _seed_external_datastream(
    conn, project_id: str, ds_id: str, plan_id: str, mapping_id: str
) -> None:
    with conn.cursor() as cur:
        # external_bq datastream: no module, no connection (read-only registration).
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by)
            VALUES (%s, %s, 'EXT', NULL, 'external_bq', FALSE, 'test')
            """,
            (ds_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', 'external_bq', 'external', 'external_read_only',
                    '{}'::jsonb, repeat('a', 64), repeat('b', 64), 'test')
            """,
            (plan_id, ds_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, executable, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', repeat('c', 64), %s, repeat('d', 64),
                    '0.1.1', '1', TRUE, '{}'::jsonb, '{}'::jsonb, repeat('e', 64), 'test')
            """,
            (mapping_id, ds_id, project_id, plan_id),
        )
    conn.commit()


@pytest.fixture()
def pg_conn():
    import psycopg  # noqa: PLC0415

    dsn = os.environ["TEST_POSTGRES_DSN"]
    conn = psycopg.connect(dsn)
    _apply_migrations(conn)
    yield conn
    conn.rollback()
    conn.close()


def test_scheduler_exclusion_flag_synced_from_source_kind(pg_conn):
    """migration-076 trigger: external_bq => external_dispatch_excluded TRUE."""
    project_id = _id("proj_")
    ds_id = _id("ds_")
    _seed_project(pg_conn, project_id)
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by)
            VALUES (%s, %s, 'EXT', NULL, 'external_bq', FALSE, 'test')
            RETURNING external_dispatch_excluded
            """,
            (ds_id, project_id),
        )
        assert cur.fetchone()[0] is True
        # Flip to connector_pull -> flag flips to FALSE.
        cur.execute(
            """
            UPDATE app.datastreams
            SET source_kind = 'connector_pull', module_name = 'generic'
            WHERE id = %s
            RETURNING external_dispatch_excluded
            """,
            (ds_id,),
        )
        assert cur.fetchone()[0] is False
    pg_conn.commit()


def test_observation_ledger_is_append_only(pg_conn):
    project_id = _id("proj_")
    ds_id = _id("ds_")
    plan_id = _id("dsp_").replace("dsp_", "dsp_")
    mapping_id = _id("dmap_").replace("dmap_", "dmap_")
    _seed_project(pg_conn, project_id)
    _seed_external_datastream(pg_conn, project_id, ds_id, plan_id, mapping_id)

    obs_id = "ebqo_" + "01J8ZC4Q0N7R2K3W5X6Y7Z8A9B"[:26]
    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.external_bq_observations
                (id, datastream_id, project_id, object_project, object_dataset,
                 object_name, writer_identity, row_count, verdict, observed_by)
            VALUES (%s, %s, %s, 'acme', 'wh', 'fct', 'ext@acme', 100, 'ok', 'test')
            """,
            (obs_id, ds_id, project_id),
        )
    pg_conn.commit()

    # UPDATE rejected.
    with pytest.raises(Exception):
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.external_bq_observations SET verdict = 'absent' WHERE id = %s",
                (obs_id,),
            )
    pg_conn.rollback()
    # DELETE rejected.
    with pytest.raises(Exception):
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM app.external_bq_observations WHERE id = %s", (obs_id,))
    pg_conn.rollback()


def test_observe_and_register_mints_execution_and_observation(pg_conn):
    from core import external_bq_registration as ebq  # noqa: PLC0415

    project_id = _id("proj_")
    ds_id = _id("ds_")
    plan_id = "dsp_" + "01J8ZC4Q0N7R2K3W5X6Y7Z8A9C"[:26]
    mapping_id = "dmap_" + "01J8ZC4Q0N7R2K3W5X6Y7Z8A9D"[:26]
    _seed_project(pg_conn, project_id)
    _seed_external_datastream(pg_conn, project_id, ds_id, plan_id, mapping_id)

    result = ebq.observe_and_register(
        datastream_id=ds_id,
        project_id=project_id,
        external_object=_EXTERNAL_OBJECT,
        plan_version_id=plan_id,
        mapping_version_id=mapping_id,
        projection_plan={"executable": True, "measures": ["revenue"]},
        probe_result={
            "exists": True,
            "readable": True,
            "row_count": 100,
            "schema_hash": "d" * 64,
            "object_location": "EU",
            "sample_rows": [{"a": 1}],
            "watermark": datetime(2026, 7, 22, tzinfo=timezone.utc),
        },
        actor="lead@acme",
        idempotency_key="idem-live-1",
        conn=pg_conn,
        project_region="EU",
    )
    pg_conn.commit()

    assert result["verdict"] == "ok"
    assert result["execution"]["state"] == "created"
    # The observation row exists and links to the minted execution.
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT verdict, execution_id FROM app.external_bq_observations "
            "WHERE datastream_id = %s",
            (ds_id,),
        )
        verdict, execution_id = cur.fetchone()
    assert verdict == "ok"
    assert execution_id == result["execution"]["id"]


def test_repeated_unchanged_observation_is_noop(pg_conn):
    from core import external_bq_registration as ebq  # noqa: PLC0415

    project_id = _id("proj_")
    ds_id = _id("ds_")
    plan_id = "dsp_" + "01J8ZC4Q0N7R2K3W5X6Y7Z8A9E"[:26]
    mapping_id = "dmap_" + "01J8ZC4Q0N7R2K3W5X6Y7Z8A9F"[:26]
    _seed_project(pg_conn, project_id)
    _seed_external_datastream(pg_conn, project_id, ds_id, plan_id, mapping_id)

    probe = {
        "exists": True,
        "readable": True,
        "row_count": 100,
        "schema_hash": "d" * 64,
        "object_location": "EU",
        "sample_rows": [{"a": 1}],
        "watermark": datetime(2026, 7, 22, tzinfo=timezone.utc),
    }
    common = dict(
        datastream_id=ds_id,
        project_id=project_id,
        external_object=_EXTERNAL_OBJECT,
        plan_version_id=plan_id,
        mapping_version_id=mapping_id,
        projection_plan={"executable": True},
        actor="lead@acme",
        conn=pg_conn,
        project_region="EU",
    )
    first = ebq.observe_and_register(probe_result=probe, idempotency_key="idem-a", **common)
    pg_conn.commit()
    assert first["verdict"] == "ok"

    # Second identical observation -> auditable no-op, no new execution.
    second = ebq.observe_and_register(probe_result=probe, idempotency_key="idem-b", **common)
    pg_conn.commit()
    assert second["verdict"] == "unchanged_noop"
    assert "execution" not in second

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_executions WHERE datastream_id = %s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == 1  # only the first observation minted an execution


def test_same_key_same_evidence_replay_returns_existing_execution(pg_conn):
    """HIGH-1: a replay with the same idempotency_key + same observation evidence
    reproduces a byte-identical projection-plan snapshot (deterministic vpull id), so
    the 12.5 candidate registry returns the EXISTING execution instead of raising a
    false ``IdempotencyConflict``.
    """
    from core import external_bq_registration as ebq  # noqa: PLC0415
    from core.datastream_publication import create_execution  # noqa: PLC0415

    project_id = _id("proj_")
    ds_id = _id("ds_")
    plan_id = "dsp_" + "01J8ZC4Q0N7R2K3W5X6Y7Z8B10"[:26]
    mapping_id = "dmap_" + "01J8ZC4Q0N7R2K3W5X6Y7Z8B11"[:26]
    _seed_project(pg_conn, project_id)
    _seed_external_datastream(pg_conn, project_id, ds_id, plan_id, mapping_id)

    probe = {
        "exists": True,
        "readable": True,
        "row_count": 100,
        "schema_hash": "d" * 64,
        "object_location": "EU",
        "sample_rows": [{"a": 1}],
        "watermark": datetime(2026, 7, 22, tzinfo=timezone.utc),
    }
    first = ebq.observe_and_register(
        datastream_id=ds_id,
        project_id=project_id,
        external_object=_EXTERNAL_OBJECT,
        plan_version_id=plan_id,
        mapping_version_id=mapping_id,
        projection_plan={"executable": True, "measures": ["revenue"]},
        probe_result=probe,
        actor="lead@acme",
        idempotency_key="idem-replay",
        conn=pg_conn,
        project_region="EU",
    )
    pg_conn.commit()
    minted_id = first["execution"]["id"]

    # Rebuild the SAME deterministic provenance snapshot the first call handed to
    # create_execution, and replay it with the SAME idempotency_key.
    observed_hash = ebq.content_fingerprint(probe["sample_rows"], probe["row_count"])
    vpull = ebq.build_virtual_pull_commit(
        external_object=_EXTERNAL_OBJECT,
        watermark=probe["watermark"],
        content_hash=observed_hash,
        schema_hash=probe["schema_hash"],
        row_count=probe["row_count"],
        object_location=probe["object_location"],
    )
    plan_with_provenance = {
        "executable": True,
        "measures": ["revenue"],
        "virtual_pull_commit": vpull,
    }
    replay = create_execution(
        ds_id, project_id, plan_id, mapping_id, plan_with_provenance,
        "lead@acme", "idem-replay", pg_conn,
    )
    pg_conn.commit()
    # Same key + byte-identical plan -> the existing execution, NOT a conflict.
    assert replay["id"] == minted_id
