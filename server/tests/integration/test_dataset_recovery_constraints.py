"""Live-Postgres contract tests for the safe dataset rollback (Story 12.12).

These apply migrations 030/032/042/081 (idempotently) and exercise the REAL
constraints a mocked cursor cannot catch:
  * the DATASET-pointer rollback swaps ``current_published_execution_id`` BACK to a
    retained healthy prior published execution ONLY after the gates pass and within
    the rollback deadline; an EXPIRED deadline DISABLES it,
  * the rollback writes a NEW append-only ``datastream_publication_log`` row (the
    042 immutability trigger still forbids mutation of history),
  * a concurrent mutation on the same destination is rejected with the active
    execution + lock reason,
  * the DATASET rollback is DISTINCT from the 36.18 MAPPING rollback: it moves
    ``current_published_execution_id`` and NOT ``current_mapping_version_id``.

They SKIP when TEST_POSTGRES_DSN is unset. Migrations are applied to the disposable
test database referenced by TEST_POSTGRES_DSN, never to Supabase (human-gated).
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
ROLLBACK_MIGRATION = MIGRATIONS / "081_dataset_rollback.sql"

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)

_ULID_SAMPLE = "01J8ZC4Q0N7R2K3W5X6Y7Z8A9B"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _dse(index: int) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "dse_" + _ULID_SAMPLE[:-1] + alphabet[index % len(alphabet)]


def _apply_migrations(conn) -> None:
    with conn.cursor() as cur:
        for path in (
            INTENT_MIGRATION,
            MAPPING_MIGRATION,
            REGISTRY_MIGRATION,
            ROLLBACK_MIGRATION,
        ):
            cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _seed(conn, project_id: str, ds_id: str, plan_id: str, mapping_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'story-12.12-test')",
            (project_id, project_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by)
            VALUES (%s, %s, 'DS', 'generic', 'connector_pull', FALSE, 'test')
            """,
            (ds_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', 'connector_pull', 'toorow', 'managed_raw',
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


def _insert_published_execution(
    conn, exec_id, ds_id, project_id, plan_id, mapping_id, *, content_hash, row_count
):
    """Insert a PUBLISHED execution + its publication-log row (a prior version)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_executions
                (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                 projection_plan_ref, state, content_hash, row_count, created_by)
            VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'published', %s, %s, 'test')
            """,
            (exec_id, ds_id, project_id, plan_id, mapping_id, content_hash, row_count),
        )
        log_id = "dplog_" + _ULID_SAMPLE[:-1] + exec_id[-1]
        cur.execute(
            """
            INSERT INTO app.datastream_publication_log
                (id, execution_id, datastream_id, project_id, plan_version_id,
                 mapping_version_id, content_hash, row_count, published_by,
                 rollback_deadline, retained)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'test',
                    NOW() + INTERVAL '30 days', TRUE)
            """,
            (log_id, exec_id, ds_id, project_id, plan_id, mapping_id, content_hash, row_count),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Migration text assertions (run without Postgres).
# ---------------------------------------------------------------------------


def test_migration_081_declares_rollback_window_and_retention() -> None:
    sql = ROLLBACK_MIGRATION.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS rollback_deadline TIMESTAMPTZ" in sql
    assert "ADD COLUMN IF NOT EXISTS retained BOOLEAN" in sql
    assert "ADD COLUMN IF NOT EXISTS rollback_window_hours INTEGER" in sql
    assert "ADD COLUMN IF NOT EXISTS append_stable_key JSONB" in sql
    # It is additive on the EXISTING 042 log, not a second engine/pointer.
    assert "app.datastream_publication_log" in sql
    assert "current_published_execution_id" not in sql or "042" in sql


def test_migration_081_has_no_backfill_update_on_the_publication_log() -> None:
    """C2: the backfill UPDATE is REMOVED (it fired the 042 append-only trigger).

    An `UPDATE app.datastream_publication_log SET rollback_deadline = ...` fires the
    042 BEFORE-UPDATE immutability trigger (RAISE EXCEPTION) for every existing row and
    aborts the whole migration on any POPULATED log. It must not be present; the
    runtime COALESCE(rollback_deadline, published_at + resolved_window) supplies the
    default instead. We assert there is no UPDATE against the publication log.
    """
    sql = ROLLBACK_MIGRATION.read_text(encoding="utf-8")
    lowered = sql.lower()
    assert "update app.datastream_publication_log" not in lowered
    # And the old 30-day hardcode backfill is gone.
    assert "interval '30 days'" not in lowered


# ---------------------------------------------------------------------------
# Live-Postgres rollback contract tests.
# ---------------------------------------------------------------------------


@requires_postgres
def test_rollback_swaps_dataset_pointer_back_and_appends_log(live_postgres) -> None:
    from core.dataset_recovery import rollback_dataset

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    prior, current = _dse(1), _dse(2)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    _insert_published_execution(
        conn, prior, ds_id, project_id, plan_id, mapping_id,
        content_hash="a" * 64, row_count=90,
    )
    _insert_published_execution(
        conn, current, ds_id, project_id, plan_id, mapping_id,
        content_hash="b" * 64, row_count=100,
    )
    # The live pointer is the CURRENT execution.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET current_published_execution_id = %s WHERE id = %s",
            (current, ds_id),
        )
    conn.commit()

    result = rollback_dataset(
        conn, datastream_id=ds_id, project_id=project_id, actor="owner-1",
        target_execution_id=prior,
    )
    assert result["rolled_back_from"] == current
    assert result["rolled_back_to"] == prior

    with conn.cursor() as cur:
        # DATASET pointer swapped BACK.
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams WHERE id = %s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == prior
        # A NEW append-only log row records the rollback (prior_execution_id == the
        # version we left).
        cur.execute(
            "SELECT prior_execution_id FROM app.datastream_publication_log WHERE id = %s",
            (result["publication_log_id"],),
        )
        assert cur.fetchone()[0] == current
    conn.rollback()


