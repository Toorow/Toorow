"""Governed Postgres -> DuckDB/BigQuery mirror sync (AD-8, Story 4.4).

Syncs a configurable list of Postgres tables to the DuckDB mirror schema
(local analog of BigQuery mirror_* dataset). Postgres is the sole writer;
DuckDB/BigQuery is read-only from the analytics perspective.

AD-8: this is the ONLY Postgres->BigQuery path. No other code path may write
Postgres data to BigQuery/DuckDB directly.

This is the ONLY file that may write to the mirror.* schema.

Story 5.3 extension: to add alert_definitions to the mirror, add it to
MIRROR_SYNC_TABLES env var or extend the default list below.
No code change in this file is required (HG-8).

Story 11.3 extension: context_topics, procedures, context_graph, schema_context
added to _ALLOWED_TABLES and _DEFAULT_TABLES. Single Postgres->warehouse path
(AD-8) unchanged; DuckDB-first; BigQuery Phase B unaffected.

Story 22.1 extension: media_plans, media_plan_versions, media_plan_lines,
plan_allocation_daily added to the mirror (FR38/CAP-26). The plan-vs-actual mart
(22.4) reads these; media_plan_versions carries is_active so the mart filters the
active version. AD-8 path unchanged.

Story 22.3 extension: plan_line_mappings added to the mirror (FR38/CAP-26). The
22.4 ventilation joins active mappings against fact_daily_kpi to split each
campaign's spend across the plan lines that claim it. AD-8 path unchanged.

Story 13.2 extension: fx_conflict_resolutions added to the mirror (AD-6). The
dbt staging models (stg_*_daily.sql) JOIN on this table to prefer the declared
source currency over raw.cost_source_currency when a CURRENCY_CONFLICT resolution
exists. AD-6 path unchanged: no Python conversion, only dbt staging reads this.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level last sync result (AC9)
# Health endpoint reads this to surface lag metric.
# ---------------------------------------------------------------------------
_last_sync_result: dict | None = None

# ---------------------------------------------------------------------------
# connection_ref_dim column selection (AC2)
# HARDCODED intentionally -- only project-dimension columns, no tokens (AD-3).
# Adding columns requires an explicit code change here to prevent accidental
# token leakage. Do NOT make this dynamic.
#
# NOTE: The actual Postgres schema uses 'provider' for the connector identifier
# (not 'connector_name'). We alias it as 'connector_name' in the mirror so dbt
# models use a consistent name. 'display_name' does not exist in connection_ref
# at this schema version; it is aliased as NULL for forward compatibility with
# Story 5.3 which may add it. This column list is the authoritative definition
# of what the mirror exposes -- no token columns (nango_connection_id excluded).
# ---------------------------------------------------------------------------
_CONNECTION_REF_DIM_SQL = """
    SELECT project_id, provider AS connector_name, NULL::VARCHAR AS display_name
    FROM app.connection_ref
