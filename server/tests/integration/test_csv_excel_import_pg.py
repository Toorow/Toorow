"""Live-Postgres contract tests for CSV/Excel governed import (Story 12.9).

These apply migrations 030/032/042/077/078 (idempotently) against a disposable test
database and exercise the REAL invariants a mocked cursor cannot catch -- the ones the
fresh-context review flagged as unshipped or unproven:

  * AC2 contract versioning: ``version_contract`` inserts a ``cic_<ULID>`` row for a new
    config and DE-DUPLICATES an identical config (same fingerprint -> same id).
  * the 078 immutability trigger: the contract identity fields are frozen; DELETE is
    rejected; ``label`` / ``is_active`` remain mutable.
  * the ledger ``import_contract_id`` FK: ``run_import`` threads the contract id onto the
    ledger row and the FK to ``csv_excel_import_contracts`` holds.
  * the UNMOCKED rejection gate: parsed per-row rejections (mutation-shaped dents) drive a
    real ``evaluate_rejection_gate_for_ledger`` call that blocks above the threshold and
    the outcome is marked ``failed``; a below-threshold import stays ``written``.
  * publication honesty: a passing gate leaves the ledger ``written`` and the result
    carries ``published=False`` / ``outcome='written_pending_publication'`` -- never a
    fabricated publish (physical write + pointer swap are Phase B).

They SKIP when TEST_POSTGRES_DSN is unset. Migration 078 is applied to Supabase only
under Jean's authorization; these tests apply it to the disposable test database.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"
INTENT_MIGRATION = MIGRATIONS / "030_versioned_datastream_intents.sql"
MAPPING_MIGRATION = MIGRATIONS / "032_datastream_field_mappings.sql"
REGISTRY_MIGRATION = MIGRATIONS / "042_datastream_candidate_registry.sql"
LEDGER_MIGRATION = MIGRATIONS / "077_managed_feed_import_ledger.sql"
CONTRACT_MIGRATION = MIGRATIONS / "078_csv_excel_import_contract.sql"

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)

_EXECUTABLE_PLAN = {"executable": True, "grain": ["date"]}


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _apply_migrations(conn) -> None:
    with conn.cursor() as cur:
        for path in (
            INTENT_MIGRATION,
            MAPPING_MIGRATION,
            REGISTRY_MIGRATION,
            LEDGER_MIGRATION,
            CONTRACT_MIGRATION,
        ):
            cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _seed(conn, project_id: str, ds_id: str, plan_id: str, mapping_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by)
            VALUES (%s, %s, %s, 'story-12.9-test')
            """,
            (project_id, project_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by)
            VALUES (%s, %s, 'DS', NULL, 'managed_feed', FALSE, 'test')
            """,
            (ds_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', 'managed_feed', 'toorow', 'managed_raw',
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
            VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('c', 64), '0.1.1',
                    '1', TRUE, '{}'::jsonb, '{}'::jsonb, repeat('d', 64), 'test')
            """,
            (mapping_id, ds_id, project_id, plan_id),
        )
    conn.commit()


# A CSV with 2 good integer rows + N garbage rows -> N per-row rejections. Used to feed
# the UNMOCKED rejection gate real mutation-shaped dents.
def _csv_with_rejections(n_bad: int) -> bytes:
    lines = ["id", "1", "2"] + ["garbage" for _ in range(n_bad)]
    return "\n".join(lines).encode("utf-8")


def _clean_csv() -> bytes:
    return "id\n1\n2\n3\n4\n".encode("utf-8")


# ---------------------------------------------------------------------------
# Migration text assertions (run without Postgres).
# ---------------------------------------------------------------------------


def test_migration_078_owns_contracts_table_and_ledger_fk_only() -> None:
    sql = CONTRACT_MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app.csv_excel_import_contracts" in sql
    assert "uq_csv_excel_contract_fingerprint" in sql
    assert "trg_csv_excel_contract_immutable" in sql
    assert "ADD COLUMN IF NOT EXISTS import_contract_id" in sql
    # M1/M2: 078 must NOT re-add columns owned by 042 / 077.
    assert "ADD COLUMN IF NOT EXISTS allow_empty_publication" not in sql
    assert "ADD COLUMN IF NOT EXISTS max_rejected_row_pct" not in sql
    # Additive / idempotent shape.
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert "DROP TRIGGER IF EXISTS" in sql
    # AD-2: no provider vocabulary in the migration.
    lowered = sql.lower()
    for provider in ("google_ads", "meta", "tiktok", "shopify", "stripe"):
        assert provider not in lowered