@requires_postgres
def test_rollback_disabled_when_deadline_expired(live_postgres) -> None:
    from core.dataset_recovery import RollbackWindowExpired, rollback_dataset

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    prior, current = _dse(3), _dse(4)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    _insert_published_execution(
        conn, prior, ds_id, project_id, plan_id, mapping_id,
        content_hash="a" * 64, row_count=90,
    )
    _insert_published_execution(
        conn, current, ds_id, project_id, plan_id, mapping_id,
        content_hash="b" * 64, row_count=100,
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET current_published_execution_id = %s WHERE id = %s",
            (current, ds_id),
        )
        # Force the target's rollback deadline into the past.
        cur.execute(
            "UPDATE app.datastream_publication_log "
            "SET rollback_deadline = NOW() - INTERVAL '1 hour' WHERE execution_id = %s",
            (prior,),
        )
    conn.commit()

    with pytest.raises(RollbackWindowExpired):
        rollback_dataset(
            conn, datastream_id=ds_id, project_id=project_id, actor="owner-1",
            target_execution_id=prior,
        )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams WHERE id = %s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == current  # pointer UNCHANGED
    conn.rollback()


@requires_postgres
def test_rollback_rejected_when_mutation_active(live_postgres) -> None:
    from core.dataset_recovery import ConcurrentMutationActive, rollback_dataset

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    prior, current, inflight = _dse(5), _dse(6), _dse(7)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    _insert_published_execution(
        conn, prior, ds_id, project_id, plan_id, mapping_id,
        content_hash="a" * 64, row_count=90,
    )
    _insert_published_execution(
        conn, current, ds_id, project_id, plan_id, mapping_id,
        content_hash="b" * 64, row_count=100,
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET current_published_execution_id = %s WHERE id = %s",
            (current, ds_id),
        )
        # An in-flight (non-terminal) execution => a mutation is active.
        cur.execute(
            """
            INSERT INTO app.datastream_executions
                (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                 projection_plan_ref, state, created_by)
            VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'loading', 'test')
            """,
            (inflight, ds_id, project_id, plan_id, mapping_id),
        )
    conn.commit()

    with pytest.raises(ConcurrentMutationActive) as exc:
        rollback_dataset(
            conn, datastream_id=ds_id, project_id=project_id, actor="owner-1",
            target_execution_id=prior,
        )
    assert exc.value.blocking_execution_id == inflight
    assert exc.value.lock_reason == "execution_in_flight"
    conn.rollback()


