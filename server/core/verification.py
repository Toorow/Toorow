"""toorow -- Post-pull populate verification (Story 3.5, AD-13).

Every successful pull is followed by a verification pass that compares the rows
actually landed in the raw table against the expected row count derived from the
module manifest's report profile.  Results are written to app.pull_verifications
and, when rows are empty or sparse, the connection health is set to
'populate_failed' (distinct from 'revoked' / 'stale').

Public API:
  compute_expected_rows(manifest, date_from, date_to) -> int
  run_post_pull_verification(pull_id, connection_ref_id, date_from, date_to, manifest) -> None

Environment variables:
  VERIFICATION_PARTIAL_THRESHOLD  float in [0,1], default 0.5.
      A pull whose completeness_ratio is below this threshold (but > 0) receives
      verdict 'partial'.  A pull with zero actual rows always receives 'empty'.
  TOOROW_RAW_TABLE_NAME  name of the raw DuckDB table to count rows from.
      Defaults to the value set by the active module at startup via
      register_raw_table_name().  Required at P3-dev.
      # TODO(Phase-B): use BigQuery client for raw row count when TOOROW_DB_MODE=bigquery
      # TODO(3.6): generalize -- each module declares its raw table name in the manifest

Dev notes:
  - compute_expected_rows() reads manifest['report_profiles'] dimension cardinalities.
    When the manifest has no cardinality declarations (string-only dimension names),
    the fallback of 1 row/day applies.  This means any pull returning at least 1 row
    will receive verdict 'ok', which is the safe-conservative baseline.
    A future story can add 'cardinality' to the manifest dimensions for stricter checks.
  - All log strings are ASCII-only (AI-03).
  - This module never raises -- exceptions are caught and logged at WARNING.

AD-2: source-agnostic -- no module-specific strings anywhere in this file.
AD-13: populate verification is a mandatory architecture requirement.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from math import prod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_PARTIAL_THRESHOLD = 0.5

# Raw table name registry (AD-2: source-agnostic -- no module-specific names here).
# Each module's connector.py registers its raw table name at startup via
# register_raw_table_name(). Overridable via TOOROW_RAW_TABLE_NAME env var.
# TODO(3.6): generalize to per-module raw table name declared in the manifest
# review-3-5 F-5: PER-PROVIDER registry -- a single global would be silently
# overwritten by the second module (cross-provider row counts). The legacy
# single-arg call keeps working as a fallback entry for callers that predate
# the provider-aware signature.
_raw_table_names: dict[str, str] = {}
_raw_table_fallback: str = ""


def register_raw_table_name(name: str, provider: str | None = None) -> None:
    """Register a module's raw table name (called at module load time).

    Args:
        name: raw table name (module-owned string; core stays source-agnostic).
        provider: the module's manifest name. When omitted (legacy call), the
            name is stored as the single-module fallback.
    """
    global _raw_table_fallback
    if provider:
        _raw_table_names[provider] = name
    else:
        _raw_table_fallback = name


def _get_raw_table_name(provider: str | None = None) -> str:
    """Resolve the raw table for *provider* (env override > registry > fallback)."""
    env = os.environ.get("TOOROW_RAW_TABLE_NAME", "")
    if env:
        return env
    if provider and provider in _raw_table_names:
        return _raw_table_names[provider]
    return _raw_table_fallback


def _partial_threshold() -> float:
    """Read VERIFICATION_PARTIAL_THRESHOLD env var (default 0.5)."""
    try:
        raw = os.environ.get("VERIFICATION_PARTIAL_THRESHOLD", str(_DEFAULT_PARTIAL_THRESHOLD))
        return float(raw)
    except (ValueError, TypeError):
        return _DEFAULT_PARTIAL_THRESHOLD


# ---------------------------------------------------------------------------
# Expected-row computation from manifest (AC2)
# ---------------------------------------------------------------------------


def compute_expected_rows(manifest: dict, date_from: str, date_to: str) -> int:
    """Compute the expected row count for a pull window from the manifest profile.

    Algorithm:
      # AI-20: partial-verdict reachability
      1. Find the first report_profiles entry with granularity=='day' (or any if
         granularity is absent).  Fall back to days if no profiles exist.
      2. If the chosen profile declares verification_expected_rows_per_day (int),
         return days * verification_expected_rows_per_day. This activates the
         partial-verdict path: a module with value=150 means a 7-day pull expects
         1050 rows; if only 30 land, completeness_ratio = 0.029 -> verdict 'partial'.
         If null or absent, fall through to the cardinality-product algorithm.
      3. Read 'dimensions' list from the profile; extract 'cardinality' per entry
         (default 1 when absent -- conservative: any rows are 'expected').
      4. Return days * product(cardinalities).

    If the manifest has no report_profiles or no dimensions, return days (1 row
    per day as the safe minimum expectation -- avoids false-positive 'empty'
    verdicts on sparse sources).

    Args:
        manifest:  The module manifest dict (from loader.LoadedModule.manifest).
        date_from: ISO-8601 date string (inclusive start of pull window).
        date_to:   ISO-8601 date string (inclusive end of pull window).

    Returns:
        Expected row count (>= 1 when date window is valid).
    """
    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
        days = (d_to - d_from).days + 1
        if days < 1:
            days = 1
    except (ValueError, TypeError):
        return 1

    profiles = manifest.get("report_profiles")
    if not profiles:
        return days

    # Pick first profile with granularity=='day', or first profile if none specify granularity
    chosen = None
    for p in profiles:
        if p.get("granularity") == "day":
            chosen = p
            break
    if chosen is None:
        chosen = profiles[0]

    # AI-20: partial-verdict reachability — use manifest-declared rows_per_day when present.
    # verification_expected_rows_per_day must be an int (not null) to activate this path.
    rows_per_day = chosen.get("verification_expected_rows_per_day")
    if isinstance(rows_per_day, int):
        return days * rows_per_day
    # null or absent -> fall through to cardinality-product algorithm below

    dimensions = chosen.get("dimensions", [])
    if not dimensions:
        return days

    # Extract cardinalities; dimension entries may be strings (name only) or dicts
    cardinalities: list[int] = []
    for dim in dimensions:
        if isinstance(dim, dict):
            cardinalities.append(int(dim.get("cardinality", 1)))
        # String dimension names carry no cardinality information -- use 1
        else:
            cardinalities.append(1)

    if not cardinalities:
        return days

    return days * prod(cardinalities)


# ---------------------------------------------------------------------------
# Raw row count (AC4) -- DuckDB at P3-dev
# ---------------------------------------------------------------------------


def _count_raw_rows(
    pull_id: str,
    provider: str | None = None,
    project_id: str | None = None,
) -> int:
    """Count rows in the raw table for the given pull_id (DuckDB at P3-dev).

    The raw table name is read from TOOROW_RAW_TABLE_NAME env var or from
    the value registered by the active module via register_raw_table_name().

    Story 24.3 (T3): when TOOROW_ORG_SCHEMAS=1 and *project_id* is given,
    reads from the org raw schema (``org_<wslug>_raw.raw_*``) to match where
    connectors wrote their rows; falls back to unqualified ``main.raw_*``
    when the org is unresolvable (same degradation contract as warehouse_write).

    Returns 0 on any error (verification is best-effort; never raises).

    # TODO(Phase-B): use BigQuery client for raw row count when TOOROW_DB_MODE=bigquery
    # TODO(3.6): per-module table name from manifest (multi-module support)
    """
    import duckdb  # noqa: PLC0415

    raw_table = _get_raw_table_name(provider)
    if not raw_table:
        logger.warning(
            "verification: raw_table_name not configured -- cannot count rows pull_id=%s", pull_id
        )
        return 0

    duckdb_path = os.environ.get("TOOROW_DUCKDB_PATH", "")
    if not duckdb_path or not os.path.exists(duckdb_path):
        logger.warning(
            "verification: duckdb_path not found -- cannot count raw rows pull_id=%s", pull_id
        )
        return 0

    # Story 24.3: qualify the table name with the org raw schema when flag ON.
    qualified_table = raw_table  # default: unqualified (reads from main)
    try:
        from core import warehouse_tenancy  # noqa: PLC0415

        if warehouse_tenancy.org_schemas_enabled() and project_id:
            schemas = warehouse_tenancy.resolve_org_schemas(project_id=project_id)
            if schemas is not None:
                # DuckDB identifier folding symmetry: the write path uses SET
                # search_path + unquoted DDL, so DuckDB lowercases every table name
                # at creation time.  Quote BOTH schema AND table here so the reader
                # resolves the same lowercased identifier the writer produced --
                # prevents false-empty verdicts if a module ever uses a mixed-case
                # table name in its DDL (DuckDB folds it; quoting the read side
                # would send a different case).  All current raw_* names are already
                # pure lowercase snake_case, so the quotes are cosmetically neutral
                # but make the symmetry explicit and safe for future changes.
                qualified_table = f'"{schemas.raw}"."{raw_table}"'
    except Exception as exc:  # noqa: BLE001 -- degradation contract
        logger.warning(
            "verification: org schema resolution failed for project_id=%s: %s"
            " -- counting in main",
            project_id,
            exc,
        )

    try:
        con = duckdb.connect(duckdb_path, read_only=True)
        try:
            # Table name comes from the module registry / qualified path; not user input.
            row = con.execute(
                f"SELECT COUNT(*) FROM {qualified_table} WHERE pull_id = ?",  # noqa: S608
                [pull_id],
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            con.close()
    except Exception as exc:
        logger.warning("verification: count_raw_rows_failed pull_id=%s: %s", pull_id, exc)
        return 0


# ---------------------------------------------------------------------------
# Connection health red state (AC5)
# ---------------------------------------------------------------------------


def _set_connection_health_red(connection_ref_id: str, verdict: str, pull_id: str) -> None:
    """Upsert connection_health to status='populate_failed' for connection_ref_id.

    Mirrors health_poller._upsert_health() pattern but deliberately does NOT
    update last_fetched_at -- the pull ran (data was attempted), it just landed
    no or too few rows.

    Never raises -- logs at WARNING on exception (AI-03 ASCII-only strings).
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    now = datetime.now(tz=timezone.utc)
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.connection_health
                        (connection_ref_id, status, last_checked_at)
                    VALUES (%(id)s, 'populate_failed', %(now)s)
                    ON CONFLICT (connection_ref_id) DO UPDATE
                        SET status          = EXCLUDED.status,
                            last_checked_at = EXCLUDED.last_checked_at
                    """,
                    {"id": connection_ref_id, "now": now},
                )
            conn.commit()
        logger.warning(
            "verification: populate_failed: conn=%s verdict=%s pull_id=%s",
            connection_ref_id,
            verdict,
            pull_id,
        )
    except Exception as exc:
        logger.warning(
            "verification: set_health_red_failed conn=%s: %s", connection_ref_id, exc
        )


# ---------------------------------------------------------------------------
# Main verification entry point (AC3, AC4)
# ---------------------------------------------------------------------------


def run_post_pull_verification(
    pull_id: str,
    connection_ref_id: str,
    date_from: str,
    date_to: str,
    manifest: dict,
    provider: str | None = None,
    rejected_rows: int = 0,
    project_id: str | None = None,
) -> None:
    """Run post-pull populate verification and write result to app.pull_verifications.

    Called from queue._execute_job() after a successful pull (job state == 'done').
    This function NEVER raises -- any failure is logged at WARNING and the job
    state is left unchanged (HG-1: verification is an annotation, not job control).

    Story 24.3 (T3): accepts *project_id* (default None for backward compat) and
    threads it to ``_count_raw_rows`` so the count targets the same schema the
    connector wrote to (``org_<wslug>_raw`` when flag ON, ``main`` otherwise).

    Verdict logic:
      - actual_rows == 0                            -> 'empty'
      - completeness_ratio < VERIFICATION_PARTIAL_THRESHOLD -> 'partial'
      - otherwise                                   -> 'ok'

    When expected_rows == 0, set completeness_ratio = 1.0, verdict = 'ok'
    (zero-expectation pull cannot be 'empty'; avoids ZeroDivisionError).

    When verdict in ('empty', 'partial'), connection health is set to
    'populate_failed' via _set_connection_health_red() (HG-2: distinct from
    'auth_expired' / 'revoked').

    All log strings are ASCII-only (AI-03).
    """
    from ulid import ULID  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    try:
        # 1. Count actual rows in raw table (24.3: pass project_id for schema routing)
        actual_rows = _count_raw_rows(pull_id, provider, project_id=project_id)

        # 2. Compute expected rows from manifest
        expected_rows = compute_expected_rows(manifest, date_from, date_to)

        # 3. Completeness ratio + verdict (handle zero-division)
        partial_threshold = _partial_threshold()
        if expected_rows == 0:
            completeness_ratio = 1.0
            verdict = "ok"
        elif actual_rows == 0:
            completeness_ratio = 0.0
            verdict = "empty"
        else:
            completeness_ratio = round(actual_rows / expected_rows, 4)
            if completeness_ratio < partial_threshold:
                verdict = "partial"
            else:
                verdict = "ok"

        # 4. Mint ver_ ULID
        ver_id = f"ver_{ULID()}"

        # 5. Insert into app.pull_verifications
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.pull_verifications
                        (id, pull_id, connection_ref_id, expected_rows, actual_rows,
                         completeness_ratio, verdict, rejected_rows)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        ver_id,
                        pull_id,
                        connection_ref_id,
                        expected_rows,
                        actual_rows,
                        completeness_ratio,
                        verdict,
                        int(rejected_rows or 0),
                    ),
                )
            conn.commit()

        # 6. Log result
        logger.info(
            "verification: pull_id=%s verdict=%s actual=%d expected=%d ratio=%.4f",
            pull_id,
            verdict,
            actual_rows,
            expected_rows,
            completeness_ratio,
        )

        # 7. Set connection health red on empty/partial
        if verdict in ("empty", "partial"):
            _set_connection_health_red(connection_ref_id, verdict, pull_id)

    except Exception as exc:
        logger.warning(
            "verification: run_post_pull_verification_failed pull_id=%s: %s", pull_id, exc
        )
