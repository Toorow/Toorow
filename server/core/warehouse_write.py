"""toorow -- shared raw-write entry point for connector modules (Story 24.3, Epic 24).

Single helper ``open_raw_writer`` that connectors call in place of a bare
``duckdb.connect(path)`` for write operations.  The schema routing (legacy
``main`` vs org-partitioned ``org_<wslug>_raw``) lives HERE and nowhere else
inside the write path.

Design rules enforced:
  - AD-2: source-agnostic -- no imports from server/modules, no provider names.
  - Naming invariant: schema names are NEVER composed here.  They come
    exclusively from ``warehouse_tenancy.resolve_org_schemas`` (``OrgSchemas.raw``).
  - Flag OFF (default): returns a plain DuckDB connection on ``main`` (the schema
    DuckDB uses when no search_path is set) -- bit-identical to the legacy
    ``duckdb.connect(path)`` pattern.
  - Flag ON: resolves ``org_<wslug>_raw``, creates the schema if absent
    (idempotent), sets ``search_path``, returns the connection.
  - Unresolvable org (flag ON): degrades to legacy ``main`` + WARNING once per
    project (same degradation contract as warehouse_tenancy / branding.py).
  - Contract: the caller calls ``con.close()`` as before; this helper does NOT
    wrap with a context-manager so module code is unchanged beyond the one-liner
    swap.  (DuckDB connections support both ``.close()`` and ``with`` usage.)
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Warn-once set (mirrors warehouse_tenancy._WARNED pattern, scoped to this module).
_WARNED: set[str] = set()

_CANDIDATE_TABLE_STATEMENT = re.compile(
    r"\b(CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE|INSERT\s+INTO)"
    r"\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

_DUCKDB_TO_LOGICAL = {
    "VARCHAR": "STRING",
    "TEXT": "STRING",
    "DOUBLE": "FLOAT",
    "FLOAT": "FLOAT",
    "REAL": "FLOAT",
    "BIGINT": "INTEGER",
    "INTEGER": "INTEGER",
    "SMALLINT": "INTEGER",
    "BOOLEAN": "BOOLEAN",
    "DATE": "DATE",
    "TIMESTAMP": "TIMESTAMP",
    "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
    "JSON": "JSON",
}

#: Physical DuckDB representations accepted for an already-recorded logical
#: landing contract. Raw DATE/TIMESTAMP values are deliberately stored as text
#: by ``raw_landing``; compatibility must follow that writer mapping rather than
#: reverse-introspection's most obvious logical type.
_LOGICAL_TO_DUCKDB = {
    "STRING": frozenset({"VARCHAR", "TEXT"}),
    "FLOAT": frozenset({"DOUBLE", "FLOAT", "REAL"}),
    "INTEGER": frozenset({"BIGINT", "INTEGER", "SMALLINT"}),
    "BOOLEAN": frozenset({"BOOLEAN"}),
    "DATE": frozenset({"VARCHAR", "TEXT", "DATE"}),
    "TIMESTAMP": frozenset({"VARCHAR", "TEXT", "TIMESTAMP", "TIMESTAMP WITH TIME ZONE"}),
    "JSON": frozenset({"JSON", "VARCHAR", "TEXT"}),
}


def _normalized_duckdb_type(value: object) -> str:
    normalized = str(value).upper()
    if normalized.startswith(("DECIMAL(", "NUMERIC(")):
        return "DOUBLE"
    return normalized


def _validate_physical_contract(
    relation: str,
    logical: list[tuple[str, str]],
    physical: list[tuple[object, object]],
) -> None:
    """Refuse a stale physical relation that contradicts its logical contract."""
    from core.raw_landing import RawLandingError  # noqa: PLC0415

    if len(logical) != len(physical):
        raise RawLandingError(
            f"candidate writer: physical schema for {relation!r} is incompatible "
            f"with its logical contract: {len(physical)} columns, expected {len(logical)}"
        )
    for (expected_name, logical_type), (actual_name, physical_type) in zip(
        logical, physical, strict=True
    ):
        normalized = _normalized_duckdb_type(physical_type)
        allowed = _LOGICAL_TO_DUCKDB.get(str(logical_type).upper(), frozenset())
        if str(actual_name).lower() != str(expected_name).lower() or normalized not in allowed:
            raise RawLandingError(
                f"candidate writer: physical schema for {relation!r} is incompatible "
                f"with logical column {(expected_name, logical_type)!r}: "
                f"observed {(actual_name, physical_type)!r}"
            )


class _CandidateDuckDBWriter:
    """DuckDB-shaped writer that suffixes and records candidate relations."""

    def __init__(self, connection, *, schema: str, execution_id: str):
        self._connection = connection
        self._schema = schema
        self._execution_id = execution_id
        self._tables: set[str] = set()
        self._closed = False

    def _rewrite(self, sql: str) -> str:
        from core.raw_landing import candidate_table  # noqa: PLC0415

        match = _CANDIDATE_TABLE_STATEMENT.search(sql or "")
        if match is None:
            return sql
        table = candidate_table(match.group(2), self._execution_id)
        self._tables.add(table)
        return f"{sql[: match.start(2)]}{table}{sql[match.end(2) :]}"

    def execute(self, sql: str, parameters=None):
        rewritten = self._rewrite(sql)
        if parameters is None:
            self._connection.execute(rewritten)
        else:
            self._connection.execute(rewritten, parameters)
        return self

    def executemany(self, sql: str, values):
        self._connection.executemany(self._rewrite(sql), values)
        return self

    def close(self) -> None:
        if self._closed:
            return
        from core.raw_landing import (  # noqa: PLC0415
            RawLandingError,
            landed_candidate_columns,
            record_candidate_landing,
        )

        try:
            for table in sorted(self._tables):
                physical = self._connection.execute(
                    "SELECT column_name,data_type FROM information_schema.columns "
                    "WHERE table_schema=? AND table_name=? ORDER BY ordinal_position",
                    [self._schema, table.lower()],
                ).fetchall()
                if not physical:
                    continue
                # `land_raw_rows` records its logical declaration before opening
                # this writer. Keep it authoritative, but prove the table that
                # already existed is compatible instead of trusting stale DDL.
                logical = landed_candidate_columns(self._execution_id, table)
                if logical:
                    _validate_physical_contract(table, logical, physical)
                    continue
                columns: list[tuple[str, str]] = []
                for name, physical_type in physical:
                    normalized = _normalized_duckdb_type(physical_type)
                    logical = _DUCKDB_TO_LOGICAL.get(normalized)
                    if logical is None:
                        raise RawLandingError(
                            f"candidate writer: DuckDB type {physical_type!r} has no "
                            "publication contract"
                        )
                    columns.append((str(name), logical))
                record_candidate_landing(self._execution_id, table, columns)
        finally:
            self._closed = True
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()
        return False


def _warn_once(key: str, message: str, *args) -> None:
    if key in _WARNED:
        return
    _WARNED.add(key)
    logger.warning(message, *args)


def _reset_warn() -> None:
    """Test seam: reset the warn-once set."""
    _WARNED.clear()


def open_raw_writer(
    duckdb_path: str,
    project_id: str | None = None,
) -> "duckdb.DuckDBPyConnection":  # type: ignore[name-defined]  # noqa: F821
    """Open a DuckDB connection routed to the correct raw schema for *project_id*.

    Flag OFF (``TOOROW_ORG_SCHEMAS`` absent / ``"0"``):
        Returns ``duckdb.connect(duckdb_path)`` on schema ``main`` -- identical
        to the legacy pattern; no Postgres call is made.

    Flag ON (``TOOROW_ORG_SCHEMAS=1``):
        Resolves the org raw schema via ``warehouse_tenancy.resolve_org_schemas``,
        executes ``CREATE SCHEMA IF NOT EXISTS "<raw>"`` (idempotent), sets
        ``search_path`` on the connection, and returns it.  On failure (org
        unresolvable, DB error) degrades to ``main`` + WARNING (never raises).

    Parameters
    ----------
    duckdb_path:
        File-system path to the DuckDB database.
    project_id:
        The platform project id used to resolve the org schema.  When None and
        flag ON, degrades to ``main`` with a one-time WARNING.

    Returns
    -------
    duckdb.DuckDBPyConnection
        An open, writable connection.  The caller is responsible for calling
        ``.close()`` (or using it as a context-manager via ``with``).
    """
    import duckdb  # noqa: PLC0415

    from core import warehouse_tenancy  # noqa: PLC0415

    # A PREVIEW WRITES NOWHERE. Same reasoning as the candidate isolation below,
    # and the same seam: an in-memory DuckDB accepts the module's CREATE TABLE /
    # INSERT unchanged, so a Connector that never heard of previews is previewed
    # anyway -- and no module needs a `dry_run` parameter of its own. The
    # connection is registered on the buffer so the caller can read back what
    # would have landed.
    from core.raw_landing import active_preview_capture  # noqa: PLC0415

    capture = active_preview_capture()
    if capture is not None:
        writer = duckdb.connect(":memory:")
        capture["writers"].append(writer)
        return writer

    # BigQuery deployments get a writer that speaks the same three statements
    # (CREATE TABLE / ALTER TABLE ADD COLUMN / INSERT) and lands them in BigQuery.
    # This is the one seam every converted module already goes through, so the
    # backend changes here instead of in sixty-six landing functions -- each of
    # which would be its own chance to mis-map a column silently.
    if (os.environ.get("TOOROW_DB_MODE") or "duckdb").strip().lower() == "bigquery":
        from core.bigquery_raw_writer import BigQueryRawWriter  # noqa: PLC0415

        return BigQueryRawWriter(project_id=project_id)

    # A candidate execution writes into ITS OWN schema, so the shared raw tables
    # the staging models name are untouched until someone publishes. Done here
    # rather than in each module because `pull()` has a fixed signature across 24
    # connectors: the scope carries the execution, and a module that never heard
    # of candidates is isolated anyway. See core.raw_landing.candidate_execution.
    from core.raw_landing import active_candidate_execution  # noqa: PLC0415

    execution_id = active_candidate_execution()
    if execution_id:
        return _open_candidate_writer(duckdb, duckdb_path, project_id, execution_id)

    if not warehouse_tenancy.org_schemas_enabled():
        # Flag OFF: legacy path, bit-identical.
        return duckdb.connect(duckdb_path)

    # Flag ON: resolve the org schema.
    schemas = warehouse_tenancy.resolve_org_schemas(project_id=project_id)
    if schemas is None:
        _warn_once(
            f"noschema:{project_id}",
            "warehouse_write: org schema unresolvable for project_id=%s"
            " -- falling back to main (legacy)",
            project_id,
        )
        return duckdb.connect(duckdb_path)

    raw_schema = schemas.raw  # name is resolved by warehouse_tenancy, e.g. org_<wslug>_raw
    con = duckdb.connect(duckdb_path)
    try:
        # CREATE SCHEMA IF NOT EXISTS is idempotent (AC6 / F-5).
        # schema name is a trusted identifier from warehouse_tenancy, not user input.
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{raw_schema}"')  # noqa: S608
        con.execute(f'SET search_path="{raw_schema}"')  # noqa: S608
    except Exception as exc:  # noqa: BLE001 -- degradation contract
        _warn_once(
            f"schemaerr:{raw_schema}",
            "warehouse_write: failed to set schema %r -- falling back to main: %s",
            raw_schema,
            exc,
        )
        # Re-force search_path to main so the returned connection is in a known
        # state regardless of which instruction above raised (CREATE SCHEMA could
        # have succeeded before SET failed, or vice-versa).  DuckDB SET is
        # effectively atomic, but being explicit removes any ambiguity.
        try:
            con.execute('SET search_path="main"')  # noqa: S608
        except Exception:  # noqa: BLE001
            pass  # if SET main fails too, the default search_path is still main
    return con


def _open_candidate_writer(duckdb, duckdb_path: str, project_id, execution_id: str):
    """A connection whose search_path is THIS execution's isolated schema.

    The module's own `CREATE TABLE IF NOT EXISTS raw_<x>_daily` then creates that
    table inside the candidate schema, and its INSERT lands there. Staging reads
    the org raw schema and never names this one, so the candidate is invisible to
    the marts by construction -- no dbt model changes, and no module changes.

    The schema name is derived through `raw_landing.candidate_table`, so the
    execution id is mapped the same collision-free way in both backends: two
    distinct executions can never share a schema, which is the whole guarantee.
    """
    from core import warehouse_tenancy  # noqa: PLC0415
    from core.raw_landing import candidate_table  # noqa: PLC0415

    base = "raw"
    if warehouse_tenancy.org_schemas_enabled():
        resolved = warehouse_tenancy.resolve_org_schemas(project_id=project_id)
        if resolved is not None:
            base = resolved.raw
    schema = candidate_table(base, execution_id)

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')  # noqa: S608
        con.execute(f'SET search_path="{schema}"')  # noqa: S608
    except Exception:  # noqa: BLE001
        # Never degrade to the shared schema here. Degrading is right for a normal
        # pull -- the rows belong there anyway -- but for a candidate it would
        # publish unconfirmed numbers, which is the exact failure this exists to
        # prevent. Fail closed.
        con.close()
        raise
    return _CandidateDuckDBWriter(con, schema=schema, execution_id=execution_id)