@requires_postgres
def test_dataset_rollback_is_distinct_from_mapping_rollback(live_postgres) -> None:
    """PROVE the 12.12 dataset rollback is NOT the 36.18 mapping rollback.

    A dataset rollback moves ``current_published_execution_id`` and leaves
    ``current_mapping_version_id`` untouched. (36.18's rollback_publication does the
    opposite -- it moves the mapping pointer.)
    """
    from core.dataset_recovery import rollback_dataset

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    prior, current = _dse(8), _dse(9)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    _insert_published_execution(
        conn, prior, ds_id, project_id, plan_id, mapping_id,
        content_hash="a" * 64, row_count=90,
    )
    _insert_published_execution(
        conn, current, ds_id, project_id, plan_id, mapping_id,
        content_hash="b" * 64, row_count=100,
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams "
            "SET current_published_execution_id = %s, current_mapping_version_id = %s "
            "WHERE id = %s",
            (current, mapping_id, ds_id),
        )
    conn.commit()

    rollback_dataset(
        conn, datastream_id=ds_id, project_id=project_id, actor="owner-1",
        target_execution_id=prior,
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id, current_mapping_version_id "
            "FROM app.datastreams WHERE id = %s",
            (ds_id,),
        )
        exec_ptr, mapping_ptr = cur.fetchone()
    # The DATASET (execution) pointer moved; the MAPPING pointer is UNCHANGED.
    assert exec_ptr == prior
    assert mapping_ptr == mapping_id
    conn.rollback()


# ---------------------------------------------------------------------------
# C1 -- the window is enforced on REAL commit_publication-produced rows (NULL
#       rollback_deadline), NOT only on hand-seeded deadlines.
# ---------------------------------------------------------------------------


def _publish_via_commit_publication(conn, exec_id, ds_id, project_id, plan_id, mapping_id,
                                    *, content_hash, row_count):
    """Publish through the REAL 12.5 commit_publication path (leaves deadline NULL)."""
    from core.datastream_publication import commit_publication

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_executions
                (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                 projection_plan_ref, state, content_hash, row_count, created_by)
            VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'ready', %s, %s, 'test')
            """,
            (exec_id, ds_id, project_id, plan_id, mapping_id, content_hash, row_count),
        )
    conn.commit()
    dsn = os.environ["TEST_POSTGRES_DSN"]

    class _Factory:
        def __call__(self):
            import psycopg
            return psycopg.connect(dsn)

    commit_publication(exec_id, project_id, "user-1", conn, connection_factory=_Factory())


@requires_postgres
def test_rollback_window_enforced_on_real_commit_publication_rows(live_postgres) -> None:
    """C1: a rollback target published via REAL commit_publication has a NULL stored
    rollback_deadline; the window MUST still be enforced from published_at + the
    resolved window. With a 0-hour window the target is IMMEDIATELY expired (proving the
    window is NOT a no-op that leaves data 'rollbackable forever').
    """
    from core.dataset_recovery import RollbackWindowExpired, rollback_dataset

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    prior, current = _dse(10), _dse(11)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    # Publish PRIOR then CURRENT via the real path -> both log rows have NULL deadline.
    _publish_via_commit_publication(
        conn, prior, ds_id, project_id, plan_id, mapping_id,
        content_hash="a" * 64, row_count=90,
    )
    _publish_via_commit_publication(
        conn, current, ds_id, project_id, plan_id, mapping_id,
        content_hash="b" * 64, row_count=100,
    )

    # Confirm the stored deadline really is NULL (the C1 root condition).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rollback_deadline FROM app.datastream_publication_log "
            "WHERE execution_id = %s",
            (prior,),
        )
        assert cur.fetchone()[0] is None

    # Set a 0-hour rollback window: with NULL stored deadline, the EFFECTIVE deadline is
    # published_at + 0h == published_at (already past) -> expired -> DISABLED.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.project_preferences (project_id, rollback_window_hours)
            VALUES (%s, 0)
            ON CONFLICT (project_id) DO UPDATE SET rollback_window_hours = 0
            """,
            (project_id,),
        )
    conn.commit()

    with pytest.raises(RollbackWindowExpired) as exc:
        rollback_dataset(
            conn, datastream_id=ds_id, project_id=project_id, actor="owner-1",
            target_execution_id=prior,
        )
    # The reported source is the RESOLVED window (not a phantom stored deadline).
    assert exc.value.deadline_source == "resolved_window:project_preference"

    # Pointer unchanged (rollback was disabled, not silently allowed).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams WHERE id = %s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == current
    conn.rollback()