# ---------------------------------------------------------------------------
# Live-Postgres constraint tests.
# ---------------------------------------------------------------------------


@requires_postgres
def test_version_contract_new_row_then_dedup(live_postgres) -> None:
    from core.csv_excel_import import version_contract

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    contract = {"format": "csv", "write_mode": "replace", "header_row": 1, "delimiter": ";"}

    first = version_contract(
        contract, datastream_id=ds_id, project_id=project_id, actor="u", conn=conn
    )
    conn.commit()
    assert first.startswith("cic_")

    # Identical config (different dict ordering) -> SAME id (dedup), no new row.
    same = version_contract(
        {"delimiter": ";", "header_row": 1, "write_mode": "replace", "format": "csv"},
        datastream_id=ds_id, project_id=project_id, actor="u", conn=conn,
    )
    conn.commit()
    assert same == first

    # A DIFFERENT config -> a new row with a new id.
    other = version_contract(
        {"format": "csv", "write_mode": "replace", "header_row": 2},
        datastream_id=ds_id, project_id=project_id, actor="u", conn=conn,
    )
    conn.commit()
    assert other != first

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.csv_excel_import_contracts WHERE datastream_id = %s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == 2  # only two distinct fingerprints
    conn.rollback()


@requires_postgres
def test_contract_identity_is_frozen_and_delete_rejected(live_postgres) -> None:
    import psycopg
    from core.csv_excel_import import version_contract

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    cid = version_contract(
        {"format": "csv", "write_mode": "replace", "header_row": 1},
        datastream_id=ds_id, project_id=project_id, actor="u", conn=conn,
    )
    conn.commit()

    # Mutating a frozen identity field is rejected by the 078 trigger.
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.csv_excel_import_contracts SET format = 'excel' WHERE id = %s",
                (cid,),
            )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp")

    # DELETE is rejected (append-only).
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp2")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.csv_excel_import_contracts WHERE id = %s", (cid,)
            )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp2")

    # label + is_active ARE mutable (allowed).
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.csv_excel_import_contracts SET label = 'v1', is_active = FALSE "
            "WHERE id = %s",
            (cid,),
        )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT label, is_active FROM app.csv_excel_import_contracts WHERE id = %s",
            (cid,),
        )
        label, active = cur.fetchone()
    assert label == "v1"
    assert active is False
    conn.rollback()


