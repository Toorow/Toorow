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

logger = logging.getLogger(__name__)

# Warn-once set (mirrors warehouse_tenancy._WARNED pattern, scoped to this module).
_WARNED: set[str] = set()


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
