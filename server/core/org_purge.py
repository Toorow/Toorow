"""toorow -- explicit purge of an organization's dependent rows (RGPD org drop).

Why this module exists
----------------------
``DELETE FROM app.organizations`` cannot succeed on its own: the tenant tree is
held together by ~50 ``ON DELETE RESTRICT`` / ``NO ACTION`` foreign keys spread
over ~40 tables (org -> projects -> datastreams -> executions -> ledger -> ...).
Those RESTRICT rules are DELIBERATE everywhere else: deleting a project must not
silently erase its datastreams, which is exactly what the product's "archive
before delete" guard relies on. Flipping them to CASCADE would fix the org drop
by weakening every other delete path, so the schema is left untouched and the
org-scoped erasure is made EXPLICIT here instead -- reachable only from the
human-gated ``DELETE /api/organizations/{id}`` endpoint (``X-Confirm-Delete``).

How it works
------------
The FK graph is read from ``pg_constraint`` at call time rather than hardcoded,
so a new org-scoped table added by a later migration is purged automatically
instead of silently re-blocking the endpoint months later.

Only BLOCKING edges are traversed (``RESTRICT`` and ``NO ACTION``): children on
``CASCADE`` / ``SET NULL`` are resolved by Postgres itself during the final
``DELETE``, so walking them would be redundant work and extra statements.

Cycles are real in this schema (``datastreams`` <-> ``datastream_executions``
via ``current_published_execution_id``, ``invitations.superseded_by``, ...).
They are broken by NULLing the back-reference before the deletes; every such
column is nullable, and a non-nullable cycle raises rather than half-deleting.

``app.audit_log`` is NEVER deleted -- it is the durable RGPD trace of the
erasure itself. Its ``connection_ref`` back-reference is NULLed instead.

Contract: every statement runs on the CALLER's connection, inside the caller's
open transaction, and nothing is committed here. The caller commits (or rolls
back, leaving the org intact) -- the no-partial-deletion invariant of the
endpoint is preserved.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Tables that must survive the purge of an org (durable traces).
#: A blocking edge pointing INTO one of these is broken by NULLing instead.
PRESERVED_TABLES: frozenset[str] = frozenset({"app.audit_log"})

#: Root of the tenant tree.
ROOT_TABLE = "app.organizations"
ROOT_PREDICATE = "id = %s"

#: Safety bounds -- the traversal is data-independent (it walks the schema, not
#: rows), but a pathological future schema must fail loudly, never hang.
MAX_DEPTH = 16
MAX_OPERATIONS = 800

_FK_GRAPH_SQL = """
SELECT
    c.conname,
    c.conrelid::regclass::text  AS child_table,
    c.confrelid::regclass::text AS parent_table,
    (SELECT array_agg(a.attname ORDER BY k.ord)
       FROM unnest(c.conkey) WITH ORDINALITY k(attnum, ord)
       JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
    ) AS child_cols,
    (SELECT array_agg(a.attname ORDER BY k.ord)
       FROM unnest(c.confkey) WITH ORDINALITY k(attnum, ord)
       JOIN pg_attribute a ON a.attrelid = c.confrelid AND a.attnum = k.attnum
    ) AS parent_cols
FROM pg_constraint c
WHERE c.contype = 'f'
  AND c.confdeltype IN ('a', 'r')          -- NO ACTION / RESTRICT only
  AND c.connamespace = 'app'::regnamespace
"""

_NULLABLE_SQL = """
SELECT c.relname, a.attname, a.attnotnull
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
WHERE c.relnamespace = 'app'::regnamespace
  AND a.attnum > 0
  AND NOT a.attisdropped
