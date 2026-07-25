"""Tests for server/scripts/migrate_raw_to_org_schemas.py (Story 24.3, T5.b).

All tests are OFFLINE (tmp DuckDB, mocked resolve_org_schemas).

Coverage:
  - Re-ventilation multi-projets (rows routed to correct org schemas).
  - Idempotence: 2 runs produce the same counts (no duplicate rows).
  - project_id sans org -> reste dans main + WARNING.
  - --drop-legacy refuses when orphans exist.
  - --drop-legacy refuses when org count != main count.
  - drop émet un audit (write_audit_row appelé).
  - drop réussi: main tables supprimées.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import patch

import duckdb

# Ensure server/ is on sys.path for the script import
_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from scripts.migrate_raw_to_org_schemas import run_migration  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_schemas(slug: str):
    from core.warehouse_tenancy import OrgSchemas

    wslug = slug.replace("-", "_")
    return OrgSchemas(
        org_id=f"org-{slug}",
        org_slug=slug,
        warehouse_slug=wslug,
        raw=f"org_{wslug}_raw",
        marts=f"org_{wslug}_marts",
    )


def _seed_main(db_path: str, project_ids: list[str], table: str = "raw_ga4_standard_daily") -> None:
    """Seed main.<table> with one row per project_id."""
    con = duckdb.connect(db_path)
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {table}"
        " (date VARCHAR, pull_id VARCHAR, project_id VARCHAR)"
    )
    for pid in project_ids:
        con.execute(
            f"INSERT INTO {table} VALUES (?, ?, ?)",
            ["2024-01-01", f"pull-{pid}", pid],
        )
    con.close()


# ---------------------------------------------------------------------------
# Multi-project re-ventilation
# ---------------------------------------------------------------------------

def test_multi_project_routing(tmp_path):
    """Rows are routed to the correct org schemas for each project_id."""
    db = str(tmp_path / "multi.duckdb")
    _seed_main(db, ["proj-a", "proj-b"])

    schemas_a = _make_schemas("alpha")
    schemas_b = _make_schemas("beta")

    def mock_resolve(project_id=None, **_kw):
        return {"proj-a": schemas_a, "proj-b": schemas_b}.get(project_id)

    with patch("core.warehouse_tenancy.resolve_org_schemas", side_effect=mock_resolve), \
         patch("core.audit.write_audit_row"):
        rc = run_migration(db, platform_dsn=None, apply=True, drop_legacy=False)

    assert rc == 0

    con = duckdb.connect(db)
    alpha_count = con.execute(
        "SELECT COUNT(*) FROM org_alpha_raw.raw_ga4_standard_daily WHERE project_id='proj-a'"
    ).fetchone()[0]
    beta_count = con.execute(
        "SELECT COUNT(*) FROM org_beta_raw.raw_ga4_standard_daily WHERE project_id='proj-b'"
    ).fetchone()[0]
    # No cross-contamination
    alpha_has_b = con.execute(
        "SELECT COUNT(*) FROM org_alpha_raw.raw_ga4_standard_daily WHERE project_id='proj-b'"
    ).fetchone()[0]
    con.close()

    assert alpha_count == 1
    assert beta_count == 1
    assert alpha_has_b == 0


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

def test_idempotence_two_runs(tmp_path):
    """Running the migration twice yields identical row counts (no duplicates)."""
    db = str(tmp_path / "idem.duckdb")
    _seed_main(db, ["proj-1"])

    schemas = _make_schemas("myorg")

    def mock_resolve(project_id=None, **_kw):
        return schemas if project_id == "proj-1" else None

    with patch("core.warehouse_tenancy.resolve_org_schemas", side_effect=mock_resolve):
        rc1 = run_migration(db, platform_dsn=None, apply=True, drop_legacy=False)

    with patch("core.warehouse_tenancy.resolve_org_schemas", side_effect=mock_resolve):
        rc2 = run_migration(db, platform_dsn=None, apply=True, drop_legacy=False)

    assert rc1 == 0
    assert rc2 == 0

    con = duckdb.connect(db)
    count = con.execute(
        "SELECT COUNT(*) FROM org_myorg_raw.raw_ga4_standard_daily WHERE project_id='proj-1'"
    ).fetchone()[0]
    con.close()

    assert count == 1, f"Expected exactly 1 row after 2 runs, got {count}"


# ---------------------------------------------------------------------------
# Orphan: project_id without resolvable org stays in main
# ---------------------------------------------------------------------------

def test_orphan_stays_in_main(tmp_path, caplog):
    """project_id with no resolvable org: rows stay in main, WARNING emitted."""
    db = str(tmp_path / "orphan.duckdb")
    _seed_main(db, ["proj-orphan"])

    with patch("core.warehouse_tenancy.resolve_org_schemas", return_value=None):
        with caplog.at_level(logging.WARNING):
            rc = run_migration(db, platform_dsn=None, apply=True, drop_legacy=False)

    assert rc == 0  # not a fatal error -- rows just stay in main

    con = duckdb.connect(db)
    main_count = con.execute(
        "SELECT COUNT(*) FROM raw_ga4_standard_daily WHERE project_id='proj-orphan'"
    ).fetchone()[0]
    con.close()

    assert main_count == 1, "Orphan rows must remain in main"
    assert any("no resolvable org" in r.message or "orphan" in r.message.lower()
               for r in caplog.records), "Expected WARNING about orphan project_id"


# ---------------------------------------------------------------------------
# --drop-legacy refused when orphans exist
# ---------------------------------------------------------------------------

def test_drop_refused_with_orphans(tmp_path):
    """--drop-legacy must be refused when orphan project_ids exist."""
    db = str(tmp_path / "drop_orphan.duckdb")
    _seed_main(db, ["proj-known", "proj-unknown"])

    schemas = _make_schemas("known")

    def mock_resolve(project_id=None, **_kw):
        return schemas if project_id == "proj-known" else None

    with patch("core.warehouse_tenancy.resolve_org_schemas", side_effect=mock_resolve), \
         patch("core.audit.write_audit_row") as mock_audit:
        rc = run_migration(db, platform_dsn=None, apply=True, drop_legacy=True)

    assert rc == 1, "Should fail because of orphans"
    mock_audit.assert_not_called()

    # Legacy table must still exist
    con = duckdb.connect(db)
    tables = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        " AND table_name='raw_ga4_standard_daily'"
    ).fetchall()
    con.close()
    assert tables, "Legacy table must NOT be dropped when orphans exist"


# ---------------------------------------------------------------------------
# --drop-legacy refused when completeness check fails
# ---------------------------------------------------------------------------

def test_drop_refused_when_incompleteness(tmp_path):
    """--drop-legacy must be refused when org count < main count (incomplete copy).

    The apply step is idempotent and re-syncs before the completeness check, so
    the only reachable incompleteness is a copy FAILURE: simulate it by forcing
    _copy_rows to copy nothing (its real failure mode logs and returns 0)."""
    db = str(tmp_path / "incomplete.duckdb")
    _seed_main(db, ["proj-c"])

    schemas = _make_schemas("comp")

    def mock_resolve(project_id=None, **_kw):
        return schemas if project_id == "proj-c" else None

    with patch("core.warehouse_tenancy.resolve_org_schemas", side_effect=mock_resolve), \
         patch("core.audit.write_audit_row") as mock_audit, \
         patch("scripts.migrate_raw_to_org_schemas._copy_rows", return_value=0):
        rc = run_migration(db, platform_dsn=None, apply=True, drop_legacy=True)

    assert rc == 1, "Should fail due to incompleteness"
    mock_audit.assert_not_called()

    # The legacy table must still be there (nothing dropped).
    con = duckdb.connect(db)
    remaining = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        " AND table_name='raw_ga4_standard_daily'"
    ).fetchall()
    con.close()
    assert remaining, "Legacy table must survive a refused drop"


# ---------------------------------------------------------------------------
# --drop-legacy success: audit emitted + tables dropped
# ---------------------------------------------------------------------------

def test_drop_success_audit_and_clean(tmp_path):
    """Successful drop: audit row written, legacy table removed."""
    db = str(tmp_path / "drop_ok.duckdb")
    _seed_main(db, ["proj-drop"])

    schemas = _make_schemas("drop_org")

    def mock_resolve(project_id=None, **_kw):
        return schemas if project_id == "proj-drop" else None

    with patch("core.warehouse_tenancy.resolve_org_schemas", side_effect=mock_resolve), \
         patch("core.audit.write_audit_row") as mock_audit:
        rc = run_migration(db, platform_dsn=None, apply=True, drop_legacy=True)

    assert rc == 0
    mock_audit.assert_called_once()
    audit_call = mock_audit.call_args
    # Action must be the canonical ACTION_ORG_SCHEMAS_DROPPED value
    from core.audit import ACTION_ORG_SCHEMAS_DROPPED
    called_action = audit_call.kwargs.get("action") or (
        audit_call.args[1] if len(audit_call.args) > 1 else None
    )
    assert called_action == ACTION_ORG_SCHEMAS_DROPPED

    con = duckdb.connect(db)
    remaining = con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
        " AND table_name='raw_ga4_standard_daily'"
    ).fetchall()
    con.close()
    assert remaining == [], "Legacy table must be dropped on success"


# ---------------------------------------------------------------------------
# Dry-run: no data written
# ---------------------------------------------------------------------------

def test_dry_run_does_not_write(tmp_path):
    """Dry-run (no --apply): no rows copied, no schemas created."""
    db = str(tmp_path / "dryrun.duckdb")
    _seed_main(db, ["proj-dry"])

    schemas = _make_schemas("dryorg")

    def mock_resolve(project_id=None, **_kw):
        return schemas

    with patch("core.warehouse_tenancy.resolve_org_schemas", side_effect=mock_resolve):
        rc = run_migration(db, platform_dsn=None, apply=False, drop_legacy=False)

    assert rc == 0

    con = duckdb.connect(db)
    schema_exists = con.execute(
        "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name='org_dryorg_raw'"
    ).fetchone()[0]
    con.close()
    assert schema_exists == 0, "Dry-run must not create any schema"