@requires_postgres
def test_run_import_threads_contract_id_and_fk_holds(live_postgres) -> None:
    from core.csv_excel_import import OUTCOME_WRITTEN_PENDING_PUBLICATION, run_import

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    contract = {"format": "csv", "write_mode": "replace", "header_row": 1}
    result = run_import(
        _clean_csv(),
        datastream_id=ds_id,
        project_id=project_id,
        plan_version_id=plan_id,
        mapping_version_id=mapping_id,
        projection_plan=_EXECUTABLE_PLAN,
        actor="u",
        idempotency_key="imp-ci-1",
        source_metadata={"filename": "clean.csv"},
        contract=contract,
        conn=conn,
    )
    conn.commit()

    # Publication honesty: written, NOT published (Phase B).
    assert result["blocked"] is False
    assert result["published"] is False
    assert result["outcome"] == OUTCOME_WRITTEN_PENDING_PUBLICATION
    assert result["row_count"] == 4
    assert result["rejected_count"] == 0

    contract_id = result["import_contract_id"]
    assert contract_id.startswith("cic_")

    # The ledger row carries import_contract_id and the FK resolves to the contract row.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT import_contract_id, outcome FROM app.managed_feed_import_ledger "
            "WHERE id = %s AND project_id = %s",
            (result["ledger"]["id"], project_id),
        )
        row = cur.fetchone()
    assert row[0] == contract_id
    assert row[1] == "written"

    # The FK target actually exists (join succeeds).
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.id
            FROM app.managed_feed_import_ledger l
            JOIN app.csv_excel_import_contracts c ON c.id = l.import_contract_id
            WHERE l.id = %s
            """,
            (result["ledger"]["id"],),
        )
        assert cur.fetchone()[0] == contract_id
    conn.rollback()


@requires_postgres
def test_ledger_rejects_dangling_contract_fk(live_postgres) -> None:
    """The import_contract_id FK is enforced: a non-existent contract id is rejected."""
    import psycopg
    from core.managed_feed_ledger import open_import

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        open_import(
            datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
            mapping_version_id=mapping_id, feed_format="csv",
            projection_plan=_EXECUTABLE_PLAN, actor="u", idempotency_key="imp-fk",
            source_metadata={}, content_hash=None, conn=conn,
            import_contract_id="cic_does_not_exist",
        )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp")
    conn.rollback()


@requires_postgres
def test_run_import_rejection_gate_blocks_and_marks_failed(live_postgres) -> None:
    """C2 end-to-end: parsed per-row rejections drive the UNMOCKED gate; over the
    threshold the ledger is marked 'failed' and the result is blocked."""
    from core.csv_excel_import import run_import

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    # 2 accepted + 8 rejected = 8/10 = 80% > default 25% -> blocked.
    result = run_import(
        _csv_with_rejections(8),
        datastream_id=ds_id,
        project_id=project_id,
        plan_version_id=plan_id,
        mapping_version_id=mapping_id,
        projection_plan=_EXECUTABLE_PLAN,
        actor="u",
        idempotency_key="imp-gate-block",
        source_metadata={"filename": "dents.csv"},
        contract={"format": "csv", "write_mode": "replace", "header_row": 1},
        conn=conn,
    )
    conn.commit()

    assert result["blocked"] is True
    assert result["reason"] == "rejection_threshold_exceeded"
    assert result["published"] is False
    assert result["row_count"] == 2
    assert result["rejected_count"] == 8

    # The ledger row is terminal 'failed'; the rejected rows are traceable + append-only.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, rejected_row_count FROM app.managed_feed_import_ledger "
            "WHERE id = %s",
            (result["ledger"]["id"],),
        )
        outcome, rej_count = cur.fetchone()
    assert outcome == "failed"
    assert rej_count == 8

    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.managed_feed_rejected_rows WHERE ledger_id = %s",
            (result["ledger"]["id"],),
        )
        assert cur.fetchone()[0] == 8
    conn.rollback()


@requires_postgres
def test_run_import_below_threshold_stays_written(live_postgres) -> None:
    """Below the rejection threshold the import is NOT blocked and stays 'written'
    (pending publication) -- the allowed rejections remain downloadable, not fatal."""
    from core.csv_excel_import import run_import

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    # Raise the threshold generously so 1 bad row out of many stays allowed.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.project_preferences (project_id, max_rejected_row_pct)
            VALUES (%s, 90)
            ON CONFLICT (project_id)
              DO UPDATE SET max_rejected_row_pct = EXCLUDED.max_rejected_row_pct
            """,
            (project_id,),
        )
    conn.commit()

    result = run_import(
        _csv_with_rejections(1),  # 2 accepted, 1 rejected = 33% < 90%
        datastream_id=ds_id,
        project_id=project_id,
        plan_version_id=plan_id,
        mapping_version_id=mapping_id,
        projection_plan=_EXECUTABLE_PLAN,
        actor="u",
        idempotency_key="imp-gate-allow",
        source_metadata={"filename": "one-dent.csv"},
        contract={"format": "csv", "write_mode": "replace", "header_row": 1},
        conn=conn,
    )
    conn.commit()

    assert result["blocked"] is False
    assert result["published"] is False  # still Phase B: written, not published
    assert result["rejected_count"] == 1
    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome FROM app.managed_feed_import_ledger WHERE id = %s",
            (result["ledger"]["id"],),
        )
        assert cur.fetchone()[0] == "written"
    conn.rollback()
