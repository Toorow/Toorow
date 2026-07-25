"""Live-Postgres test for warehouse_write org routing (Story 24.3, T7).

Skipped when TEST_POSTGRES_DSN is not set (CI pattern from isolation/).
Proves end-to-end that flag ON with a real Postgres DB resolves the org slug
and lands a raw_* table in the correct DuckDB schema.

Run manually:
    TEST_POSTGRES_DSN=postgresql://... TOOROW_ORG_SCHEMAS=1 pytest \
        server/tests/core/test_warehouse_write_pg.py -v
"""

from __future__ import annotations

import os
import uuid

import pytest

TEST_POSTGRES_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(
    not TEST_POSTGRES_DSN,
    reason="Set TEST_POSTGRES_DSN to run live-Postgres tests",
)


@pytest.fixture()
def pg_conn():
    import psycopg2  # type: ignore[import]

    conn = psycopg2.connect(TEST_POSTGRES_DSN)
    yield conn
    conn.close()


def test_open_raw_writer_resolves_real_org(tmp_path, pg_conn):
    """Flag ON + real Postgres: open_raw_writer routes to org_<wslug>_raw."""
    import duckdb
    from core import warehouse_tenancy, warehouse_write

    warehouse_write._reset_warn()
    warehouse_tenancy._reset_cache()

    # Resolve a real project to get its org slug
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, o.slug FROM app.projects p"
            " JOIN app.organizations o ON o.id = p.org_id"
            " LIMIT 1"
        )
        row = cur.fetchone()

    if row is None:
        pytest.skip("No project with an org found in Postgres -- skipping")

    project_id, org_slug = row
    wslug = org_slug.replace("-", "_")
    expected_schema = f"org_{wslug}_raw"

    db = str(tmp_path / "pg_live.duckdb")
    pull_id = f"pull-{uuid.uuid4().hex[:8]}"

    os.environ["TOOROW_ORG_SCHEMAS"] = "1"
    try:
        con = warehouse_write.open_raw_writer(db, project_id=str(project_id))
        con.execute(
            "CREATE TABLE IF NOT EXISTS raw_test_24_3 (pull_id VARCHAR, project_id VARCHAR)"
        )
        con.execute(
            "INSERT INTO raw_test_24_3 VALUES (?, ?)", [pull_id, str(project_id)]
        )
        con.close()

        # Verify table landed in the org schema
        con2 = duckdb.connect(db)
        count = con2.execute(
            f'SELECT COUNT(*) FROM "{expected_schema}".raw_test_24_3 WHERE pull_id = ?',
            [pull_id],
        ).fetchone()[0]
        con2.close()

        assert count == 1, (
            f"Expected 1 row in {expected_schema}.raw_test_24_3, got {count}"
        )
    finally:
        os.environ.pop("TOOROW_ORG_SCHEMAS", None)
        warehouse_tenancy._reset_cache()
