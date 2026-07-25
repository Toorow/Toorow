"""AC7 runtime invariant: all raw_* tables live in an org_* schema (Story 24.3, T5).

Tests that, after writes via open_raw_writer with flag ON, no raw_* table exists
in ``main`` (or any non-org_* schema).  Also proves the detection works in both
directions: adding a ``main.raw_x`` makes the check fail.

These tests are fully OFFLINE (tmp DuckDB, mocked resolve).
"""

from __future__ import annotations

from unittest.mock import patch

import duckdb


def _make_schemas(slug: str = "acme"):
    from core.warehouse_tenancy import OrgSchemas

    wslug = slug.replace("-", "_")
    return OrgSchemas(
        org_id="org-1",
        org_slug=slug,
        warehouse_slug=wslug,
        raw=f"org_{wslug}_raw",
        marts=f"org_{wslug}_marts",
    )


def _raw_tables_outside_org(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Return fully-qualified names of raw_* tables NOT in an org_* schema."""
    rows = con.execute(
        "SELECT table_schema, table_name FROM information_schema.tables"
        " WHERE table_name LIKE 'raw_%'"
        "   AND table_schema NOT LIKE 'org_%'"
    ).fetchall()
    return [f"{r[0]}.{r[1]}" for r in rows]


# ---------------------------------------------------------------------------
# Positive path: flag ON writes land in org schema only
# ---------------------------------------------------------------------------

def test_no_raw_tables_in_main_after_org_write(tmp_path):
    """Flag ON: after writing raw_* via open_raw_writer, no raw_* in main."""
    from core import warehouse_tenancy, warehouse_write

    warehouse_write._reset_warn()
    db = str(tmp_path / "ac7.duckdb")
    schemas = _make_schemas("myorg")

    with patch.object(
        warehouse_tenancy, "org_schemas_enabled", return_value=True
    ), patch.object(
        warehouse_tenancy, "resolve_org_schemas", return_value=schemas
    ):
        con = warehouse_write.open_raw_writer(db, project_id="proj-1")
        con.execute(
            "CREATE TABLE IF NOT EXISTS raw_ga4_standard_daily (date VARCHAR, pull_id VARCHAR)"
        )
        con.execute("INSERT INTO raw_ga4_standard_daily VALUES (?, ?)", ["2024-01-01", "pull-abc"])
        con.close()

    # Verify invariant
    con2 = duckdb.connect(db)
    violations = _raw_tables_outside_org(con2)
    con2.close()

    assert violations == [], (
        f"Invariant violated: raw_* tables found outside org_* schema: {violations}"
    )


def test_raw_table_in_org_schema_readable(tmp_path):
    """The org-schema raw table is readable by its qualified name."""
    from core import warehouse_tenancy, warehouse_write

    warehouse_write._reset_warn()
    db = str(tmp_path / "ac7b.duckdb")
    schemas = _make_schemas("readable")

    with patch.object(
        warehouse_tenancy, "org_schemas_enabled", return_value=True
    ), patch.object(
        warehouse_tenancy, "resolve_org_schemas", return_value=schemas
    ):
        con = warehouse_write.open_raw_writer(db, project_id="proj-r")
        con.execute("CREATE TABLE IF NOT EXISTS raw_stripe_payments (date VARCHAR)")
        con.execute("INSERT INTO raw_stripe_payments VALUES (?)", ["2024-03-15"])
        con.close()

    con2 = duckdb.connect(db)
    row = con2.execute(
        "SELECT COUNT(*) FROM org_readable_raw.raw_stripe_payments"
    ).fetchone()
    con2.close()
    assert row[0] == 1


# ---------------------------------------------------------------------------
# Negative path: a main.raw_x DOES trigger the invariant check failure
# (proves detection works)
# ---------------------------------------------------------------------------

def test_detection_fires_when_raw_in_main(tmp_path):
    """Sanity check: if raw_* is in main, our invariant check detects it."""
    db = str(tmp_path / "detect.duckdb")

    con = duckdb.connect(db)
    con.execute("CREATE TABLE raw_polluted (id INTEGER)")
    con.close()

    con2 = duckdb.connect(db)
    violations = _raw_tables_outside_org(con2)
    con2.close()

    # The detection must fire for main.raw_polluted
    assert any("raw_polluted" in v for v in violations), (
        f"Expected to detect main.raw_polluted but got: {violations}"
    )


# ---------------------------------------------------------------------------
# Multi-project: two orgs, no cross-contamination
# ---------------------------------------------------------------------------

def test_two_orgs_no_cross_contamination(tmp_path):
    """Two projects with distinct orgs write to separate schemas, no leakage."""
    from core import warehouse_tenancy, warehouse_write

    warehouse_write._reset_warn()
    db = str(tmp_path / "multi.duckdb")

    schemas_a = _make_schemas("alpha")
    schemas_b = _make_schemas("beta")

    with patch.object(warehouse_tenancy, "org_schemas_enabled", return_value=True):
        # Write for org alpha
        with patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=schemas_a
        ):
            con = warehouse_write.open_raw_writer(db, project_id="proj-a")
            con.execute("CREATE TABLE IF NOT EXISTS raw_gsc_daily (date VARCHAR)")
            con.execute("INSERT INTO raw_gsc_daily VALUES (?)", ["2024-01-01"])
            con.close()

        # Write for org beta
        with patch.object(
            warehouse_tenancy, "resolve_org_schemas", return_value=schemas_b
        ):
            con = warehouse_write.open_raw_writer(db, project_id="proj-b")
            con.execute("CREATE TABLE IF NOT EXISTS raw_gsc_daily (date VARCHAR)")
            con.execute("INSERT INTO raw_gsc_daily VALUES (?)", ["2024-01-02"])
            con.close()

    con2 = duckdb.connect(db)
    # Both schemas exist
    alpha_count = con2.execute(
        "SELECT COUNT(*) FROM org_alpha_raw.raw_gsc_daily"
    ).fetchone()[0]
    beta_count = con2.execute(
        "SELECT COUNT(*) FROM org_beta_raw.raw_gsc_daily"
    ).fetchone()[0]
    violations = _raw_tables_outside_org(con2)
    con2.close()

    assert alpha_count == 1
    assert beta_count == 1
    assert violations == [], f"Cross-schema contamination: {violations}"
