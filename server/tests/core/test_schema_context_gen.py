"""Unit, seam, and live-Postgres tests for Schema Context Generator (Story 11.2).

Fix pass (2026-07-20, 3-reviewer REJECT):
  - Re-homed REST route to core.schema_context_api (ADMIN-only gate); the seam
    test targets the new route and asserts 200.
  - Historisation: live-Postgres test proving change => new version, no-change => 0.
  - Idempotence: real DuckDB double-run test asserting second run updated == 0.
  - AD-17 both-directions: generator writes only schema_context; structural check
    that no REST/CRUD route targets schema_context.
  - SQL identifier injection: unsafe relation/column names are rejected.
  - Allowlist: an explicit empty list means "nothing allowed".
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import duckdb
import pytest
from core.main import build_asgi_app
from core.schema_context_gen import (
    UnsafeIdentifierError,
    _quote_ident,
    build_columns_markdown,
    build_description_markdown,
    build_preview_markdown,
    generate_schema_context,
    inspect_relation_schema,
    is_pii_column,
    is_relation_allowed,
    profile_relation_stats,
    upsert_schema_context_doc,
)
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra" / "nango" / "migrations" / "031_context_layer.sql"

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres schema-context test skipped",
)


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def test_is_pii_column():
    assert is_pii_column("user_email") is True
    assert is_pii_column("auth_token") is True
    assert is_pii_column("user_password") is True
    assert is_pii_column("phone_number") is True
    assert is_pii_column("created_at") is False
    assert is_pii_column("total_revenue") is False
    # LOW fix: extended PII patterns.
    assert is_pii_column("stripe_api_key") is True
    assert is_pii_column("rsa_private_key") is True
    assert is_pii_column("card_number") is True
    assert is_pii_column("date_of_dob") is True
    assert is_pii_column("birth_date") is True


def test_is_relation_allowed():
    assert is_relation_allowed("stg_ga4_events") is True
    assert is_relation_allowed("mart_revenue_daily") is True
    assert is_relation_allowed("dim_customers") is True
    assert is_relation_allowed("fct_orders") is True
    assert is_relation_allowed("raw_users", allowlist=["raw_users"]) is True
    assert is_relation_allowed("raw_secrets") is False


def test_is_relation_allowed_explicit_empty_list_denies_everything():
    """LOW fix: an EXPLICIT empty allowlist means 'nothing allowed'.

    Previously ``if allowlist:`` treated [] as falsy and fell through to the
    default stg_/mart_ patterns -- so an operator passing [] to mean 'block all'
    would still profile stg_/mart_ tables. The guard is now ``allowlist is None``.
    """
    assert is_relation_allowed("stg_ga4_events", allowlist=[]) is False
    assert is_relation_allowed("mart_revenue", allowlist=[]) is False
    # None (nothing supplied) still uses the default safe patterns.
    assert is_relation_allowed("stg_ga4_events", allowlist=None) is True


def test_quote_ident_rejects_injection():
    """HIGH fix: unsafe SQL identifiers are rejected before interpolation."""
    assert _quote_ident("stg_orders") == '"stg_orders"'
    assert _quote_ident("_x1") == '"_x1"'
    for bad in ("stg orders", "a;DROP TABLE x", 'a"b', "1abc", "", "a-b", "a.b"):
        with pytest.raises(UnsafeIdentifierError):
            _quote_ident(bad)


def test_inspect_relation_schema_rejects_unsafe_relation():
    db = duckdb.connect(":memory:")
    with pytest.raises(UnsafeIdentifierError):
        inspect_relation_schema(db, "a; DROP TABLE t; --")


def test_duckdb_schema_introspection_and_profiling():
    db = duckdb.connect(":memory:")
    db.execute(
        """
        CREATE TABLE stg_test (
            id INTEGER,
            user_email VARCHAR,
            amount DOUBLE,
            created_at TIMESTAMP
        );
        INSERT INTO stg_test VALUES
            (1, 'alice@example.com', 10.5, '2026-01-01 10:00:00'),
            (2, 'bob@example.com', 20.0, '2026-01-02 11:00:00');
        """
    )

    cols = inspect_relation_schema(db, "stg_test")
    assert len(cols) == 4
    col_names = [c["name"] for c in cols]
    assert "user_email" in col_names
    assert "amount" in col_names

    stats = profile_relation_stats(db, "stg_test", cols)
    assert stats["row_count"] == 2
    assert stats["column_stats"]["user_email"]["pii"] is True
    assert stats["column_stats"]["amount"]["min"] == 10.5
    assert stats["column_stats"]["amount"]["max"] == 20.0

    cols_md = build_columns_markdown(cols, stats)
    assert "Schema Columns" in cols_md
    assert "PII (Redacted)" in cols_md

    desc_md = build_description_markdown("stg_test", 2)
    assert "Warehouse relation `stg_test`" in desc_md
    assert "Row Count:** 2" in desc_md

    prev_md = build_preview_markdown(db, "stg_test", cols, limit=5)
    assert "Preview: stg_test" in prev_md
    assert "[REDACTED]" in prev_md
    assert "alice@example.com" not in prev_md


def test_build_preview_markdown_no_exception_text_leak():
    """MEDIUM fix: on a read error, exception text is NOT embedded into body_md."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.execute.side_effect = RuntimeError("SECRET-DETAIL-boom")

    md = build_preview_markdown(conn, "stg_test", [{"name": "id"}], limit=5)
    assert "SECRET-DETAIL-boom" not in md
    assert "_No sample rows available._" in md


