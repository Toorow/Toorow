"""toorow -- Per-source binding store: wire the OUTBOUND collection (Story 40.2).

The OUTBOUND half of Epic 40 (competitor / brand registry, E40-AD2). It HANGS OFF the 40.1
ORG entity master (app.tracked_entities) and adds exactly one thing: the per-source BINDING
that says HOW to ask a source about an entity (app.entity_source_bindings). Declaring a
brand ONCE (40.1) + a binding per source wires the outbound queries for every source
AUTOMATICALLY -- the queries are DERIVED from the registry, never re-typed per datastream
(E40-FR03).

  * BINDING (app.entity_source_bindings): ORG-scoped, one row per (entity, source,
    [account_scope]), carrying an OPAQUE source handle, an OPAQUE external_id, and a
    free-form query_spec JSONB blob. This binding is the QUERY DRIVER: a source's pull-setup
    reads it (resolve_source_binding / list_bindings_for_source) to know WHAT to request for
    this brand, without re-typing a query per datastream.

BINDING = ORG (E40-AD1 inherited): the binding hangs off an ORG entity, so it is ORG-scoped
ONLY -- HOW to ask a source about a brand (one opaque per-source id per brand per org) is an
ORG fact; WHETHER a project cares is the 40.1 PROJECT role. It carries NO scope triplet
of its own beyond entity_id: it derives its org from the entity's org_id
(binding.org_id == entity.org_id, a Python store guard on write). scope_level is kept
'ORG'-CHECKed for family parity with 086; a Phase-B PLATFORM/PROJECT binding is a CHECK
relaxation, not a schema fork.

STRICTLY ADDITIVE & PASSIVE (40.2): this module is not wired onto the runtime -- it is
called by no one in 40.2. Query-cost / backfill preview of THIS binding is 40.4; the MCP
governance surface (the governed binding CRUD tool) is 40.5; the connector-side pull-setup
consumption of resolve_source_binding (and each connector declaring HOW it consumes a brand
id in its own descriptor) is each source's own follow-up. The inbound half (alias matching)
is 40.3 and does NOT touch this table. No warehouse row and no existing join is read or
written here (E40-NFR06 satisfied by inaction).

TRUST CONTRACT (S-3, lesson 27.2): this STORE has NO authorization guard BY DESIGN -- EXCEPT
the existence-hiding ownership primitive assert_binding_in_org that MUST live at the store
level. Its other functions assume their arguments are ALREADY authorized -- authorization
(org-read for reads, org-manage for mutations) is the responsibility of the SURFACE (MCP
governance tool, built in 40.5), never the store. Any surface that mutates a binding BY ID
MUST call assert_binding_in_org FIRST with the caller's authorised org.

REUSE of 27.1 / 40.1 (import, NOT copy -- E40-NFR05 / E40-AD3): validate_scope, _mint_id,
_write_semantics_audit, SCOPE_ORG, InvalidScope are IMPORTED from core.metric_semantics
(single source of truth for scope validation, ULID minting and the audit writer); the entity
read get_tracked_entity, the existence-hiding EntityNotFound, and the audit verbs
ACTION_CREATED/ACTION_UPSERTED/ACTION_DELETED are IMPORTED from core.tracked_entity_registry
(40.1 is the single source of truth for the entity, its ownership doctrine and the verbs).
Re-querying the entity table independently, a parallel audit table, or a re-implemented scope
validator would fork the single source of truth and is a REJECT: all mutations write to
app.metric_semantics_audit (049) via _write_semantics_audit.

AD-2 (ZERO provider vocabulary): this module contains NO connector name, NO source name, NO
brand name and NO query-shape keyword. source, external_id and query_spec are org DATA passed
by the caller, never code. The ONLY vocabulary is the STATUS_* product constants mirroring
the SQL CHECK (tolerated by the AD-2 grep, exactly like tracked_entity_registry's ROLE_* and
dimension_conformance's STATUS_* constants). _normalize_source is a SHAPE guard (strip +
non-empty), NOT an allow-list.

Design mirrors tracked_entity_registry.py / metric_semantics.py: ``from __future__ import
annotations``, module logger, lazy ``core.db`` imports inside the DB functions, prefixed-ULID
mint helper, pure functions kept separate from I/O so the invariants are testable without
Postgres.

Windows/CI note: all log/message strings use ASCII-safe characters only.
"""

