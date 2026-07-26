"""Live-Postgres contract tests for the managed-feed import ledger (Story 12.8).

These apply migrations 030/032/042/077 (idempotently) and exercise the REAL
constraints a mocked cursor cannot catch -- the KEYSTONE invariants Stories 12.9 and
12.10 depend on:

  * the immutable/append-only ledger trigger (identity frozen, DELETE rejected),
  * write-once content_hash + write-once execution_id,
  * terminal-outcome freeze,
  * idempotent-replay (same key + same payload -> the existing result),
  * different-payload-reject (same key + different payload -> ImportPayloadConflict),
  * the unchanged-snapshot NO-OP (a re-import of a published content hash creates a
    'noop' ledger row, NO candidate, NO duplicated rows),
  * the append-only rejected-rows table + the blocking rejection threshold,
  * the candidate created BEFORE any row (NFR14: the isolated 042 execution exists
    the instant the ledger row is 'opened').

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
INTENT_MIGRATION = MIGRATIONS / "030_versioned_datastream_intents.sql"
MAPPING_MIGRATION = MIGRATIONS / "032_datastream_field_mappings.sql"
REGISTRY_MIGRATION = MIGRATIONS / "042_datastream_candidate_registry.sql"
LEDGER_MIGRATION = MIGRATIONS / "077_managed_feed_import_ledger.sql"
# 078 adds the import_contract_id column + FK that open_import now writes (Story 12.9
# threads the contract version onto every ledger row). open_import's INSERT therefore
# requires 078 to be applied after 077 -- both ship together in the epic-12 closure.
CONTRACT_MIGRATION = MIGRATIONS / "078_csv_excel_import_contract.sql"

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)

_ULID_SAMPLE = "01J8ZC4Q0N7R2K3W5X6Y7Z8A9B"
_EXECUTABLE_PLAN = {"executable": True, "grain": ["date"]}


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _mfl(index: int) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "mfl_" + _ULID_SAMPLE[:-1] + alphabet[index % len(alphabet)]


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
            INSERT INTO app.projects (id, name, slug, created_by, org_id)
            VALUES (%s, %s, %s, 'story-12.8-test', 'org_test_fixture')
            """,
            (project_id, project_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
            VALUES (%s, %s, 'DS', NULL, 'managed_feed', FALSE, 'test', 'org_test_fixture')
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


def _publish_execution(conn, exec_id: str, ds_id: str, project_id: str) -> None:
    """Force an execution + pointer to 'published' so the no-op oracle sees it."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.datastream_executions
            SET state = 'published', state_changed_at = NOW()
            WHERE id = %s
            """,
            (exec_id,),
        )
        cur.execute(
            """
            UPDATE app.datastreams SET current_published_execution_id = %s
            WHERE id = %s AND project_id = %s
            """,
            (exec_id, ds_id, project_id),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Migration text assertions (run without Postgres).
# ---------------------------------------------------------------------------


def test_migration_077_declares_ledger_rejected_rows_and_triggers() -> None:
    sql = LEDGER_MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app.managed_feed_import_ledger" in sql
    assert "CREATE TABLE IF NOT EXISTS app.managed_feed_rejected_rows" in sql
    # Append-only / immutability triggers.
    assert "trg_managed_feed_import_ledger_protect" in sql
    assert "BEFORE UPDATE OR DELETE ON app.managed_feed_import_ledger" in sql
    assert "trg_managed_feed_rejected_rows_immutable" in sql
    assert "app.protect_managed_feed_import_ledger" in sql
    # Idempotency uniqueness + content lookup for the no-op path.
    assert "uq_managed_feed_import_ledger_idempotency" in sql
    assert "idx_managed_feed_import_ledger_content" in sql
    # Composite-scoped FK to the 042 execution registry.
    assert "REFERENCES app.datastream_executions (id, datastream_id, project_id)" in sql
    # Rejection-threshold preference (additive).
    assert "max_rejected_row_pct" in sql
    # Write-once + terminal-freeze invariants are enforced in the trigger body.
    assert "content_hash is write-once" in sql
    assert "execution_id is write-once" in sql
    assert "is terminal" in sql


def test_migration_077_is_additive_and_idempotent_shaped() -> None:
    sql = LEDGER_MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "ADD COLUMN IF NOT EXISTS max_rejected_row_pct" in sql
    assert "CREATE OR REPLACE FUNCTION" in sql
    assert "DROP TRIGGER IF EXISTS" in sql
    # AD-2: no provider/source vocabulary in the migration (format is an opaque enum).
    lowered = sql.lower()
    for provider in ("google_ads", "meta", "tiktok", "shopify", "stripe"):
        assert provider not in lowered


# ---------------------------------------------------------------------------
# Live-Postgres constraint tests.
# ---------------------------------------------------------------------------


@requires_postgres
def test_open_import_creates_ledger_and_isolated_candidate_before_rows(
    live_postgres,
) -> None:
    from core.managed_feed_ledger import OUTCOME_OPENED, open_import

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    result = open_import(
        datastream_id=ds_id,
        project_id=project_id,
        plan_version_id=plan_id,
        mapping_version_id=mapping_id,
        feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN,
        actor="user-1",
        idempotency_key="imp-1",
        source_metadata={"filename": "b.csv"},
        content_hash="a" * 64,
        conn=conn,
    )
    conn.commit()

    assert result["no_op"] is False
    assert result["replay"] is False
    ledger = result["ledger"]
    assert ledger["outcome"] == OUTCOME_OPENED
    # NFR14: the isolated candidate exists the instant the ledger row is opened.
    assert result["execution"] is not None
    assert ledger["execution_id"] == result["execution"]["id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM app.datastream_executions WHERE id = %s",
            (ledger["execution_id"],),
        )
        assert cur.fetchone()[0] == "created"
    conn.rollback()


@requires_postgres
def test_idempotent_replay_same_key_same_payload_returns_existing(
    live_postgres,
) -> None:
    from core.managed_feed_ledger import open_import

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    kwargs = dict(
        datastream_id=ds_id,
        project_id=project_id,
        plan_version_id=plan_id,
        mapping_version_id=mapping_id,
        feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN,
        actor="user-1",
        idempotency_key="imp-replay",
        source_metadata={"filename": "b.csv"},
        content_hash="a" * 64,
    )
    first = open_import(conn=conn, **kwargs)
    conn.commit()
    second = open_import(conn=conn, **kwargs)
    conn.commit()

    assert second["replay"] is True
    assert second["ledger"]["id"] == first["ledger"]["id"]
    # Exactly ONE ledger row exists for this key -- no duplicate.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.managed_feed_import_ledger WHERE datastream_id = %s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == 1
    conn.rollback()


@requires_postgres
def test_different_payload_same_key_is_rejected(live_postgres) -> None:
    from core.managed_feed_ledger import ImportPayloadConflict, open_import

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    open_import(
        datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
        mapping_version_id=mapping_id, feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN, actor="user-1", idempotency_key="imp-x",
        source_metadata={"filename": "b.csv"}, content_hash="a" * 64, conn=conn,
    )
    conn.commit()

    # Same key, DIFFERENT identity payload (different source metadata) -> conflict.
    with pytest.raises(ImportPayloadConflict):
        open_import(
            datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
            mapping_version_id=mapping_id, feed_format="csv",
            projection_plan=_EXECUTABLE_PLAN, actor="user-1", idempotency_key="imp-x",
            source_metadata={"filename": "OTHER.csv"}, content_hash="a" * 64, conn=conn,
        )
    conn.rollback()


@requires_postgres
def test_unchanged_snapshot_is_a_no_op_without_duplicating_rows(
    live_postgres,
) -> None:
    from core.managed_feed_ledger import (
        OUTCOME_NOOP,
        OUTCOME_PUBLISHED,
        mark_outcome,
        open_import,
        record_rows,
    )

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    content = "e" * 64

    # First import: open -> write rows -> publish (so the no-op oracle sees it).
    first = open_import(
        datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
        mapping_version_id=mapping_id, feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN, actor="user-1", idempotency_key="imp-A",
        source_metadata={"filename": "b.csv"}, content_hash=content, conn=conn,
    )
    conn.commit()
    record_rows(
        ledger_id=first["ledger"]["id"], project_id=project_id,
        landing_relation="org_x_raw.managed_feed_" + ds_id,
        accepted_row_count=42, content_hash=content, rejected_rows=[],
        actor="user-1", conn=conn,
    )
    conn.commit()
    _publish_execution(conn, first["execution"]["id"], ds_id, project_id)
    mark_outcome(first["ledger"]["id"], project_id, OUTCOME_PUBLISHED, "user-1", conn)
    conn.commit()

    # Second import of the SAME content with a NEW idempotency key -> a no-op.
    second = open_import(
        datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
        mapping_version_id=mapping_id, feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN, actor="user-1", idempotency_key="imp-B",
        source_metadata={"filename": "b.csv"}, content_hash=content, conn=conn,
    )
    conn.commit()

    assert second["no_op"] is True
    assert second["execution"] is None
    assert second["ledger"]["outcome"] == OUTCOME_NOOP
    assert second["ledger"]["superseded_ledger_id"] == first["ledger"]["id"]
    # No SECOND candidate was created (no duplicated rows path).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_executions WHERE datastream_id = %s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == 1  # only the first import's candidate
    conn.rollback()


@requires_postgres
def test_ledger_is_append_only_delete_rejected(live_postgres) -> None:
    import psycopg
    from core.managed_feed_ledger import open_import

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    res = open_import(
        datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
        mapping_version_id=mapping_id, feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN, actor="u", idempotency_key="k",
        source_metadata={}, content_hash=None, conn=conn,
    )
    conn.commit()
    ledger_id = res["ledger"]["id"]

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.managed_feed_import_ledger WHERE id = %s", (ledger_id,)
            )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp")
    conn.rollback()