def test_upsert_schema_context_doc_idempotency():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    # First call: no existing record -> returns True (inserted)
    cur.fetchone.return_value = None
    res1 = upsert_schema_context_doc(
        conn, project_id="proj_A", relation="stg_test", doc_kind="columns", body_md="# Columns"
    )
    assert res1 is True

    # Second call: identical body -> returns False (skipped, no version)
    cur.fetchone.return_value = ("sctx_123", "# Columns", "2026-01-01")
    res2 = upsert_schema_context_doc(
        conn, project_id="proj_A", relation="stg_test", doc_kind="columns", body_md="# Columns"
    )
    assert res2 is False

    # Third call: modified body -> returns True (updated + version appended).
    # fetchone is called twice on the update path: first the SELECT (existing row),
    # then MAX(version_number). Use side_effect to model both reads.
    cur.fetchone.side_effect = [("sctx_123", "# Old Columns", "2026-01-01"), (0,)]
    res3 = upsert_schema_context_doc(
        conn, project_id="proj_A", relation="stg_test", doc_kind="columns", body_md="# New Columns"
    )
    assert res3 is True
    # A version INSERT into app.schema_context_versions must have occurred.
    version_inserts = [
        c for c in cur.execute.call_args_list
        if "app.schema_context_versions" in str(c.args[0]) and "INSERT" in str(c.args[0])
    ]
    assert len(version_inserts) == 1


def test_generate_schema_context_pipeline():
    db = duckdb.connect(":memory:")
    db.execute(
        """
        CREATE TABLE stg_orders (id INT, total DOUBLE);
        INSERT INTO stg_orders VALUES (101, 49.99);
        """
    )

    pg_conn = MagicMock()
    cur = MagicMock()
    pg_conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = None  # New doc insertion

    res = generate_schema_context(
        pg_conn, project_id="proj_A", warehouse_conn=db, allowlist=["stg_orders"]
    )
    assert res["processed"] == 1
    assert res["updated"] == 1
    assert res["skipped"] == 0
    assert len(res["errors"]) == 0

    # LOW fix: exactly 3 doc_kind rows are written for the single relation.
    def _norm(sql):
        return " ".join(str(sql).split())

    doc_inserts = [
        c for c in cur.execute.call_args_list
        if _norm(c.args[0]).startswith("INSERT INTO app.schema_context (")
    ]
    assert len(doc_inserts) == 3
    written_kinds = sorted(c.args[1][3] for c in doc_inserts)
    assert written_kinds == ["columns", "description", "preview"]


