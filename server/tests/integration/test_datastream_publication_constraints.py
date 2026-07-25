"""Live-Postgres contract tests for the atomic candidate registry (Story 12.5).

These apply migration 042 (idempotently, after 030/032) and exercise the REAL
constraints a mocked cursor cannot catch: the append-only publication-log trigger,
the atomic 4-write commit_publication transaction, the pointer swap, the rollback
leaving the prior pointer intact, and the concurrent-execution structural guard.

They SKIP when TEST_POSTGRES_DSN is unset. The migration itself is applied to
Supabase only under Jean's authorization; these tests apply it to the disposable
test database referenced by TEST_POSTGRES_DSN.
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

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)

# A fixed valid ULID body (Crockford base32) reused where the exact value is
# irrelevant; uniqueness comes from swapping the trailing char.
_ULID_SAMPLE = "01J8ZC4Q0N7R2K3W5X6Y7Z8A9B"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _dse(index: int) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    return "dse_" + _ULID_SAMPLE[:-1] + alphabet[index % len(alphabet)]


def _apply_migrations(conn) -> None:
    with conn.cursor() as cur:
        for path in (INTENT_MIGRATION, MAPPING_MIGRATION, REGISTRY_MIGRATION):
            cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _seed(conn, project_id: str, ds_id: str, plan_id: str, mapping_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by)
            VALUES (%s, %s, %s, 'story-12.5-test')
            """,
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


def _insert_execution(
    conn, exec_id: str, ds_id: str, project_id: str, plan_id: str, mapping_id: str,
    state: str = "ready", *, content_hash: str | None = None, row_count: int | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_executions
                (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                 projection_plan_ref, state, content_hash, row_count, created_by)
            VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, %s, %s, 'test')
            """,
            (exec_id, ds_id, project_id, plan_id, mapping_id, state, content_hash, row_count),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Migration text assertions (run without Postgres).
# ---------------------------------------------------------------------------


def test_migration_042_declares_registry_and_append_only_trigger() -> None:
    sql = REGISTRY_MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app.datastream_executions" in sql
    assert "CREATE TABLE IF NOT EXISTS app.datastream_publication_log" in sql
    assert "CREATE TABLE IF NOT EXISTS app.datastream_outbox" in sql
    assert "current_published_execution_id" in sql
    assert "CREATE TRIGGER trg_datastream_publication_log_immutable" in sql
    assert "BEFORE UPDATE OR DELETE ON app.datastream_publication_log" in sql
    assert "app.reject_publication_log_mutation" in sql
    # Concurrent-execution structural guard + idempotency partial-unique index.
    assert "uq_datastream_executions_active" in sql
    assert "uq_datastream_executions_idempotency" in sql
    # DQ-gate preference columns (additive).
    assert "max_row_count_delta_pct" in sql
    assert "allow_empty_publication" in sql


# ---------------------------------------------------------------------------
# Live-Postgres constraint tests.
# ---------------------------------------------------------------------------


@requires_postgres
def test_publication_log_is_append_only(live_postgres) -> None:
    import psycopg

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    exec_id = _dse(1)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    _insert_execution(
        conn, exec_id, ds_id, project_id, plan_id, mapping_id,
        state="published", content_hash="a" * 64, row_count=100,
    )
    log_id = "dplog_" + _ULID_SAMPLE
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_publication_log
                (id, execution_id, datastream_id, project_id, plan_version_id,
                 mapping_version_id, content_hash, row_count, published_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'test')
            """,
            (log_id, exec_id, ds_id, project_id, plan_id, mapping_id, "a" * 64, 100),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.datastream_publication_log SET row_count = 999 WHERE id = %s",
                (log_id,),
            )
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp")
        cur.execute("SAVEPOINT sp2")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.datastream_publication_log WHERE id = %s", (log_id,))
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp2")


@requires_postgres
def test_commit_publication_atomic_pointer_swap(live_postgres) -> None:
    from core.datastream_publication import commit_publication

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    exec_id = _dse(2)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    _insert_execution(
        conn, exec_id, ds_id, project_id, plan_id, mapping_id,
        state="ready", content_hash="a" * 64, row_count=100,
    )

    # commit_publication opens its own out-of-band connection only on failure; the
    # happy path commits `conn`. We pass a factory that reuses the same DSN.
    dsn = os.environ["TEST_POSTGRES_DSN"]

    class _Factory:
        def __call__(self):
            import psycopg
            return psycopg.connect(dsn)

    result = commit_publication(exec_id, project_id, "user-1", conn, connection_factory=_Factory())
    assert result["execution"]["state"] == "published"

    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams WHERE id = %s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == exec_id
        cur.execute(
            "SELECT row_count FROM app.datastream_publication_log WHERE execution_id = %s",
            (exec_id,),
        )
        assert cur.fetchone()[0] == 100
        cur.execute(
            "SELECT event_type FROM app.datastream_outbox WHERE execution_id = %s",
            (exec_id,),
        )
        assert cur.fetchone()[0] == "published"
    conn.rollback()  # leave the shared test DB clean