"""


@dataclass(frozen=True)
class FKEdge:
    """One blocking foreign key: *child* rows pin *parent* rows in place."""

    conname: str
    child_table: str
    parent_table: str
    child_cols: tuple[str, ...]
    parent_cols: tuple[str, ...]


@dataclass(frozen=True)
class PurgeOp:
    """One planned statement. ``kind`` is ``"null"`` (break) or ``"delete"``."""

    kind: str
    table: str
    sql: str
    conname: str
    depth: int


def _load_graph(conn) -> tuple[dict[str, list[FKEdge]], dict[tuple[str, str], bool]]:
    """Return (parent_table -> blocking edges, (table, col) -> nullable)."""
    edges: dict[str, list[FKEdge]] = {}
    with conn.cursor() as cur:
        cur.execute(_FK_GRAPH_SQL)
        for conname, child, parent, child_cols, parent_cols in cur.fetchall():
            edges.setdefault(parent, []).append(
                FKEdge(
                    conname=conname,
                    child_table=child,
                    parent_table=parent,
                    child_cols=tuple(child_cols or ()),
                    parent_cols=tuple(parent_cols or ()),
                )
            )
        cur.execute(_NULLABLE_SQL)
        nullable = {
            (f"app.{relname}", attname): not attnotnull
            for relname, attname, attnotnull in cur.fetchall()
        }
    # Deterministic order: the plan (and its audit metadata) must not depend on
    # the physical order pg_constraint happens to return.
    for parent in edges:
        edges[parent].sort(key=lambda e: (e.child_table, e.conname))
    return edges, nullable


def _child_predicate(edge: FKEdge, parent_predicate: str) -> str:
    """Rows of ``edge.child_table`` whose FK points at the selected parent rows.

    NULL FK values never match ``IN``, which is the wanted semantics: a row that
    does not reference this org is not part of its tree.
    """
    child_cols = ", ".join(edge.child_cols)
    parent_cols = ", ".join(edge.parent_cols)
    return (
        f"({child_cols}) IN ("
        f"SELECT {parent_cols} FROM {edge.parent_table} WHERE {parent_predicate})"
    )


def _break_sql(edge: FKEdge, columns: list[str], predicate: str) -> str:
    assignments = ", ".join(f"{col} = NULL" for col in columns)
    return f"UPDATE {edge.child_table} SET {assignments} WHERE {predicate}"


def plan_purge(conn, org_id: str) -> list[PurgeOp]:
    """Build the ordered statement plan erasing the tenant tree of *org_id*.

    Pure planning: reads only the catalog, touches no tenant row. Returned order
    is directly executable -- every cycle-breaking UPDATE precedes the DELETEs,
    and DELETEs are emitted deepest-first (post-order DFS over the acyclic
    remainder), so no statement can trip a blocking FK.
    """
    edges, nullable = _load_graph(conn)
    breaks: list[PurgeOp] = []
    deletes: list[PurgeOp] = []
    budget = [MAX_OPERATIONS]

    def visit(table: str, predicate: str, path: tuple[str, ...], depth: int) -> None:
        if depth > MAX_DEPTH:
            raise RuntimeError(
                f"org_purge: FK traversal exceeded depth {MAX_DEPTH} at {table}"
                f" (path: {' -> '.join(path)})"
            )
        for edge in edges.get(table, []):
            budget[0] -= 1
            if budget[0] < 0:
                raise RuntimeError(
                    f"org_purge: plan exceeded {MAX_OPERATIONS} statements"
                    " -- refusing to run a runaway purge"
                )
            predicate_child = _child_predicate(edge, predicate)
            if edge.child_table in PRESERVED_TABLES:
                # A preserved ledger is append-only: it may be neither deleted
                # NOR updated, so it cannot be detached row by row either. A
                # blocking FK from it into the tenant tree is a schema defect,
                # not something to work around at runtime (mig 098 dropped the
                # one that existed). Fail loudly rather than half-erase.
                raise RuntimeError(
                    f"org_purge: {edge.child_table} is preserved but"
                    f" {edge.conname} pins {edge.parent_table} rows in place"
                    " -- that foreign key must be dropped by a migration"
                )
            if edge.child_table in path:
                # The child is already being deleted higher up the path: this
                # edge closes a cycle. Detach the back-reference instead of
                # recursing forever. Under the default MATCH SIMPLE, a composite
                # FK stops being enforced as soon as ONE of its columns is NULL,
                # so only the nullable subset needs clearing -- which is what
                # makes the datastreams <-> datastream_executions cycle
                # breakable at all: its back-reference is
                # (current_published_execution_id, id, project_id) where id and
                # project_id are NOT NULL and must obviously stay set.
                clearable = [
                    col
                    for col in edge.child_cols
                    if nullable.get((edge.child_table, col), False)
                ]
                if not clearable:
                    raise RuntimeError(
                        f"org_purge: cannot break the cycle at {edge.conname} on"
                        f" {edge.child_table}: every FK column is NOT NULL"
                        f" ({', '.join(edge.child_cols)})"
                    )
                breaks.append(
                    PurgeOp(
                        kind="null",
                        table=edge.child_table,
                        sql=_break_sql(edge, clearable, predicate_child),
                        conname=edge.conname,
                        depth=depth,
                    )
                )
                continue
            visit(edge.child_table, predicate_child, path + (edge.child_table,), depth + 1)
            # Post-order: children of this child are already queued before it.
            deletes.append(
                PurgeOp(
                    kind="delete",
                    table=edge.child_table,
                    sql=f"DELETE FROM {edge.child_table} WHERE {predicate_child}",
                    conname=edge.conname,
                    depth=depth,
                )
            )

    visit(ROOT_TABLE, ROOT_PREDICATE, (ROOT_TABLE,), 0)
    # A table reachable by several paths (host_preflights hangs off org,
    # project, operation and setup_task) yields the same statement once per
    # path. Dropping exact duplicates keeps the plan readable and the audit
    # counts honest; order is preserved, so post-order validity is untouched.
    return _dedupe(breaks) + _dedupe(deletes)


def _dedupe(ops: list[PurgeOp]) -> list[PurgeOp]:
    seen: set[str] = set()
    unique: list[PurgeOp] = []
    for op in ops:
        if op.sql in seen:
            continue
        seen.add(op.sql)
        unique.append(op)
    return unique


def purge_org_tree(conn, org_id: str) -> dict[str, Any]:
    """Erase every row hanging off *org_id* -- NOT the org row itself.

    Runs inside the caller's transaction and commits nothing: the caller issues
    the final ``DELETE FROM app.organizations`` and owns the commit/rollback, so
    a failure anywhere leaves the org fully intact.

    Returns ``{"statements": int, "rows_by_table": {table: rows}, "total_rows": int}``.
    Raises on any SQL failure -- the endpoint must NOT report a successful
    erasure it could not perform.
    """
    plan = plan_purge(conn, org_id)
    rows_by_table: dict[str, int] = {}
    detached = 0
    total = 0
    with conn.cursor() as cur:
        # Append-only ledgers inside the tenant tree refuse DELETE unless the
        # erasure flags itself (migration 098, same idiom as the funnel
        # retention purge). SET LOCAL scopes it to the caller's transaction: it
        # disappears on commit AND on rollback, so it can never leak into an
        # unrelated statement or a pooled session.
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        for op in plan:
            cur.execute(op.sql, (org_id,))
            affected = cur.rowcount or 0
            if not affected:
                continue
            if op.kind == "null":
                # A detached back-reference is not an erased row -- counting it
                # in total_rows would overstate the erasure in the audit trail.
                detached += affected
                continue
            rows_by_table[op.table] = rows_by_table.get(op.table, 0) + affected
            total += affected
    logger.info(
        "org_purge: org=%s statements=%d rows_deleted=%d refs_detached=%d tables=%s",
        org_id,
        len(plan),
        total,
        detached,
        sorted(rows_by_table),
    )
    return {
        "statements": len(plan),
        "rows_by_table": rows_by_table,
        "total_rows": total,
        "refs_detached": detached,
    }