def test_generate_schema_context_double_run_is_idempotent():
    """CRITICAL fix (idempotence): a real double-run over an unchanged DuckDB
    relation produces 0 updates on the second pass (byte-identical preview via
    ORDER BY 1). Uses an in-process dict-backed fake Postgres 'schema_context'."""
    db = duckdb.connect(":memory:")
    db.execute(
        """
        CREATE TABLE stg_double (id INT, region VARCHAR, amount DOUBLE);
        INSERT INTO stg_double VALUES
            (3, 'eu', 30.0), (1, 'us', 10.0), (2, 'apac', 20.0);
        """
    )

    store: dict[tuple, str] = {}

    class _FakeCursor:
        def __init__(self):
            self._result = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=()):
            s = " ".join(sql.split())
            if s.startswith("SELECT id, body_md, generated_at FROM app.schema_context"):
                key = (params[0], params[1], params[2])
                if key in store:
                    self._result = ("sctx_x", store[key], "t0")
                else:
                    self._result = None
            elif s.startswith("INSERT INTO app.schema_context ("):
                key = (params[1], params[2], params[3])
                store[key] = params[4]
                self._result = None
            elif s.startswith("UPDATE app.schema_context"):
                # params: (body_md, id) -- update by re-finding the key is not
                # needed here; the double-run test never reaches UPDATE on run 2.
                self._result = None
            elif "SAVEPOINT" in s or "RELEASE" in s or "ROLLBACK" in s:
                self._result = None
            else:
                self._result = None

        def fetchone(self):
            return self._result

        def fetchall(self):
            return []

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self):
            pass

    conn = _FakeConn()

    res1 = generate_schema_context(
        conn, project_id="proj_D", warehouse_conn=db, allowlist=["stg_double"]
    )
    assert res1["updated"] == 1

    res2 = generate_schema_context(
        conn, project_id="proj_D", warehouse_conn=db, allowlist=["stg_double"]
    )
    assert res2["updated"] == 0
    assert res2["skipped"] == 1


# ---------------------------------------------------------------------------
# AD-17 both-directions (HIGH fix)
# ---------------------------------------------------------------------------


def test_ad17_generator_writes_only_schema_context():
    """The generator's Postgres writes target ONLY app.schema_context and its
    append-only history table -- never topics/procedures/graph."""
    db = duckdb.connect(":memory:")
    db.execute("CREATE TABLE stg_ad17 (id INT); INSERT INTO stg_ad17 VALUES (1);")

    pg_conn = MagicMock()
    cur = MagicMock()
    pg_conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = None

    generate_schema_context(
        pg_conn, project_id="proj_A", warehouse_conn=db, allowlist=["stg_ad17"]
    )

    writes = [
        str(c.args[0])
        for c in cur.execute.call_args_list
        if any(kw in str(c.args[0]) for kw in ("INSERT", "UPDATE", "DELETE"))
    ]
    assert writes, "Expected at least one write"
    forbidden = ("context_topics", "app.procedures", "context_graph")
    for w in writes:
        assert "app.schema_context" in w
        for f in forbidden:
            assert f not in w, f"Generator must not write {f}: {w}"


def test_ad17_no_rest_route_writes_schema_context():
    """Structural AD-17: no REST/CRUD handler mutates app.schema_context except
    the single-writer generator. Scans core.schema_context_api and core.context_api
    source for direct INSERT/UPDATE/DELETE against app.schema_context."""
    import inspect

    from core import context_api, schema_context_api

    for mod in (context_api, schema_context_api):
        src = inspect.getsource(mod)
        # The only reference to schema_context in schema_context_api is a call to
        # the generator (generate_schema_context) -- never inline DML.
        for verb in ("INSERT INTO app.schema_context", "UPDATE app.schema_context",
                     "DELETE FROM app.schema_context"):
            assert verb not in src, (
                f"{mod.__name__} must not perform inline DML on schema_context ({verb})"
            )


# ---------------------------------------------------------------------------
# REST seam (CRITICAL fix): re-homed route + ADMIN-only gate.
# ---------------------------------------------------------------------------