@requires_postgres
def test_commit_publication_rollback_between_log_and_pointer_leaves_state_intact(
    live_postgres,
) -> None:
    """Real-Postgres rollback proof (FIX 7).

    Inject a failure BETWEEN the append-only log INSERT and the pointer-swap UPDATE
    inside commit_publication, then assert the REAL post-rollback Postgres state:
      * app.datastreams.current_published_execution_id UNCHANGED (prior value),
      * ZERO new app.datastream_publication_log rows,
      * ZERO new app.datastream_outbox rows.

    A cursor proxy raises on the pointer-swap UPDATE (which fires strictly AFTER the
    log INSERT), simulating a mid-transaction DB error at exactly that seam. The
    out-of-band failure write runs on a SEPARATE connection (the factory), so the
    rolled-back primary connection is never reused for the failure mark.
    """
    import psycopg

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    exec_id = _dse(5)
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    _insert_execution(
        conn, exec_id, ds_id, project_id, plan_id, mapping_id,
        state="ready", content_hash="a" * 64, row_count=100,
    )
    # The prior published pointer is NULL (no prior publication); capture it.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams WHERE id = %s",
            (ds_id,),
        )
        prior_pointer = cur.fetchone()[0]
    conn.commit()

    from core.datastream_publication import commit_publication

    dsn = os.environ["TEST_POSTGRES_DSN"]

    class _Factory:
        def __call__(self):
            return psycopg.connect(dsn)

    class _RaisingCursor:
        """Wrap a real cursor; raise on the pointer-swap UPDATE only."""

        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

        def execute(self, sql, params=None):
            normalized = " ".join(sql.split())
            if normalized.startswith(
                "UPDATE app.datastreams SET current_published_execution_id"
            ):
                # Fire strictly AFTER the log INSERT, strictly BEFORE the swap lands.
                raise RuntimeError("injected failure between log-insert and pointer-swap")
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    class _ProxyConn:
        def __init__(self, inner):
            self._inner = inner

        def cursor(self, *a, **k):
            return _RaisingCursor(self._inner.cursor(*a, **k))

        def commit(self):
            return self._inner.commit()

        def rollback(self):
            return self._inner.rollback()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    proxy = _ProxyConn(conn)
    with pytest.raises(RuntimeError, match="injected failure between log-insert"):
        commit_publication(exec_id, project_id, "user-1", proxy, connection_factory=_Factory())

    # The real transaction rolled back; assert the real post-rollback DB state on a
    # fresh view of the same connection.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams WHERE id = %s",
            (ds_id,),
        )
        assert cur.fetchone()[0] == prior_pointer  # UNCHANGED
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_publication_log WHERE execution_id = %s",
            (exec_id,),
        )
        assert cur.fetchone()[0] == 0  # ZERO new log rows
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_outbox WHERE execution_id = %s",
            (exec_id,),
        )
        assert cur.fetchone()[0] == 0  # ZERO new outbox rows
        # The execution was failed OUT OF BAND on the separate connection.
        cur.execute(
            "SELECT state FROM app.datastream_executions WHERE id = %s",
            (exec_id,),
        )
        assert cur.fetchone()[0] == "failed"
    conn.rollback()


@requires_postgres
def test_concurrent_active_execution_blocked_by_partial_unique(live_postgres) -> None:
    import psycopg

    conn = live_postgres
    _apply_migrations(conn)
    project_id, ds_id = _id("proj_"), _id("ds_")
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    _seed(conn, project_id, ds_id, plan_id, mapping_id)
    _insert_execution(conn, _dse(3), ds_id, project_id, plan_id, mapping_id, state="loading")

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_execution(conn, _dse(4), ds_id, project_id, plan_id, mapping_id, state="validating")
    with conn.cursor() as cur:
        cur.execute("ROLLBACK TO SAVEPOINT sp")