"""

_DEFAULT_TABLES = [
    "context_events",
    "project_preferences",
    "connection_ref_dim",
    "alert_definitions",   # Story 5.3: added with zero code change (HG-8)
    "context_topics",      # Story 11.3: knowledge warehouse mirror (AD-8, DuckDB-first)
    "procedures",          # Story 11.3: knowledge warehouse mirror (AD-8, DuckDB-first)
    "context_graph",       # Story 11.3: knowledge warehouse mirror (AD-8, DuckDB-first)
    "schema_context",      # Story 11.3: knowledge warehouse mirror (AD-8, DuckDB-first)
    "media_plans",         # Story 22.1: media plan mirror (AD-8) -- FR38/CAP-26
    "media_plan_versions", # Story 22.1: includes is_active so 22.4 filters the active version
    "media_plan_lines",    # Story 22.1: version-scoped plan lines (line_key stable identity)
    "plan_allocation_daily",  # Story 22.1: materialised daily spread for the plan-vs-actual join
    "plan_line_mappings",  # Story 22.3: N:M line<->campaign mapping + splits (22.4 ventilation)
    "fx_conflict_resolutions",  # Story 13.2: CURRENCY_CONFLICT resolutions (AD-6, consumed by dbt staging)
]

# ---------------------------------------------------------------------------
# Allowlist of table names that may be synced (mirror_sync hardening)
# Any name returned by _get_tables() that is NOT in this set is ignored with
# a WARNING log.  Add new tables here when the migration + dbt model land.
# AD-2: no provider-specific strings -- only governance / analytics tables.
# ---------------------------------------------------------------------------
_ALLOWED_TABLES: frozenset[str] = frozenset(_DEFAULT_TABLES)

# ---------------------------------------------------------------------------
# Process-level DuckDB write lock (mirror_sync hardening)
# DuckDB allows only one writer at a time per file within a process.
# This lock serialises our own concurrent write calls so two nightly runs
# (e.g. triggered manually + scheduled) cannot race on the DuckDB file.
# Cross-process safety: DuckDB's own file-level lock handles that; if a
# concurrent reader holds the file we retry once after a short sleep.
# ---------------------------------------------------------------------------
_duckdb_write_lock = threading.Lock()


def _get_tables(tables: list[str] | None) -> list[str]:
    """Resolve and validate the list of tables to sync.

    Priority:
    1. Explicit ``tables`` argument (caller-supplied).
    2. MIRROR_SYNC_TABLES env var (comma-separated).
    3. Default list: context_events, project_preferences, connection_ref_dim.

    Validation: every resolved name is checked against _ALLOWED_TABLES.
    Unknown names are logged at WARNING and silently dropped so a typo in
    MIRROR_SYNC_TABLES cannot cause data corruption or SQL injection
    (table names are used in f-string SQL -- must be allowlisted).

    AD-2: table list is externally configured -- no module-specific strings.
    HG-8: adding alert_definitions requires only an env-var change, not code.
    """
    if tables is not None:
        raw = tables
    else:
        env_val = os.environ.get("MIRROR_SYNC_TABLES", "").strip()
        if env_val:
            raw = [t.strip() for t in env_val.split(",") if t.strip()]
        else:
            raw = list(_DEFAULT_TABLES)

    validated: list[str] = []
    for name in raw:
        if name in _ALLOWED_TABLES:
            validated.append(name)
        else:
            logger.warning(
                "mirror_sync: unknown_table_ignored table=%s"
                " (not in _ALLOWED_TABLES -- add it to mirror_sync.py to enable)",
                name,
            )
    return validated


def _fetch_from_postgres(table: str) -> tuple[list[str], list[dict]]:
    """Fetch all rows from app.<table> in Postgres.

    For connection_ref_dim: uses explicit safe column SELECT (no tokens, AD-3).
    For all other tables: SELECT * from app.<table>.

    Returns (col_names, rows_as_list_of_dicts).
    """
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            if table == "connection_ref_dim":
                cur.execute(_CONNECTION_REF_DIM_SQL)
            else:
                cur.execute(f"SELECT * FROM app.{table}")  # noqa: S608 — table name from config only

            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return cols, rows


def _write_to_duckdb(table: str, col_names: list[str], rows: list[dict], db_path: str) -> None:
    """Write rows to mirror.<table> in DuckDB (full replace, CREATE OR REPLACE).

    Uses pandas DataFrame as an in-memory intermediary so DuckDB can infer
    column types from Python objects cleanly. When rows are empty, creates an
    empty table with the correct schema (col_names) so the table exists and
    _fetch_context_events can detect it as "synced but empty" vs "not synced".

    Thread-safety: acquires _duckdb_write_lock before opening the DuckDB
    connection so that concurrent calls within the same process are serialised.
    Retries once on duckdb.IOException (e.g. a concurrent reader from another
    process briefly holds the file) with a 0.5-second sleep.
    """
    import duckdb  # noqa: PLC0415
    import pandas as pd  # noqa: PLC0415

    def _do_write() -> None:
        conn = duckdb.connect(db_path)
        try:
            conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
            if rows:
                df = pd.DataFrame(rows)
                conn.register("_mirror_sync_df", df)
                conn.execute(  # review-epic-4 F-2: atomic, no CatalogException window
                    f"CREATE OR REPLACE TABLE mirror.{table} AS SELECT * FROM _mirror_sync_df"
                )
                conn.unregister("_mirror_sync_df")
            else:
                # No rows but we know the schema from col_names -- create an empty
                # table with correct column structure so the table exists in DuckDB.
                # This distinguishes "synced but empty" from "not yet synced" (which
                # would raise CatalogException in _fetch_context_events -> graceful []).
                cols_ddl = ", ".join(f"{c} VARCHAR" for c in col_names)
                conn.execute(f"CREATE OR REPLACE TABLE mirror.{table} ({cols_ddl})")
        finally:
            conn.close()

    with _duckdb_write_lock:
        try:
            _do_write()
        except duckdb.IOException as exc:
            # Retry once: a concurrent reader in another process may hold the file.
            logger.warning(
                "mirror_sync: duckdb_io_error table=%s -- retrying once: %s", table, exc
            )
            time.sleep(0.5)
            _do_write()  # raises if it fails a second time


def sync_tables(
    tables: list[str] | None = None,
    target_backend: str | None = None,
) -> dict:
    """Sync Postgres governance tables to the DuckDB/BigQuery mirror schema.

    AD-8: the ONLY Postgres->DuckDB write path. No other module may call this.

    Args:
        tables: List of table names to sync. Defaults to MIRROR_SYNC_TABLES env
                var or ["context_events", "project_preferences", "connection_ref_dim"].
                For connection_ref_dim, only safe columns are selected (AD-3).
        target_backend: "duckdb" | "bigquery". Defaults to TOOROW_DB_MODE env
                        var (same dual-backend pattern as warehouse.py).

    Returns:
        {
            "synced": {"context_events": N, "project_preferences": N, ...},
            "lag_seconds": float,     # wall-clock from Postgres read to DuckDB write
            "synced_at": str,         # ISO-8601 UTC timestamp
        }
        On Postgres error:
        {
            "error": str,
            "synced": {},
            "lag_seconds": 0.0,
        }
    """
    global _last_sync_result  # noqa: PLW0603

    table_list = _get_tables(tables)
    backend = target_backend or os.environ.get("TOOROW_DB_MODE", "duckdb")
    db_path = os.environ.get("TOOROW_DUCKDB_PATH", "")

    t0 = time.perf_counter()
    synced: dict[str, int] = {}

    for table in table_list:
        try:
            col_names, rows = _fetch_from_postgres(table)
        except Exception as exc:
            logger.warning("mirror_sync: postgres_fetch_error table=%s: %s", table, exc)
            result: dict = {
                "error": str(exc),
                "synced": synced,
                "lag_seconds": 0.0,
            }
            _last_sync_result = result
            return result

        if backend == "duckdb":
            if not db_path:
                logger.warning(
                    "mirror_sync: TOOROW_DUCKDB_PATH not set -- skipping DuckDB write"
                )
                synced[table] = len(rows)
                continue
            try:
                _write_to_duckdb(table, col_names, rows, db_path)
                synced[table] = len(rows)
            except Exception as exc:
                logger.warning(
                    "mirror_sync: duckdb_write_error table=%s: %s", table, exc
                )
                result = {"error": str(exc), "synced": synced, "lag_seconds": 0.0}
                _last_sync_result = result
                return result

        elif backend == "bigquery":
            # BigQuery write path — deferred to Phase B.
            # Same pattern: create or replace mirror_* dataset table.
            # Not implemented at P4-dev (local only).
            logger.info(
                "mirror_sync: bigquery target -- write for %s deferred (Phase B)", table
            )
            synced[table] = len(rows)

        else:
            raise ValueError(f"Unknown TOOROW_DB_MODE for mirror sync: {backend!r}")

    lag_seconds = round(time.perf_counter() - t0, 4)
    synced_at = datetime.now(tz=timezone.utc).isoformat()

    logger.info(
        "mirror_sync: synced %s in %.2fs",
        " ".join(f"{k}={v}" for k, v in synced.items()),
        lag_seconds,
    )

    result = {
        "synced": synced,
        "lag_seconds": lag_seconds,
        "synced_at": synced_at,
        "tables": synced,  # alias for AC9 health shape compatibility
    }
    _last_sync_result = result
    return result
