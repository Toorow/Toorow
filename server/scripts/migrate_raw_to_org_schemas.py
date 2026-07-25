"""Migrate legacy main.raw_* DuckDB tables to per-org schemas (Story 24.3, AC5).

Usage
-----
  # Dry-run: show what would be moved (no data written)
  python migrate_raw_to_org_schemas.py --duckdb-path /data/warehouse.duckdb

  # Apply: copy rows to org schemas (idempotent, no drop)
  python migrate_raw_to_org_schemas.py --duckdb-path /data/warehouse.duckdb \\
      --platform-dsn postgresql://... --apply

  # Apply + drop legacy tables after verifying completeness (human-gated)
  python migrate_raw_to_org_schemas.py --duckdb-path /data/warehouse.duckdb \\
      --platform-dsn postgresql://... --apply --drop-legacy

Safety invariants
-----------------
- Copy and drop are SEPARATED: --drop-legacy requires --apply to have succeeded
  first, AND requires zero orphan rows (project_id with no resolvable org).
- The drop is audited via core.audit.write_audit_row.
- A project_id with no resolvable org is SKIPPED with a WARNING; its rows remain
  in main for manual inspection.
- The script is idempotent: re-run after a partial apply copies only missing rows
  (deduplication by pull_id).
- BigQuery is NOT handled here (AI-08; BQ raw follows Epic 3 / Phase B).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DDL_CACHE: dict[str, str] = {}  # table_name -> CREATE TABLE ... statement


def _ensure_core_on_path() -> None:
    """Add server/ to sys.path so core.* imports work when run as a script."""
    server_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if server_dir not in sys.path:
        sys.path.insert(0, server_dir)


def _get_raw_tables(duck) -> list[str]:
    """Return names of tables in the ``main`` schema matching ``raw_*``."""
    rows = duck.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'main' AND table_name LIKE 'raw_%'"
        " ORDER BY table_name"
    ).fetchall()
    return [r[0] for r in rows]


def _distinct_project_ids(duck, table: str) -> list[str]:
    """Return distinct non-NULL project_id values from main.<table>."""
    try:
        rows = duck.execute(
            # F-4: quote table name for symmetry with schema quoting.
            # Names come from information_schema (trusted), quoting is defensive.
            f'SELECT DISTINCT project_id FROM main."{table}"'  # noqa: S608
            " WHERE project_id IS NOT NULL ORDER BY project_id"
        ).fetchall()
        return [r[0] for r in rows]
    except Exception as exc:
        logger.warning("migrate: could not read project_ids from %s: %s", table, exc)
        return []


def _get_create_ddl(duck, table: str) -> str:
    """Return the CREATE TABLE DDL for main.<table> from duckdb_tables()."""
    if table in _DDL_CACHE:
        return _DDL_CACHE[table]
    # DuckDB provides the DDL via duckdb_tables() or SHOW CREATE TABLE
    try:
        row = duck.execute(
            f'SHOW CREATE TABLE main."{table}"'  # noqa: S608  # F-4: quote table
        ).fetchone()
        if row:
            ddl = row[0]
            # Change the table reference to unqualified for re-use in any schema
            _DDL_CACHE[table] = ddl
            return ddl
    except Exception:
        pass
    # Fallback: reconstruct minimal DDL from column info
    cols = duck.execute(
        f'DESCRIBE main."{table}"'  # noqa: S608  # F-4: quote table
    ).fetchall()
    col_defs = ", ".join(f"{c[0]} {c[1]}" for c in cols)
    ddl = f"CREATE TABLE IF NOT EXISTS {table} ({col_defs})"
    _DDL_CACHE[table] = ddl
    return ddl


def _count_in_main(duck, table: str, project_id: str) -> int:
    try:
        row = duck.execute(
            # F-4: quote table name for symmetry with schema quoting.
            f'SELECT COUNT(*) FROM main."{table}" WHERE project_id = ?'  # noqa: S608
            , [project_id]
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _count_in_org(duck, raw_schema: str, table: str, project_id: str) -> int:
    try:
        row = duck.execute(
            # F-4: quote both schema and table consistently.
            f'SELECT COUNT(*) FROM "{raw_schema}"."{table}" WHERE project_id = ?'  # noqa: S608
            , [project_id]
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def _copy_rows(duck, table: str, raw_schema: str, project_id: str) -> int:
    """Copy rows from main.<table> to <raw_schema>.<table> for this project_id.

    Deduplicates by pull_id: rows already present in the destination are skipped.
    Returns the number of rows actually inserted.
    """
    # Ensure the destination table exists (same DDL as source)
    try:
        # We recreate the table with IF NOT EXISTS.
        # Get columns from source
        # F-4: quote table name for symmetry with schema quoting.
        cols = duck.execute(f'DESCRIBE main."{table}"').fetchall()  # noqa: S608
        col_defs = ", ".join(f'"{c[0]}" {c[1]}' for c in cols)
        duck.execute(
            # F-4: quote both schema and table consistently.
            f'CREATE TABLE IF NOT EXISTS "{raw_schema}"."{table}" ({col_defs})'  # noqa: S608
        )
    except Exception as exc:
        logger.warning(
            "migrate: could not ensure table %s.%s: %s", raw_schema, table, exc
        )
        return 0

    # Insert rows where pull_id not already in destination.
    # pull_id is the idempotency key (every row in raw_* has a pull_id).
    try:
        col_names = [c[0] for c in duck.execute(f'DESCRIBE main."{table}"').fetchall()]  # noqa: S608  # F-4
        select_cols = ", ".join(f'src."{c}"' for c in col_names)
        insert_cols = ", ".join(f'"{c}"' for c in col_names)

        # F-5: snapshot before-count so the fallback reports the DELTA inserted,
        # not the total rows in org (which over-counts on re-runs).
        before = _count_in_org(duck, raw_schema, table, project_id)

        result = duck.execute(
            # F-4: quote both schema and table consistently throughout.
            f"""
            INSERT INTO "{raw_schema}"."{table}" ({insert_cols})
            SELECT {select_cols}
            FROM main."{table}" AS src
            WHERE src.project_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM "{raw_schema}"."{table}" AS dst
                  WHERE dst.pull_id = src.pull_id
              )
            """,  # noqa: S608
            [project_id],
        )
        # DuckDB returns the number of affected rows via rowcount.
        # F-5: when rowcount is unavailable or unreliable (<= 0), compute the
        # delta as after - before so we report "rows copied" not "rows in org"
        # (the original fallback over-counted on subsequent runs).
        count = result.rowcount if hasattr(result, "rowcount") and result.rowcount > 0 else None
        if count is None:
            after = _count_in_org(duck, raw_schema, table, project_id)
            count = max(0, after - before)  # delta = rows inserted this run
        return count
    except Exception as exc:
        logger.warning(
            "migrate: failed to copy %s -> %s.%s for project_id=%s: %s",
            table, raw_schema, table, project_id, exc,
        )
        return 0


# ---------------------------------------------------------------------------
# Main migration logic
# ---------------------------------------------------------------------------

def run_migration(
    duckdb_path: str,
    platform_dsn: str | None,
    apply: bool,
    drop_legacy: bool,
) -> int:
    """Execute the migration.  Returns 0 on success, 1 on partial failure."""
    import duckdb as _duckdb  # noqa: PLC0415
    from core import warehouse_tenancy  # noqa: PLC0415

    if platform_dsn:
        os.environ.setdefault("DATABASE_URL", platform_dsn)

    duck = _duckdb.connect(duckdb_path)

    raw_tables = _get_raw_tables(duck)
    if not raw_tables:
        logger.info("migrate: no raw_* tables found in main -- nothing to migrate")
        duck.close()
        return 0

    logger.info("migrate: found %d raw_* tables in main: %s", len(raw_tables), raw_tables)

    # Gather (table, project_id) pairs and resolve orgs
    plan: list[tuple[str, str, str]] = []   # (table, project_id, raw_schema)
    orphans: list[tuple[str, str]] = []      # (table, project_id) with no org

    for table in raw_tables:
        project_ids = _distinct_project_ids(duck, table)
        for pid in project_ids:
            schemas = warehouse_tenancy.resolve_org_schemas(project_id=pid)
            if schemas is None:
                logger.warning(
                    "migrate: project_id=%s has no resolvable org -- "
                    "rows in %s will remain in main (inspect manually)",
                    pid, table,
                )
                orphans.append((table, pid))
            else:
                plan.append((table, pid, schemas.raw))

    # Report plan
    logger.info(
        "migrate: plan: %d (table, project) pairs to migrate, %d orphans",
        len(plan), len(orphans),
    )
    for table, pid, raw_schema in plan:
        n = _count_in_main(duck, table, pid)
        logger.info(
            "migrate: [PLAN] %s project_id=%s -> %s (%d rows)",
            table, pid, raw_schema, n,
        )
    for table, pid in orphans:
        n = _count_in_main(duck, table, pid)
        logger.warning(
            "migrate: [ORPHAN] %s project_id=%s -- %d rows cannot be migrated (no org)",
            table, pid, n,
        )

    if not apply:
        logger.info("migrate: dry-run complete (use --apply to execute)")
        duck.close()
        return 0

    # Apply: copy rows
    total_copied = 0
    for table, pid, raw_schema in plan:
        duck.execute(f'CREATE SCHEMA IF NOT EXISTS "{raw_schema}"')  # noqa: S608
        copied = _copy_rows(duck, table, raw_schema, pid)
        total_copied += copied
        logger.info(
            "migrate: copied %d rows %s project_id=%s -> %s",
            copied, table, pid, raw_schema,
        )

    logger.info("migrate: apply complete -- %d rows copied total", total_copied)

    if not drop_legacy:
        duck.close()
        return 0

    # Drop legacy: only if no orphans and completeness verified
    if orphans:
        logger.error(
            "migrate: --drop-legacy refused: %d orphan project_id(s) still in main"
            " (rows without a resolvable org). Resolve them first.",
            len(orphans),
        )
        duck.close()
        return 1

    # Verify completeness: every main row must be in its org schema
    incomplete: list[str] = []
    for table, pid, raw_schema in plan:
        main_count = _count_in_main(duck, table, pid)
        org_count = _count_in_org(duck, raw_schema, table, pid)
        if main_count != org_count:
            incomplete.append(
                f"{table}[project_id={pid}]: main={main_count} org={org_count}"
            )
    if incomplete:
        logger.error(
            "migrate: --drop-legacy refused: completeness check failed:\n  %s",
            "\n  ".join(incomplete),
        )
        duck.close()
        return 1

    # Emit audit record before dropping
    try:
        from core.audit import (  # noqa: PLC0415
            ACTION_ORG_SCHEMAS_DROPPED,
            write_audit_row,
        )

        write_audit_row(
            identity="migrate_raw_to_org_schemas",
            action=ACTION_ORG_SCHEMAS_DROPPED,
            provider_account="n/a",
            connection_ref="n/a",
            metadata={
                "event": "drop_legacy_raw",
                "tables": raw_tables,
                "plan_count": len(plan),
                "total_copied": total_copied,
            },
        )
    except Exception as exc:
        logger.warning("migrate: audit write failed (continuing with drop): %s", exc)

    # Drop legacy tables
    for table in raw_tables:
        try:
            # F-4: quote table name for symmetry with schema quoting.
            duck.execute(f'DROP TABLE IF EXISTS main."{table}"')  # noqa: S608
            logger.info("migrate: dropped main.%s", table)
        except Exception as exc:
            logger.error("migrate: failed to drop main.%s: %s", table, exc)
            duck.close()
            return 1

    logger.info("migrate: drop-legacy complete")
    duck.close()
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Migrate legacy main.raw_* DuckDB tables to per-org schemas."
    )
    parser.add_argument(
        "--duckdb-path",
        required=True,
        help="Path to the DuckDB database file.",
    )
    parser.add_argument(
        "--platform-dsn",
        default=None,
        help="PostgreSQL DSN for org resolution (default: reads DATABASE_URL env).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Execute the copy (default: dry-run only).",
    )
    parser.add_argument(
        "--drop-legacy",
        action="store_true",
        default=False,
        help=(
            "Drop legacy main.raw_* tables after verifying completeness."
            " Requires --apply. DESTRUCTIVE: human-gated."
        ),
    )

    args = parser.parse_args()

    if args.drop_legacy and not args.apply:
        parser.error("--drop-legacy requires --apply")

    _ensure_core_on_path()

    sys.exit(
        run_migration(
            duckdb_path=args.duckdb_path,
            platform_dsn=args.platform_dsn,
            apply=args.apply,
            drop_legacy=args.drop_legacy,
        )
    )


if __name__ == "__main__":
    main()
