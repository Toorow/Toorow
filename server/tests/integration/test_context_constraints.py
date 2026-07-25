"""Database contract tests for context layer tables and triggers (Story 11.1).

Fix-pass additions:
- Tightened trigger tests: catch ONLY psycopg.errors.RaiseException + assert pgcode 'P0001' (Fix 6).
- Added TRUNCATE gate tests for context_topics_versions and procedures_versions (Fix 1).
- Added concurrent-UPDATE test proving sequential version_numbers under two connections (Fix 6).
- Added archived-name reuse test proving a new procedure can reuse an archived name (Fix 2).
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra" / "nango" / "migrations" / "031_context_layer.sql"
requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def test_migration_031_file_declaration() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app.context_topics" in sql
    assert "CREATE TABLE IF NOT EXISTS app.procedures" in sql
    assert "CREATE TABLE IF NOT EXISTS app.context_graph" in sql
    assert "CREATE TABLE IF NOT EXISTS app.schema_context" in sql
    assert "CREATE TABLE IF NOT EXISTS app.context_topics_versions" in sql
    assert "CREATE TABLE IF NOT EXISTS app.procedures_versions" in sql
    assert "trg_context_topics_versions_immutable" in sql
    assert "trg_procedures_versions_immutable" in sql
    # Fix 1: TRUNCATE triggers must be declared in the migration
    assert "trg_context_topics_versions_no_truncate" in sql
    assert "trg_procedures_versions_no_truncate" in sql
    assert "uq_schema_context_key UNIQUE (project_id, relation, doc_kind)" in sql


@requires_postgres
def test_live_postgres_context_triggers_and_constraints(live_postgres) -> None:
    import psycopg
    import psycopg.errors

    conn = live_postgres
    topic_id = _id("top_")
    proc_id = _id("proc_")

    with conn.cursor() as cur:
        # Apply migration DDL
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()

    with conn.cursor() as cur:
        # Insert initial topic version row
        cur.execute(
            """
            INSERT INTO app.context_topics_versions
                (topic_id, project_id, title, body_md, status, created_by, created_at, updated_at,
                 version_number, changed_by, changed_at)
            VALUES
                (%s, 'proj_test', 'Title', 'body', 'active', 'test', now(), now(), 1, 'test', now())
            """,
            (topic_id,),
        )

        # Insert initial procedure version row
        cur.execute(
            """
            INSERT INTO app.procedures_versions
                (procedure_id, project_id, name, description, frontmatter_yaml, body_md, status,
                 created_by, created_at, updated_at, version_number, changed_by, changed_at)
            VALUES
                (
                    %s, 'proj_test', 'proc_name', 'desc',
                    'name: proc_name\ndescription: desc', 'body', 'active',
                    'test', now(), now(), 1, 'test', now()
                )
            """,
            (proc_id,),
        )
    conn.commit()

    # 1. Trigger rejects UPDATE on context_topics_versions -- tightened to exact pgcode (Fix 6).
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException) as exc_info:
            cur.execute(
                "UPDATE app.context_topics_versions SET title = 'New Title' WHERE topic_id = %s",
                (topic_id,),
            )
    assert exc_info.value.pgcode == "P0001"
    conn.rollback()

    # 2. Trigger rejects DELETE on procedures_versions -- tightened to exact pgcode (Fix 6).
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException) as exc_info:
            cur.execute(
                "DELETE FROM app.procedures_versions WHERE procedure_id = %s",
                (proc_id,),
            )
    assert exc_info.value.pgcode == "P0001"
    conn.rollback()

    # 3. schema_context UNIQUE on (project_id, relation, doc_kind)
    sctx_id1 = _id("sctx_")
    sctx_id2 = _id("sctx_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.schema_context
                (id, project_id, relation, doc_kind, body_md, generated_at)
            VALUES
                (%s, 'proj_test', 'stg_meta_ads', 'columns', 'doc md', now())
            """,
            (sctx_id1,),
        )
    conn.commit()

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                """
                INSERT INTO app.schema_context
                    (id, project_id, relation, doc_kind, body_md, generated_at)
                VALUES
                    (%s, 'proj_test', 'stg_meta_ads', 'columns', 'dup md', now())
                """,
                (sctx_id2,),
            )
    conn.rollback()

    # 4. context_graph CHECK on invalid from_type
    edge_id = _id("edge_")
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                """
                INSERT INTO app.context_graph
                    (id, from_id, from_type, to_id, to_type, edge_type, created_by)
                VALUES
                    (%s, 'from_1', 'invalid_kind', 'to_1', 'topic', 'relates_to', 'test')
                """,
                (edge_id,),
            )
    conn.rollback()

    # 5. procedures name UNIQUE constraint (active rows only)
    proc_id1 = _id("proc_")
    proc_id2 = _id("proc_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.procedures (id, project_id, name, frontmatter_yaml, created_by)
            VALUES (%s, 'proj_test', 'unique_proc', 'name: unique_proc\ndescription: d', 'test')
            """,
            (proc_id1,),
        )
    conn.commit()

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                """
                INSERT INTO app.procedures (id, project_id, name, frontmatter_yaml, created_by)
                VALUES (%s, 'proj_test', 'unique_proc', 'name: unique_proc\ndescription: d', 'test')
                """,
                (proc_id2,),
            )
    conn.rollback()

    # 6. context_graph UNIQUE edge index
    edge_id1 = _id("edge_")
    edge_id2 = _id("edge_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.context_graph
                (id, from_id, from_type, to_id, to_type, edge_type, created_by)
            VALUES
                (%s, 't_1', 'topic', 'p_1', 'procedure', 'relates_to', 'test')
            """,
            (edge_id1,),
        )
    conn.commit()

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                """
                INSERT INTO app.context_graph
                    (id, from_id, from_type, to_id, to_type, edge_type, created_by)
                VALUES
                    (%s, 't_1', 'topic', 'p_1', 'procedure', 'relates_to', 'test')
                """,
                (edge_id2,),
            )
    conn.rollback()