@requires_postgres
def test_ledger_identity_is_frozen(live_postgres) -> None:
    import psycopg
    from core.managed_feed_ledger import open_import

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    res = open_import(
        datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
        mapping_version_id=mapping_id, feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN, actor="u", idempotency_key="k2",
        source_metadata={"filename": "b.csv"}, content_hash="f" * 64, conn=conn,
    )
    conn.commit()
    ledger_id = res["ledger"]["id"]

    # Mutating a frozen identity field is rejected.
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.managed_feed_import_ledger SET feed_format = 'excel' WHERE id = %s",
                (ledger_id,),
            )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp")

    # content_hash is write-once: value -> different value is rejected.
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp2")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.managed_feed_import_ledger SET content_hash = %s WHERE id = %s",
                ("0" * 64, ledger_id),
            )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp2")
    conn.rollback()


@requires_postgres
def test_record_rows_writes_traceable_rejections_and_terminal_freeze(
    live_postgres,
) -> None:
    import psycopg
    from core.managed_feed_ledger import (
        OUTCOME_FAILED,
        OUTCOME_WRITTEN,
        LedgerTerminal,
        get_rejected_rows,
        mark_outcome,
        open_import,
        record_rows,
    )

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    res = open_import(
        datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
        mapping_version_id=mapping_id, feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN, actor="u", idempotency_key="k3",
        source_metadata={}, content_hash=None, conn=conn,
    )
    conn.commit()
    ledger_id = res["ledger"]["id"]

    updated = record_rows(
        ledger_id=ledger_id, project_id=project_id,
        landing_relation="org_x_raw.managed_feed_" + ds_id,
        accepted_row_count=8, content_hash="1" * 64,
        rejected_rows=[
            {"row_number": 3, "field_name": "date", "rule": "unparseable_date",
             "reason": "not ISO", "rejected_value": "31/13/2026"},
            {"row_number": 7, "field_name": "spend", "rule": "type_mismatch",
             "reason": "not numeric", "rejected_value": "n/a"},
        ],
        actor="u", conn=conn,
    )
    conn.commit()
    assert updated["outcome"] == OUTCOME_WRITTEN
    assert updated["row_count"] == 8
    assert updated["rejected_row_count"] == 2
    assert updated["content_hash"] == "1" * 64  # write-once NULL->value

    rejected = get_rejected_rows(ledger_id, project_id, conn)
    assert len(rejected) == 2
    first = rejected[0]
    # Traceable to field + rule + row + execution + ledger.
    assert first["row_number"] == 3
    assert first["field_name"] == "date"
    assert first["rule"] == "unparseable_date"
    assert first["execution_id"] == res["execution"]["id"]
    assert first["ledger_id"] == ledger_id

    # rejected rows are append-only.
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.managed_feed_rejected_rows SET rule = 'x' WHERE ledger_id = %s",
                (ledger_id,),
            )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp")

    # Mark terminal 'failed'; a second terminal mark is rejected (frozen).
    mark_outcome(ledger_id, project_id, OUTCOME_FAILED, "u", conn)
    conn.commit()
    with pytest.raises(LedgerTerminal):
        mark_outcome(ledger_id, project_id, OUTCOME_FAILED, "u", conn)
    conn.rollback()