@requires_postgres
def test_rollback_allowed_within_resolved_window_on_null_deadline(live_postgres) -> None:
    """C1 (positive): with a generous resolved window and NULL stored deadline the
    real-published target is still IN-window and the rollback proceeds.
    """
    from core.dataset_recovery import rollback_dataset

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    prior, current = _dse(12), _dse(13)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    _publish_via_commit_publication(
        conn, prior, ds_id, project_id, plan_id, mapping_id,
        content_hash="a" * 64, row_count=90,
    )
    _publish_via_commit_publication(
        conn, current, ds_id, project_id, plan_id, mapping_id,
        content_hash="b" * 64, row_count=100,
    )
    # Default window (documented default, 720h) -> in-window.
    result = rollback_dataset(
        conn, datastream_id=ds_id, project_id=project_id, actor="owner-1",
        target_execution_id=prior,
    )
    assert result["rolled_back_to"] == prior
    assert result["deadline_source"] == "resolved_window:documented_default"
    conn.rollback()


# ---------------------------------------------------------------------------
# C2 -- migration 081 applies cleanly on a POPULATED publication log (the removed
#       backfill UPDATE would have fired the 042 immutability trigger and aborted).
# ---------------------------------------------------------------------------


@requires_postgres
def test_migration_081_applies_on_populated_publication_log(live_postgres) -> None:
    """C2: seed a publication-log row BEFORE applying 081, then apply 081 and assert it
    succeeds. The old backfill UPDATE fired the 042 append-only trigger for the seeded
    row and aborted the whole migration; the fixed 081 (ADD COLUMN only) applies cleanly.
    """
    conn = live_postgres
    # Ensure the log table exists (idempotent). The shared test DSN may already carry
    # all migrations; that does NOT weaken the proof -- the point is that RE-applying
    # 081 on a POPULATED log must NOT raise. A backfill UPDATE would fire the 042
    # append-only trigger for the seeded row and abort; the fixed ADD-COLUMN-only 081
    # applies cleanly, and the seeded row's rollback_deadline stays NULL (no backfill).
    with conn.cursor() as cur:
        for path in (INTENT_MIGRATION, MAPPING_MIGRATION, REGISTRY_MIGRATION):
            cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()

    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    seeded = _dse(14)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    # Seed a PUBLISHED execution + a publication-log row (populated log, pre-081).
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_executions
                (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                 projection_plan_ref, state, content_hash, row_count, created_by)
            VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'published', %s, %s, 'test')
            """,
            (seeded, ds_id, project_id, plan_id, mapping_id, "a" * 64, 90),
        )
        log_id = "dplog_" + _ULID_SAMPLE[:-1] + seeded[-1]
        cur.execute(
            """
            INSERT INTO app.datastream_publication_log
                (id, execution_id, datastream_id, project_id, plan_version_id,
                 mapping_version_id, content_hash, row_count, published_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'test')
            """,
            (log_id, seeded, ds_id, project_id, plan_id, mapping_id, "a" * 64, 90),
        )
    conn.commit()

    # Apply 081 on the POPULATED log -- this must NOT raise (no backfill UPDATE).
    with conn.cursor() as cur:
        cur.execute(ROLLBACK_MIGRATION.read_text(encoding="utf-8"))
    conn.commit()

    # The new columns exist and the pre-existing row is untouched (deadline still NULL).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rollback_deadline, retained FROM app.datastream_publication_log "
            "WHERE id = %s",
            (log_id,),
        )
        deadline, retained = cur.fetchone()
        assert deadline is None  # not backfilled -> runtime COALESCE resolves it
        assert retained is True  # NOT NULL DEFAULT TRUE applied to the existing row
    conn.rollback()


# ---------------------------------------------------------------------------
# H1 -- a retried DEFAULT (target-less) rollback lands once and STAYS (no oscillation).
# ---------------------------------------------------------------------------


@requires_postgres
def test_default_rollback_twice_is_idempotent_no_oscillation(live_postgres) -> None:
    """H1: invoke the DEFAULT (no target) rollback TWICE. The pointer must land on the
    prior version ONCE and STAY there -- the retry must be a stable no-op, not a re-swap
    back to the version just rolled away.
    """
    from core.dataset_recovery import rollback_dataset

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    prior, current = _dse(15), _dse(16)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    _insert_published_execution(
        conn, prior, ds_id, project_id, plan_id, mapping_id,
        content_hash="a" * 64, row_count=90,
    )
    _insert_published_execution(
        conn, current, ds_id, project_id, plan_id, mapping_id,
        content_hash="b" * 64, row_count=100,
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET current_published_execution_id = %s WHERE id = %s",
            (current, ds_id),
        )
    conn.commit()

    # First default rollback: newest retained prior (that is != current) is `prior`.
    r1 = rollback_dataset(conn, datastream_id=ds_id, project_id=project_id, actor="owner-1")
    assert r1["rolled_back_to"] == prior
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams WHERE id = %s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == prior
        # Count publication-log rows for `prior` after the first rollback.
        cur.execute(
            "SELECT count(*) FROM app.datastream_publication_log WHERE execution_id = %s",
            (prior,),
        )
        prior_log_rows_after_first = cur.fetchone()[0]

    # Second default rollback (retry). The pointer is now `prior`; the newest retained
    # prior that is != the current pointer is `current` -- WITHOUT the H1 fix a naive
    # implementation would swap BACK to `current` (oscillation). The idempotent path
    # instead resolves to a no-op ONLY when the resolved target == current pointer. Here
    # the newest-prior-excluding-current is `current`, so we assert the pointer does NOT
    # oscillate back to `current`: it must STAY on `prior` OR (if the resolver returns
    # current) short-circuit. Either way the pointer must remain `prior`.
    r2 = rollback_dataset(conn, datastream_id=ds_id, project_id=project_id, actor="owner-1")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams WHERE id = %s",
            (ds_id,),
        )
        pointer_after_second = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM app.datastream_publication_log WHERE execution_id = %s",
            (prior,),
        )
        prior_log_rows_after_second = cur.fetchone()[0]

    # The pointer MUST NOT have oscillated back to `current`.
    assert pointer_after_second == prior, (
        f"pointer oscillated to {pointer_after_second} (expected to stay at {prior})"
    )
    # And the retry wrote NO additional log row for `prior` (stable no-op).
    assert prior_log_rows_after_second == prior_log_rows_after_first
    assert r2.get("already_at_target") is True
    conn.rollback()


@requires_postgres
def test_create_execution_serializes_against_held_datastream_lock(live_postgres) -> None:
    """M2 (Story 12.12): create_execution takes app.datastreams FOR UPDATE, so a replace
    that mints an execution BLOCKS while another transaction (a rollback in flight) holds
    that row lock -- strict serialization, not last-writer-wins. Proven with a second
    connection holding the lock + a short lock_timeout on the create.
    """
    import psycopg
    from core.datastream_publication import create_execution

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)

    holder = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        # Hold the datastream pointer lock in a SEPARATE transaction (rollback mid-flight).
        with holder.cursor() as hcur:
            hcur.execute(
                "SELECT 1 FROM app.datastreams WHERE id = %s AND project_id = %s FOR UPDATE",
                (ds_id, project_id),
            )
            hcur.fetchone()

        # create_execution on the main conn must WAIT on that lock -> lock_timeout.
        with conn.cursor() as scur:
            scur.execute("SET LOCAL lock_timeout = '500ms'")
        with pytest.raises(psycopg.errors.LockNotAvailable):
            create_execution(
                ds_id, project_id, plan_id, mapping_id,
                {"executable": True}, "user-1", "idem-m2", conn,
            )
        conn.rollback()

        # Release the holder -> the create now succeeds (serialized, not lost).
        holder.rollback()
        ex = create_execution(
            ds_id, project_id, plan_id, mapping_id,
            {"executable": True}, "user-1", "idem-m2b", conn,
        )
        assert ex["state"] == "created"
        conn.rollback()
    finally:
        holder.close()