@requires_postgres
def test_truncate_blocked_on_context_topics_versions(live_postgres) -> None:
    """TRUNCATE on context_topics_versions must raise (Fix 1: BEFORE TRUNCATE trigger)."""
    import psycopg.errors

    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException) as exc_info:
            cur.execute("TRUNCATE app.context_topics_versions")
    assert exc_info.value.pgcode == "P0001"
    conn.rollback()


@requires_postgres
def test_truncate_blocked_on_procedures_versions(live_postgres) -> None:
    """TRUNCATE on procedures_versions must raise (Fix 1: BEFORE TRUNCATE trigger)."""
    import psycopg.errors

    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException) as exc_info:
            cur.execute("TRUNCATE app.procedures_versions")
    assert exc_info.value.pgcode == "P0001"
    conn.rollback()


@requires_postgres
def test_archived_name_reuse_allowed(live_postgres) -> None:
    """Archive a procedure then create a new one with the same name → 201 (Fix 2).

    Proves that partial unique index (WHERE status != 'archived') allows name reuse.
    """
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()

    proc_id1 = _id("proc_")
    proc_id2 = _id("proc_")
    name = f"reusable_proc_{uuid.uuid4().hex[:8]}"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.procedures (id, project_id, name, frontmatter_yaml, created_by, status)
            VALUES (%s, 'proj_arc', %s, 'name: x\ndescription: d', 'test', 'archived')
            """,
            (proc_id1, name),
        )
    conn.commit()

    # Creating a new active procedure with the same name in same scope must succeed.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.procedures (id, project_id, name, frontmatter_yaml, created_by, status)
            VALUES (%s, 'proj_arc', %s, 'name: x\ndescription: d', 'test', 'active')
            """,
            (proc_id2, name),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.procedures WHERE name = %s AND project_id = 'proj_arc'",
            (name,),
        )
        rows = cur.fetchall()
    assert len(rows) == 2  # archived + active


@requires_postgres
def test_concurrent_updates_produce_sequential_version_numbers(live_postgres) -> None:
    """Two concurrent connections updating the same topic produce
    sequential version_numbers (Fix 6)."""
    import psycopg

    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()

    dsn = os.environ["TEST_POSTGRES_DSN"]
    topic_id = _id("top_conc_")

    # Insert a base topic row.
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.context_topics (id, project_id, title, body_md, status, created_by)
            VALUES (%s, 'proj_conc', 'Concurrent Topic', '', 'active', 'test')
            """,
            (topic_id,),
        )
        cur.execute(
            """
            INSERT INTO app.context_topics_versions
                (topic_id, project_id, title, body_md, status, created_by, created_at, updated_at,
                 version_number, changed_by, changed_at)
            VALUES (%s, 'proj_conc', 'Concurrent Topic', '', 'active', 'test',
                    now(), now(), 1, 'test', now())
            """,
            (topic_id,),
        )
    conn.commit()

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def update_in_thread(n: int) -> None:
        try:
            with psycopg.connect(dsn) as c2:
                barrier.wait()
                with c2.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM app.context_topics WHERE id = %s FOR UPDATE",
                        (topic_id,),
                    )
                    cur.execute(
                        "SELECT COALESCE(MAX(version_number), 0)"
                        " FROM app.context_topics_versions WHERE topic_id = %s",
                        (topic_id,),
                    )
                    max_ver = cur.fetchone()[0]
                    next_ver = max_ver + 1
                    cur.execute(
                        """
                        INSERT INTO app.context_topics_versions
                            (topic_id, project_id, title, body_md, status, created_by,
                             created_at, updated_at, version_number, changed_by, changed_at)
                        VALUES (%s, 'proj_conc', %s, '', 'active', 'test',
                                now(), now(), %s, 'test', now())
                        """,
                        (topic_id, f"Update {n}", next_ver),
                    )
                c2.commit()
        except Exception as exc:
            errors.append(exc)

    t1 = threading.Thread(target=update_in_thread, args=(1,))
    t2 = threading.Thread(target=update_in_thread, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Concurrent update errors: {errors}"

    # Verify version_numbers are sequential: 1, 2, 3 (create + 2 updates).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_number FROM app.context_topics_versions"
            " WHERE topic_id = %s ORDER BY version_number",
            (topic_id,),
        )
        versions = [row[0] for row in cur.fetchall()]
    assert versions == [1, 2, 3], f"Expected [1, 2, 3], got {versions}"