from __future__ import annotations

import logging

from core.metric_semantics import (  # noqa: F401 -- re-exported socle symbols (27.1)
    SCOPE_ORG,
    InvalidScope,
    _mint_id,
    _write_semantics_audit,
    validate_scope,
)
from core.tracked_entity_registry import (  # 40.1 entity read + existence-hiding + verbs
    ACTION_CREATED,
    ACTION_DELETED,
    ACTION_UPSERTED,
    EntityNotFound,
    get_tracked_entity,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (named so they are configurable later, per the house pattern).
#
# The STATUS_* literals are the ONLY vocabulary in this module and are PRODUCT constants
# mirroring the SQL CHECK -- the AD-2 grep tolerates them explicitly (like
# tracked_entity_registry's ROLE_* and dimension_conformance's STATUS_* constants). No
# provider/source/brand name and no query-shape keyword lives here.
# ---------------------------------------------------------------------------

# entity_type value for the shared audit (app.metric_semantics_audit, 049).
BINDING_ENTITY_TYPE = "entity_source_binding"

_BINDING_PREFIX = "esb_"

STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
_STATUSES = frozenset({STATUS_ACTIVE, STATUS_DISABLED})  # mirror the CHECK


# ---------------------------------------------------------------------------
# Typed errors (callers/endpoints map these; core never raises HTTP here).
# InvalidScope is re-exported from metric_semantics (single source of truth).
# EntityNotFound is re-exported from tracked_entity_registry (40.1) and reused for a
# foreign/absent entity OR binding id (existence-hiding, 404-semantic).
# ---------------------------------------------------------------------------


class SourceBindingError(Exception):
    """Base for source-binding validation errors."""


class InvalidSource(SourceBindingError):
    """The source handle is empty/whitespace, or a status is outside the CHECK. A SHAPE
    guard, NOT a provider allow-list -- the source string itself is opaque org DATA."""


# ---------------------------------------------------------------------------
# Row <-> dict helpers + the column list (mirror tracked_entity_registry.py).
# ---------------------------------------------------------------------------

_BINDING_COLUMNS = (
    "id, entity_id, org_id, scope_level, source, account_scope, external_id, "
    "query_spec, status, created_by, created_at, updated_at"
)

_BINDING_TS_COLS = {"created_at", "updated_at"}


def _row_to_binding(cols, row) -> dict:
    record: dict = {}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _BINDING_TS_COLS and val is not None else val
    return record


# Fields carrying no semantic change (identity/timestamps) -- excluded from the idempotency
# comparison so a re-UPSERT with identical content emits NO 'upserted' audit.
_BINDING_VOLATILE = frozenset({"id", "created_at", "updated_at"})


def _binding_changed(before: dict | None, after: dict) -> bool:
    """True when the semantic content of a binding changed (idempotency guard).

    On first write ``before`` is None -> changed. Otherwise compare every non-volatile
    field; a re-UPSERT with identical content returns False so no 'upserted' audit is written
    (import idempotency). query_spec is compared on its parsed dict value so a JSONB
    round-trip (key reordering) does NOT spuriously diverge (F-2 lesson 27.1)."""
    if before is None:
        return True
    for key, value in after.items():
        if key in _BINDING_VOLATILE:
            continue
        if before.get(key) != value:
            return True
    return False


def _normalize_source(source: str | None) -> str:
    """Return the stripped, non-empty source handle, else raise InvalidSource.

    A SHAPE guard ONLY (strip + non-empty): the source is an OPAQUE org handle (a module
    name), NEVER normalized against any allow-list -- core does not interpret it (AD-2)."""
    if source is None:
        raise InvalidSource("source handle is required (non-empty opaque string)")
    stripped = source.strip()
    if not stripped:
        raise InvalidSource("source handle must be a non-empty opaque string")
    return stripped


# ---------------------------------------------------------------------------
# B.3 Binding CRUD (ORG-scoped) -- resolve entity -> read-before -> UPSERT -> same-tx audit.
# ---------------------------------------------------------------------------


def _select_binding(conn, *, entity_id, source, account_scope) -> dict | None:
    """SELECT the binding row at (entity_id, source, COALESCE(account_scope,'')), or None."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_BINDING_COLUMNS}
            FROM app.entity_source_bindings
            WHERE entity_id = %s AND source = %s
              AND COALESCE(account_scope, '') = COALESCE(%s, '')
            """,
            (entity_id, source, account_scope),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_binding(cols, row)


def _select_binding_by_id(conn, binding_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_BINDING_COLUMNS} FROM app.entity_source_bindings WHERE id = %s",
            (binding_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_binding(cols, row)


def upsert_source_binding(
    *,
    entity_id: str,
    source: str,
    created_by: str,
    external_id: str | None = None,
    query_spec: dict | None = None,
    account_scope: str | None = None,
    status: str = STATUS_ACTIVE,
) -> dict:
    """UPSERT one ORG binding keyed on (entity_id, source, COALESCE(account_scope,'')) + audit.

    Guards, in order (fail-closed BEFORE any write):
      1. source stripped non-empty else InvalidSource (SHAPE guard, unit-testable offline).
      2. status in _STATUSES else InvalidSource (mirrors the CHECK).
      3. the entity exists (get_tracked_entity) else EntityNotFound (existence-hiding); the
         binding's org_id is set FROM the entity's org_id (never a caller-supplied org that
         disagrees -- binding.org_id == entity.org_id, E40-NFR03 org integrity).
    validate_scope(SCOPE_ORG, org_id, None) mirrors the SQL CHECK on the derived org.
    Emits 'created' on the first write, 'upserted' when an existing row changed (idempotency
    via _binding_changed; a JSONB round-trip is stable). source / external_id / query_spec
    are stored VERBATIM (opaque, AD-2). Returns the row. This is the query driver a
    pull-setup reads (E40-FR03)."""
    import json  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    source = _normalize_source(source)
    if status not in _STATUSES:
        raise InvalidSource(f"status must be one of {sorted(_STATUSES)}: {status!r}")

    entity = get_tracked_entity(entity_id)
    if entity is None:
        # Existence-hiding: an unknown/foreign entity is indistinguishable from absent.
        raise EntityNotFound(f"tracked_entity not found: {entity_id!r}")
    org_id = entity["org_id"]
    validate_scope(SCOPE_ORG, org_id, None)

    new_id = _mint_id(_BINDING_PREFIX)
    spec_json = json.dumps(query_spec if query_spec is not None else {})

    with get_connection() as conn:
        before = _select_binding(
            conn, entity_id=entity_id, source=source, account_scope=account_scope
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.entity_source_bindings
                    (id, entity_id, org_id, scope_level, source, account_scope,
                     external_id, query_spec, status, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, 'ORG', %s, %s, %s, %s::jsonb, %s, %s, now(), now())
                ON CONFLICT (entity_id, source, COALESCE(account_scope, ''))
                DO UPDATE SET
                    external_id = EXCLUDED.external_id,
                    query_spec  = EXCLUDED.query_spec,
                    status      = EXCLUDED.status,
                    updated_at  = now()
                RETURNING {_BINDING_COLUMNS}
                """,
                (
                    new_id,
                    entity_id,
                    org_id,
                    source,
                    account_scope,
                    external_id,
                    spec_json,
                    status,
                    created_by,
                ),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_binding(cols, row)

        if _binding_changed(before, after):
            _write_semantics_audit(
                conn,
                identity=created_by,
                action=ACTION_CREATED if before is None else ACTION_UPSERTED,
                entity_type=BINDING_ENTITY_TYPE,
                entity_id=after["id"],
                scope_level=SCOPE_ORG,
                org_id=org_id,
                project_id=None,
                before=before,
                after=after,
            )
        conn.commit()
    return after


def get_source_binding(binding_id: str) -> dict | None:
    """Return the binding row for *binding_id*, or None."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        return _select_binding_by_id(conn, binding_id)


def assert_binding_in_org(binding_id: str, org_id: str) -> dict:
    """Return the binding IF its entity belongs to *org_id*, else raise EntityNotFound (S-2).

    404-SEMANTIC ownership check (existence-hiding, NOT 403 -- reuse the 40.1
    assert_entity_in_org / 27.4 assert_mapping_in_org doctrine): a foreign-org binding is
    INDISTINGUISHABLE from an absent id (both raise EntityNotFound), so a caller cannot probe
    foreign ids. The owning org is read from the binding's own denormalised org_id
    (cross-checked == entity.org_id on write). ANY surface (40.5) that mutates a binding BY
    ID MUST call this FIRST with the caller's authorised org."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        row = _select_binding_by_id(conn, binding_id)
    if row is None or row.get("org_id") != org_id:
        # Foreign or absent -> indistinguishable (existence-hiding).
        raise EntityNotFound(f"entity_source_binding not found: {binding_id!r}")
    return row


def delete_source_binding(binding_id: str, *, identity: str) -> bool:
    """Delete + audit ('deleted', after=None). Returns True if a row went.

    An entity delete already CASCADEs its bindings (a deleted brand carries its bindings with
    it) -- this is the explicit single-binding removal path."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        before = _select_binding_by_id(conn, binding_id)
        if before is None:
            return False
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.entity_source_bindings WHERE id = %s", (binding_id,)
            )
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_DELETED,
            entity_type=BINDING_ENTITY_TYPE,
            entity_id=before["id"],
            scope_level=SCOPE_ORG,
            org_id=before["org_id"],
            project_id=None,
            before=before,
            after=None,
        )
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# B.4 Read surfaces (the query driver -- offline-testable shaping).
# ---------------------------------------------------------------------------


def resolve_source_binding(
    entity_id: str, source: str, account_scope: str | None = None
) -> dict | None:
    """Return the single binding for (entity, source[, account_scope]) as the query driver a
    pull-setup reads, or None if unbound. Source-agnostic: returns the opaque external_id /
    query_spec VERBATIM -- core never interprets them (E40-FR03, AD-2)."""
    from core.db import get_connection  # noqa: PLC0415

    source = _normalize_source(source)
    with get_connection() as conn:
        return _select_binding(
            conn, entity_id=entity_id, source=source, account_scope=account_scope
        )


def list_bindings_for_entity(entity_id: str) -> list[dict]:
    """Every source binding of one entity (ordered by source, account_scope). One brand's
    full outbound wiring across all sources."""
    from core.db import get_connection  # noqa: PLC0415

    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_BINDING_COLUMNS}
                FROM app.entity_source_bindings
                WHERE entity_id = %s
                ORDER BY source, COALESCE(account_scope, '')
                """,
                (entity_id,),
            )
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(_row_to_binding(cols, row))
    return rows


def list_bindings_for_source(
    org_id: str, source: str, *, status: str | None = None
) -> list[dict]:
    """Every entity's binding for one source of one org (the DERIVE-THE-QUERIES surface,
    AC2): a source's pull-setup reads this to know every brand it must request, instead of
    re-typing a query per datastream. Scoped to ONE org; there is NO all-orgs / cross-org
    enumerator. ORG-guarded at the surface (40.5)."""
    from core.db import get_connection  # noqa: PLC0415

    source = _normalize_source(source)
    clauses = ["org_id = %s", "source = %s"]
    params: list = [org_id, source]
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    where = " AND ".join(clauses)

    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_BINDING_COLUMNS}
                FROM app.entity_source_bindings
                WHERE {where}
                ORDER BY entity_id, COALESCE(account_scope, '')
                """,
                params,
            )
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(_row_to_binding(cols, row))
    return rows
