"""Database contract tests for immutable Datastream mapping versions and the
MDM canonical-identifier registry (Story 12.3).

Live-Postgres tests apply the migration idempotently and exercise the real
NOT NULL/FK/UNIQUE/CHECK constraints, the append-only mapping trigger, the
registry partial-unique indexes and CHECKs, and concurrent-revision ordinals.
They skip when TEST_POSTGRES_DSN is absent.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra" / "nango" / "migrations" / "032_datastream_field_mappings.sql"
INTENT_MIGRATION = ROOT / "infra" / "nango" / "migrations" / "030_versioned_datastream_intents.sql"
requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# A fixed valid ULID body (Crockford base32) reused where the exact value is
# irrelevant; uniqueness is provided by the test minting distinct suffixes.
ULID_ALPHABET_SAMPLE = "01J8ZC4Q0N7R2K3W5X6Y7Z8A9B"


def _mint_mdm_id(index: int) -> str:
    # Keep 26 chars, swap the trailing char deterministically for uniqueness.
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    tail = alphabet[index % len(alphabet)]
    return "mdm_" + ULID_ALPHABET_SAMPLE[:-1] + tail


def _seed_project(conn: Any, project_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by, org_id)
            VALUES (%s, %s, %s, 'story-12.3-test', 'org_test_fixture')
            """,
            (project_id, project_id, project_id),
        )