@requires_postgres
def test_rejection_threshold_gate_blocks_over_the_limit(live_postgres) -> None:
    from core.managed_feed_ledger import (
        GATE_REJECTION_THRESHOLD_EXCEEDED,
        evaluate_rejection_gate_for_ledger,
        open_import,
        record_rows,
    )

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    res = open_import(
        datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
        mapping_version_id=mapping_id, feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN, actor="u", idempotency_key="k4",
        source_metadata={}, content_hash=None, conn=conn,
    )
    conn.commit()
    ledger_id = res["ledger"]["id"]
    # 40 accepted, 60 rejected = 60% > default 25% -> blocked.
    record_rows(
        ledger_id=ledger_id, project_id=project_id,
        landing_relation="org_x_raw.managed_feed_t", accepted_row_count=40,
        content_hash="2" * 64,
        rejected_rows=[
            {"row_number": i, "rule": "bad", "reason": "x"} for i in range(60)
        ],
        actor="u", conn=conn,
    )
    conn.commit()

    issue = evaluate_rejection_gate_for_ledger(ledger_id, project_id, conn)
    assert issue is not None
    assert issue["code"] == GATE_REJECTION_THRESHOLD_EXCEEDED
    conn.rollback()


@requires_postgres
def test_second_concurrent_import_raises_import_in_progress(live_postgres) -> None:
    """H1: while one import holds a non-terminal candidate, a second import with a
    DIFFERENT payload surfaces the DOCUMENTED ``ImportInProgress`` (409), not a raw
    ``ConcurrentExecutionActive`` 500 leaking to 12.9/12.10.
    """
    from core.managed_feed_ledger import ImportInProgress, open_import

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    common = dict(
        datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
        mapping_version_id=mapping_id, feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN, actor="u", conn=conn,
    )
    first = open_import(
        idempotency_key="imp-A", source_metadata={"slot": "A"},
        content_hash=None, **common,
    )
    conn.commit()
    assert first["execution"]["state"] == "created"  # non-terminal, holds the lock

    # A different import (different key + payload) while the first is mid-flight.
    with pytest.raises(ImportInProgress) as exc:
        open_import(
            idempotency_key="imp-B", source_metadata={"slot": "B"},
            content_hash=None, **common,
        )
    assert exc.value.code == "managed_feed_import_in_progress"
    conn.rollback()


@requires_postgres
def test_record_rows_content_hash_mismatch_fails_closed(live_postgres) -> None:
    """M2: record_rows with a content_hash different from the one pinned at open_import
    fails closed (``content_hash_mismatch``) rather than silently swallowing it.
    """
    from core.managed_feed_ledger import ManagedFeedError, open_import, record_rows

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    res = open_import(
        datastream_id=ds_id, project_id=project_id, plan_version_id=plan_id,
        mapping_version_id=mapping_id, feed_format="csv",
        projection_plan=_EXECUTABLE_PLAN, actor="u", idempotency_key="k-m2",
        source_metadata={}, content_hash="1" * 64, conn=conn,
    )
    conn.commit()
    ledger_id = res["ledger"]["id"]

    with pytest.raises(ManagedFeedError) as exc:
        record_rows(
            ledger_id=ledger_id, project_id=project_id,
            landing_relation="org_x_raw.managed_feed_" + ds_id,
            accepted_row_count=10, content_hash="2" * 64,  # differs from pinned
            rejected_rows=[], actor="u", conn=conn,
        )
    assert exc.value.code == "content_hash_mismatch"
    conn.rollback()