def test_admin_api_generate_schema_context_seam():
    """The re-homed ADMIN-only route returns 200 for an authorised admin."""
    app = build_asgi_app()
    client = TestClient(app)

    db = duckdb.connect(":memory:")
    db.execute("CREATE TABLE mart_sales (id INT);")

    pg_conn = MagicMock()
    cur = MagicMock()
    # Proper context-manager mock for get_connection() (used via `with ... as conn`).
    pg_conn.__enter__.return_value = pg_conn
    pg_conn.__exit__.return_value = False
    pg_conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = None

    # Proper context-manager mock for get_warehouse_connection().
    wh_cm = MagicMock()
    wh_cm.__enter__.return_value = db
    wh_cm.__exit__.return_value = False

    with (
        patch("core.schema_context_api._check_auth", return_value=(True, "admin_user")),
        patch("core.schema_context_api._check_admin_authorized", return_value=True),
        patch("core.db.get_connection", return_value=pg_conn),
        patch("core.db.get_warehouse_connection", return_value=wh_cm),
    ):
        resp = client.post(
            "/api/admin/context/generate-schema-context",
            json={"project_id": "proj_A", "allowlist": ["mart_sales"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["processed"] == 1
        assert data["updated"] == 1


def test_admin_api_generate_schema_context_member_denied():
    """A non-admin (member) identity is refused with 403 (DoS guard)."""
    app = build_asgi_app()
    client = TestClient(app)

    pg_conn = MagicMock()
    pg_conn.__enter__.return_value = pg_conn
    pg_conn.__exit__.return_value = False

    with (
        patch("core.schema_context_api._check_auth", return_value=(True, "member_user")),
        patch("core.schema_context_api._check_admin_authorized", return_value=False),
        patch("core.db.get_connection", return_value=pg_conn),
    ):
        resp = client.post(
            "/api/admin/context/generate-schema-context",
            json={"project_id": "proj_A", "allowlist": ["mart_sales"]},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Migration declaration + live-Postgres historisation (Jean's decision)
# ---------------------------------------------------------------------------


def test_migration_031_declares_schema_context_versions():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app.schema_context_versions" in sql
    assert "trg_schema_context_versions_immutable" in sql
    assert "trg_schema_context_versions_no_truncate" in sql


@requires_postgres
def test_live_postgres_schema_context_historisation(live_postgres):
    """Change => new version row; no-change => zero version rows (idempotent)."""
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()

    project_id = _id("proj_")
    relation = "stg_hist_test"

    def _count_versions() -> int:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.schema_context_versions "
                "WHERE project_id = %s AND relation = %s",
                (project_id, relation),
            )
            return cur.fetchone()[0]

    # 1. Initial insert: no version row (nothing to historise yet).
    assert upsert_schema_context_doc(
        conn, project_id=project_id, relation=relation,
        doc_kind="columns", body_md="# v1",
    ) is True
    conn.commit()
    assert _count_versions() == 0

    # 2. Identical body: no update, no version.
    assert upsert_schema_context_doc(
        conn, project_id=project_id, relation=relation,
        doc_kind="columns", body_md="# v1",
    ) is False
    conn.commit()
    assert _count_versions() == 0

    # 3. Changed body: update + exactly one version row (the PREVIOUS body).
    assert upsert_schema_context_doc(
        conn, project_id=project_id, relation=relation,
        doc_kind="columns", body_md="# v2",
    ) is True
    conn.commit()
    assert _count_versions() == 1

    with conn.cursor() as cur:
        cur.execute(
            "SELECT body_md, version_number FROM app.schema_context_versions "
            "WHERE project_id = %s AND relation = %s",
            (project_id, relation),
        )
        row = cur.fetchone()
    assert row[0] == "# v1"  # the PREVIOUS body was historised
    assert row[1] == 1

    # 4. Another change: second version row.
    assert upsert_schema_context_doc(
        conn, project_id=project_id, relation=relation,
        doc_kind="columns", body_md="# v3",
    ) is True
    conn.commit()
    assert _count_versions() == 2


@requires_postgres
def test_live_postgres_schema_context_versions_immutable(live_postgres):
    """schema_context_versions is append-only: UPDATE/DELETE/TRUNCATE blocked."""
    import psycopg.errors

    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(MIGRATION.read_text(encoding="utf-8"))
    conn.commit()

    sctx_id = _id("sctx_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.schema_context_versions
                (schema_context_id, project_id, relation, doc_kind, body_md,
                 generated_at, version_number, changed_by)
            VALUES (%s, 'proj_imm', 'stg_x', 'columns', 'body', now(), 1, 'test')
            """,
            (sctx_id,),
        )
    conn.commit()

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException) as exc_info:
            cur.execute(
                "UPDATE app.schema_context_versions SET body_md = 'x' "
                "WHERE schema_context_id = %s",
                (sctx_id,),
            )
    assert exc_info.value.pgcode == "P0001"
    conn.rollback()

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException) as exc_info:
            cur.execute(
                "DELETE FROM app.schema_context_versions WHERE schema_context_id = %s",
                (sctx_id,),
            )
    assert exc_info.value.pgcode == "P0001"
    conn.rollback()

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.RaiseException) as exc_info:
            cur.execute("TRUNCATE app.schema_context_versions")
    assert exc_info.value.pgcode == "P0001"
    conn.rollback()