def _seed_datastream_and_plan(conn: Any, project_id: str, ds_id: str, plan_id: str) -> None:
    """Seed a datastream + an immutable plan version using the REAL migration-030
    columns (contract_version/destination_policy/normalized_payload/content_hash/
    idempotency_key_hash/created_by)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
            VALUES (%s, %s, 'Mapping DS', 'generic', 'connector_pull', FALSE, 'test',
                'org_test_fixture')
            """,
            (ds_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, capability_fingerprint, idempotency_key_hash, created_by)
            VALUES
                (%s, %s, %s, 1, '1', 'connector_pull', 'toorow', 'managed_raw',
                 '{"contract_version":"1"}'::jsonb, repeat('a', 64), repeat('a', 64),
                 repeat('b', 64), 'test')
            """,
            (plan_id, ds_id, project_id),
        )


def _apply_migrations(conn: Any) -> None:
    with conn.cursor() as cur:
        cur.execute(INTENT_MIGRATION.read_text(encoding="utf-8"))
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()


def test_migration_declares_mapping_and_registry_contract() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS current_mapping_version_id" in sql
    assert "CREATE TABLE IF NOT EXISTS app.datastream_mapping_versions" in sql
    assert "CREATE TRIGGER trg_datastream_mapping_versions_immutable" in sql
    assert "UNIQUE (datastream_id, version_number)" in sql
    assert "UNIQUE (project_id, idempotency_key_hash)" in sql
    assert "fk_datastream_mapping_plan_scope" in sql
    # Registry (Jean 2026-07-20).
    assert "CREATE TABLE IF NOT EXISTS app.mdm_canonical_fields" in sql
    assert "ck_mdm_canonical_fields_metric_aggregation" in sql
    assert "uq_mdm_canonical_name_platform" in sql
    assert "uq_mdm_canonical_name_project" in sql
    assert "fk_mdm_canonical_fields_dictionary" in sql
    # No immutability trigger on the registry (mutable-with-audit).
    assert "reject_datastream_mapping_version_mutation" in sql
    assert "mdm_canonical_fields" in sql
    assert "trg_mdm_canonical_fields_immutable" not in sql


@requires_postgres
def test_live_postgres_mapping_constraints_and_immutability(live_postgres: Any) -> None:
    """Exercise live table creation, FK constraints, and the immutability trigger."""
    import psycopg

    conn = live_postgres
    _apply_migrations(conn)

    proj_id = _id("proj_map_")
    ds_id = _id("ds_")
    plan_id = _id("dsp_")
    map_id = _id("dmap_")
    sha_hash = "a" * 64
    content_hash = "b" * 64

    try:
        _seed_project(conn, proj_id)
        _seed_datastream_and_plan(conn, proj_id, ds_id, plan_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.datastream_mapping_versions
                    (id, datastream_id, project_id, version_number, mapping_contract_version,
                     source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                     ossie_spec_version, toorow_extension_version, executable, blocking_count,
                     mapping_payload, ossie_projection, idempotency_key_hash, created_by)
                VALUES
                    (%s, %s, %s, 1, '1', %s, %s, %s, %s, '0.1.1', '1', true, 0,
                     '{"fields":[]}'::jsonb, '{"semantic_model":{}}'::jsonb, %s, 'test_user')
                """,
                (map_id, ds_id, proj_id, sha_hash, plan_id, sha_hash, content_hash, "c" * 64),
            )
            cur.execute(
                "UPDATE app.datastreams SET current_mapping_version_id = %s WHERE id = %s",
                (map_id, ds_id),
            )
            cur.execute("SAVEPOINT immutable_check")

        # Immutability trigger: UPDATE rejected.
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.datastream_mapping_versions SET executable = false WHERE id = %s",
                    (map_id,),
                )
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT immutable_check")
            cur.execute("SAVEPOINT delete_check")

        # Immutability trigger: DELETE rejected.
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.datastream_mapping_versions WHERE id = %s",
                    (map_id,),
                )
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT delete_check")
            cur.execute("SAVEPOINT plan_fk_check")

        # Plan-version composite FK: a mapping cannot bind a plan from another DS.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.datastream_mapping_versions
                        (id, datastream_id, project_id, version_number, mapping_contract_version,
                         source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                         ossie_spec_version, toorow_extension_version, executable, blocking_count,
                         mapping_payload, ossie_projection, idempotency_key_hash, created_by)
                    VALUES
                        (%s, %s, %s, 2, '1', %s, %s, %s, %s, '0.1.1', '1', true, 0,
                         '{}'::jsonb, '{}'::jsonb, %s, 'test_user')
                    """,
                    (
                        _id("dmap_"),
                        ds_id,
                        proj_id,
                        sha_hash,
                        _id("dsp_"),
                        sha_hash,
                        content_hash,
                        "d" * 64,
                    ),
                )
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT plan_fk_check")
    finally:
        conn.rollback()


@requires_postgres
def test_live_postgres_registry_constraints(live_postgres: Any) -> None:
    """Exercise the MDM registry: id CHECK, metric-aggregation CHECK, partial
    unique indexes (per scope), the dictionary FK, and mutability (no trigger)."""
    import psycopg

    conn = live_postgres
    _apply_migrations(conn)

    proj_id = _id("proj_reg_")
    other_proj = _id("proj_other_")
    try:
        _seed_project(conn, proj_id)
        _seed_project(conn, other_proj)

        # A metric with neither aggregation nor non_additive fails the CHECK.
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT chk_metric_agg")
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.mdm_canonical_fields
                        (id, project_id, concept_kind, canonical_name, created_by)
                    VALUES (%s, NULL, 'metric', 'spend', 'test')
                    """,
                    (_mint_mdm_id(1),),
                )
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT chk_metric_agg")

        # A metric declaring aggregation is accepted (platform scope).
        platform_id = _mint_mdm_id(2)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.mdm_canonical_fields
                    (id, project_id, concept_kind, canonical_name, aggregation, created_by)
                VALUES (%s, NULL, 'metric', 'spend', 'sum', 'test')
                """,
                (platform_id,),
            )
            cur.execute("SAVEPOINT dup_platform")

        # Duplicate canonical_name at platform scope rejected.
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.mdm_canonical_fields
                        (id, project_id, concept_kind, canonical_name, non_additive, created_by)
                    VALUES (%s, NULL, 'metric', 'spend', true, 'test')
                    """,
                    (_mint_mdm_id(3),),
                )
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT dup_platform")

        # Same canonical_name allowed under a project scope (distinct scope).
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.mdm_canonical_fields
                    (id, project_id, concept_kind, canonical_name, aggregation, created_by)
                VALUES (%s, %s, 'metric', 'spend', 'sum', 'test')
                """,
                (_mint_mdm_id(4), proj_id),
            )
            cur.execute("SAVEPOINT dup_project")

        # Duplicate within the same project scope rejected.
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.mdm_canonical_fields
                        (id, project_id, concept_kind, canonical_name, non_additive, created_by)
                    VALUES (%s, %s, 'metric', 'spend', true, 'test')
                    """,
                    (_mint_mdm_id(5), proj_id),
                )
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT dup_project")
            cur.execute("SAVEPOINT bad_id")

        # id must match the mdm_<ULID> pattern.
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.mdm_canonical_fields
                        (id, project_id, concept_kind, canonical_name, created_by)
                    VALUES ('not_a_ulid', NULL, 'dimension', 'country', 'test')
                    """
                )
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT bad_id")
            cur.execute("SAVEPOINT bad_dict_fk")

        # dictionary_field_name FK rejects an unknown dictionary field.
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.mdm_canonical_fields
                        (id, project_id, concept_kind, canonical_name,
                         dictionary_field_name, created_by)
                    VALUES (%s, NULL, 'dimension', 'country', 'no_such_dict_field', 'test')
                    """,
                    (_mint_mdm_id(6),),
                )
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT bad_dict_fk")

        # Registry rows are MUTABLE (no append-only trigger): UPDATE succeeds.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.mdm_canonical_fields SET status = 'archived' WHERE id = %s",
                (platform_id,),
            )
            cur.execute("SELECT status FROM app.mdm_canonical_fields WHERE id = %s", (platform_id,))
            assert cur.fetchone()[0] == "archived"

        # Archiving frees the platform canonical_name for re-minting.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.mdm_canonical_fields
                    (id, project_id, concept_kind, canonical_name, aggregation, created_by)
                VALUES (%s, NULL, 'metric', 'spend', 'sum', 'test')
                """,
                (_mint_mdm_id(7),),
            )
    finally:
        conn.rollback()


@requires_postgres
def test_live_postgres_mapping_binding_fk_rejects_unknown_registry_target() -> None:
    """The persist path blocks a binding whose mdm_target is not in the registry;
    a known/active in-scope target resolves. Proven through save_field_mapping."""
    import psycopg
    from core.datastream_field_mapping import save_field_mapping

    dsn = os.environ["TEST_POSTGRES_DSN"]
    proj_id = _id("proj_bind_")
    ds_id = _id("ds_")
    plan_id = _id("dsp_")
    good_target = _mint_mdm_id(8)
    missing_target = _mint_mdm_id(9)

    with psycopg.connect(dsn) as setup:
        _apply_migrations(setup)
        _seed_project(setup, proj_id)
        _seed_datastream_and_plan(setup, proj_id, ds_id, plan_id)
        with setup.cursor() as cur:
            cur.execute(
                "UPDATE app.datastreams SET current_plan_version_id = %s WHERE id = %s",
                (plan_id, ds_id),
            )
            cur.execute(
                """
                INSERT INTO app.mdm_canonical_fields
                    (id, project_id, concept_kind, canonical_name, aggregation, created_by)
                VALUES (%s, NULL, 'metric', 'cost', 'sum', 'test')
                """,
                (good_target,),
            )
        setup.commit()

    payload = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": plan_id,
        "grain": [],
        "fields": [
            {
                "field_id": "cost",
                "physical_type": "decimal",
                "profile": {
                    "nullable": False,
                    "unique": False,
                    "cardinality_signal": "high",
                    "sample_values": [],
                    "confidence": 0.9,
                },
                "suggestion": {
                    "semantic_role": "measure_spend",
                    "aggregation": "sum",
                    "non_additive": False,
                    "currency": "unknown",
                    "sensitivity": "none",
                    "status": "suggested",
                    "evidence": ["kind:metric"],
                },
                "binding": {
                    "canonical_target": "cost",
                    "mdm_target": missing_target,
                    "status": "resolved",
                    "blocking_reason": None,
                    "confirmed_by": None,
                    "confirmed_reason": None,
                },
            }
        ],
        "ambiguities": [],
    }

    with psycopg.connect(dsn) as conn:
        res = save_field_mapping(
            datastream_id=ds_id,
            project_id=proj_id,
            mapping_payload=payload,
            identity="tester",
            idempotency_key=f"bind-missing-{proj_id}",
            conn=conn,
        )
    # Unknown registry target -> binding blocked, version non-executable.
    assert res["executable"] is False
    assert res["blocking_count"] == 1
    field = res["mapping_payload"]["fields"][0]
    assert field["binding"]["status"] == "blocking"
    assert field["binding"]["blocking_reason"] == "register_or_pick_canonical_field"

    # Now point at the KNOWN active registry id -> resolves, executable.
    payload["fields"][0]["binding"]["mdm_target"] = good_target
    with psycopg.connect(dsn) as conn:
        res2 = save_field_mapping(
            datastream_id=ds_id,
            project_id=proj_id,
            mapping_payload=payload,
            identity="tester",
            idempotency_key=f"bind-good-{proj_id}",
            conn=conn,
        )
    assert res2["blocking_count"] == 0
    assert res2["executable"] is True

    # The save path performed ZERO registry writes.
    with psycopg.connect(dsn) as verify:
        with verify.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM app.mdm_canonical_fields")
            assert cur.fetchone()[0] == 1


@requires_postgres
def test_live_postgres_concurrent_revisions_unique_ordinals_and_pointer() -> None:
    """Two real transactions serialize on the Datastream row lock: unique version
    ordinals and a deterministic current_mapping_version_id pointer."""
    from concurrent.futures import ThreadPoolExecutor

    import psycopg
    from core.datastream_field_mapping import save_field_mapping

    dsn = os.environ["TEST_POSTGRES_DSN"]
    proj_id = _id("proj_conc_")
    ds_id = _id("ds_")
    plan_id = _id("dsp_")

    with psycopg.connect(dsn) as setup:
        _apply_migrations(setup)
        _seed_project(setup, proj_id)
        _seed_datastream_and_plan(setup, proj_id, ds_id, plan_id)
        with setup.cursor() as cur:
            cur.execute(
                "UPDATE app.datastreams SET current_plan_version_id = %s WHERE id = %s",
                (plan_id, ds_id),
            )
        setup.commit()

    def _payload(index: int) -> dict[str, Any]:
        return {
            "mapping_contract_version": "1",
            "source_schema_hash": ("abc"[index % 3] * 64)[:64],
            "plan_version_id": plan_id,
            "grain": [f"d{index}"],
            "fields": [],
            "ambiguities": [],
        }

    def _save(index: int) -> dict[str, Any]:
        with psycopg.connect(dsn) as conn:
            return save_field_mapping(
                datastream_id=ds_id,
                project_id=proj_id,
                mapping_payload=_payload(index),
                identity="tester",
                idempotency_key=f"conc-{proj_id}-{index}",
                conn=conn,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(_save, (1, 2)))

    assert {r["version_number"] for r in results} == {1, 2}

    with psycopg.connect(dsn) as verify:
        with verify.cursor() as cur:
            cur.execute(
                """
                SELECT id, version_number FROM app.datastream_mapping_versions
                WHERE datastream_id = %s ORDER BY version_number
                """,
                (ds_id,),
            )
            versions = cur.fetchall()
            cur.execute(
                "SELECT current_mapping_version_id FROM app.datastreams WHERE id = %s",
                (ds_id,),
            )
            current = cur.fetchone()[0]
    assert [v for _, v in versions] == [1, 2]
    assert current == versions[-1][0]


@requires_postgres
def test_live_postgres_idempotent_replay_and_conflict() -> None:
    """Same key + same payload replays the original; same key + different payload
    raises the idempotency conflict (mapped to 409 at the route)."""
    import psycopg
    from core.datastream_field_mapping import (
        DatastreamMappingConflict,
        save_field_mapping,
    )

    dsn = os.environ["TEST_POSTGRES_DSN"]
    proj_id = _id("proj_idem_")
    ds_id = _id("ds_")
    plan_id = _id("dsp_")

    with psycopg.connect(dsn) as setup:
        _apply_migrations(setup)
        _seed_project(setup, proj_id)
        _seed_datastream_and_plan(setup, proj_id, ds_id, plan_id)
        with setup.cursor() as cur:
            cur.execute(
                "UPDATE app.datastreams SET current_plan_version_id = %s WHERE id = %s",
                (plan_id, ds_id),
            )
        setup.commit()

    base = {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": plan_id,
        "grain": ["date"],
        "fields": [],
        "ambiguities": [],
    }
    key = f"idem-{proj_id}"

    with psycopg.connect(dsn) as conn:
        first = save_field_mapping(
            datastream_id=ds_id,
            project_id=proj_id,
            mapping_payload=dict(base),
            identity="tester",
            idempotency_key=key,
            conn=conn,
        )
    with psycopg.connect(dsn) as conn:
        replay = save_field_mapping(
            datastream_id=ds_id,
            project_id=proj_id,
            mapping_payload=dict(base),
            identity="tester",
            idempotency_key=key,
            conn=conn,
        )
    assert replay["id"] == first["id"]
    assert replay["idempotent_replay"] is True

    conflicting = dict(base)
    conflicting["grain"] = ["date", "campaign_id"]
    with psycopg.connect(dsn) as conn:
        with pytest.raises(DatastreamMappingConflict):
            save_field_mapping(
                datastream_id=ds_id,
                project_id=proj_id,
                mapping_payload=conflicting,
                identity="tester",
                idempotency_key=key,
                conn=conn,
            )
