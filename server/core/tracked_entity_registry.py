"""toorow -- Tracked-brand entity master + project role store (Story 40.1).

The DATA-MODEL FOUNDATION of Epic 40 (competitor / brand registry). It SPECIALISES the
metric-semantics socle (Epic 27) and the conformed-dimension layer (Story 27.4): the
"tracked brand" conformed dimension is promoted to a first-class master-data ENTITY at
the ORG anchor (app.tracked_entities), with a PROJECT-scoped ROLE on top
(app.entity_project_roles). Two layers, one shared audit registry:

  * ENTITY (app.tracked_entities): ORG-scoped master, one canonical record per brand per
    org (canonical_name + flat aliases), reusable by every project of the org (E40-FR01).
  * ROLE (app.entity_project_roles): PROJECT-scoped, one row per (entity, project) giving
    the project's view (own|competitor|reference), behind a HARD confidentiality boundary
    (E40-NFR01) -- a project sees ONLY its own role assignments and can never enumerate a
    sibling project's tracked brands. The client's OWN brand is an ordinary entity
    carrying an 'own' role -- a first-class entity, not a special case (E40-FR02, E40-AD1).

ENTITY = ORG, ROLE = PROJECT (E40-AD1): the entity is anchored at the ORG (validate_scope
with SCOPE_ORG); the role is a FLAT PROJECT fact (one row per pair), NOT a
PLATFORM>ORG>PROJECT cascade. resolve_entity_for_project is a flat lookup, never a cascade.

STRICTLY ADDITIVE & PASSIVE (40.1): this module is not wired onto the runtime. Per-source
binding is 40.2; inbound alias matching + the governed new-entity alert (Epic 13) is 40.3;
the MCP governance surface is 40.5. No warehouse row and no existing join is read or
written here (E40-NFR06 satisfied by inaction).

TRUST CONTRACT (S-3, lesson 27.2): this STORE has NO authorization guard BY DESIGN --
EXCEPT the existence-hiding ownership primitives that MUST live at the store level
(assert_entity_in_org / assert_role_visible_to_project). Its other functions assume their
arguments are ALREADY authorized -- authorization (org-read for reads, org-manage for
mutations) is the responsibility of the SURFACE (REST route / MCP tool, built in 40.5),
never the store. Any surface that mutates an entity/role BY ID MUST call the ownership
primitive FIRST with the caller's authorised org/project.

REUSE of 27.1 / 27.4 (import, NOT copy -- E40-NFR05 / E40-AD3): validate_scope, _mint_id,
_write_semantics_audit, SCOPE_ORG, SCOPE_PROJECT, InvalidScope are IMPORTED from
core.metric_semantics (single source of truth for scope validation, ULID minting and the
audit writer); normalize_value is IMPORTED from core.dimension_conformance (alias
normalization, 27.4). Decision (mirror dimension_conformance.py): a direct import of the
underscore helpers is chosen over a local wrapper -- they are stable socle symbols and
copy-pasting would fork the single source of truth. A second audit table is a REJECT: all
mutations write to app.metric_semantics_audit (049) via _write_semantics_audit.

AD-2 (ZERO provider vocabulary): this module contains NO connector name and NO brand name.
Canonical names, aliases and roles are org/project DATA, never code. The ONLY vocabulary
is the ROLE_* product constants mirroring the SQL CHECK (tolerated by the AD-2 grep,
exactly like dimension_conformance's status constants).

Design mirrors dimension_conformance.py / metric_semantics.py: ``from __future__ import
annotations``, module logger, lazy ``core.db`` imports inside the DB functions,
prefixed-ULID mint helper, pure functions kept separate from I/O so the invariants are
testable without Postgres.

Windows/CI note: all log/message strings use ASCII-safe characters only.
"""

from __future__ import annotations

import logging

from core.dimension_conformance import normalize_value  # alias normalization (27.4)
from core.metric_semantics import (  # noqa: F401 -- re-exported socle symbols (27.1)
    SCOPE_ORG,
    SCOPE_PROJECT,
    InvalidScope,
    _mint_id,
    _write_semantics_audit,
    validate_scope,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (named so they are configurable later, per the house pattern).
#
# The ROLE_* literals are the ONLY vocabulary in this module and are PRODUCT constants
# mirroring the SQL CHECK -- the AD-2 grep tolerates them explicitly (like
# dimension_conformance's STATUS_* constants). No provider/brand name lives here.
# ---------------------------------------------------------------------------

ROLE_OWN = "own"
ROLE_COMPETITOR = "competitor"
ROLE_REFERENCE = "reference"
_ROLES = frozenset({ROLE_OWN, ROLE_COMPETITOR, ROLE_REFERENCE})  # mirror the CHECK

# entity_type values for the shared audit (app.metric_semantics_audit, 049).
ENTITY_TYPE = "tracked_entity"
ROLE_ENTITY_TYPE = "entity_project_role"

_ENTITY_PREFIX = "tent_"
_ROLE_PREFIX = "epr_"

# Audit action verbs (the shared writer composes '<entity_type>.<verb>').
ACTION_CREATED = "created"
ACTION_UPSERTED = "upserted"
ACTION_DELETED = "deleted"


# ---------------------------------------------------------------------------
# Typed errors (callers/endpoints map these; core never raises HTTP here).
# InvalidScope is re-exported from metric_semantics (single source of truth).
# ---------------------------------------------------------------------------


class TrackedEntityError(Exception):
    """Base for tracked-entity validation errors."""


class EntityNotFound(TrackedEntityError):
    """Existence-hiding: a foreign-org entity (or a sibling project's role id) is
    indistinguishable from an absent id (S-2, lesson IDOR 27.2 F-1). Raised as a
    404-SEMANTIC refusal, NEVER a 403 that would leak existence."""


class InvalidRole(TrackedEntityError):
    """The role literal is not one of own|competitor|reference (mirrors the CHECK)."""


class CrossProjectDenied(TrackedEntityError):
    """A role mutation targeted a project whose org differs from the entity's org
    (E40-NFR01 org integrity): a project can only role an entity of ITS own org."""


# ---------------------------------------------------------------------------
# Row <-> dict helpers + the column lists (mirror metric_semantics.py).
# ---------------------------------------------------------------------------

_ENTITY_COLUMNS = (
    "id, org_id, scope_level, canonical_name, display_name, aliases, entity_kind, "
    "status, created_by, approved_by, approved_at, created_at, updated_at"
)

_ROLE_COLUMNS = (
    "id, entity_id, project_id, role, created_by, created_at, updated_at"
)

_ENTITY_TS_COLS = {"approved_at", "created_at", "updated_at"}
_ROLE_TS_COLS = {"created_at", "updated_at"}


def _row_to_entity(cols, row) -> dict:
    record: dict = {}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _ENTITY_TS_COLS and val is not None else val
    return record


def _row_to_role(cols, row) -> dict:
    record: dict = {}
    for col, val in zip(cols, row):
        record[col] = val.isoformat() if col in _ROLE_TS_COLS and val is not None else val
    return record


# Fields carrying no semantic change (identity/timestamps) -- excluded from the
# idempotency comparison so a re-UPSERT with identical content emits NO 'upserted' audit.
_ENTITY_VOLATILE = frozenset({"id", "created_at", "updated_at"})
_ROLE_VOLATILE = frozenset({"id", "created_at", "updated_at"})


def _entity_changed(before: dict | None, after: dict) -> bool:
    """True when the semantic content of an entity changed (idempotency guard).

    On first write ``before`` is None -> changed. Otherwise compare every non-volatile
    field; a re-UPSERT with identical content returns False so no 'upserted' audit is
    written (import idempotency)."""
    if before is None:
        return True
    for key, value in after.items():
        if key in _ENTITY_VOLATILE:
            continue
        if before.get(key) != value:
            return True
    return False


def _role_changed(before: dict | None, after: dict) -> bool:
    """True when the semantic content of a role row changed (idempotency guard)."""
    if before is None:
        return True
    for key, value in after.items():
        if key in _ROLE_VOLATILE:
            continue
        if before.get(key) != value:
            return True
    return False


# ---------------------------------------------------------------------------
# Alias helpers (PURE, offline-testable). Flat de-duplication on the NORMALIZED form
# (normalize_value reuse, 27.4) while preserving the VERBATIM strings and their order.
# Fuzzy matching + negative aliases + provenance-per-alias are 40.3 -- NOT here.
# ---------------------------------------------------------------------------


def normalize_alias(alias: str) -> str:
    """Return the normalized matching key of *alias* (REUSE dimension_conformance, 27.4)."""
    return normalize_value(alias)


def _dedupe_aliases(aliases: list[str] | None) -> list[str]:
    """De-duplicate on the normalized form, order-stable, keeping the verbatim strings.

    The match layer (40.3) owns fuzzy matching; 40.1 only stores a flat list with no two
    entries that normalize to the same key (a value and its lowercase twin collapse to one).
    An alias that normalizes to empty (whitespace-only) is dropped."""
    seen: set[str] = set()
    out: list[str] = []
    for alias in aliases or []:
        if alias is None:
            continue
        key = normalize_alias(alias)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(alias)
    return out


# ---------------------------------------------------------------------------
# B.3 Entity CRUD (ORG-scoped) -- read-before -> UPSERT -> same-transaction audit.
# ---------------------------------------------------------------------------


def _select_entity(conn, *, org_id, canonical_name) -> dict | None:
    """SELECT the entity row at (org_id, canonical_name), or None (unicity key)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_ENTITY_COLUMNS}
            FROM app.tracked_entities
            WHERE org_id = %s AND canonical_name = %s
            """,
            (org_id, canonical_name),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_entity(cols, row)


def _select_entity_by_id(conn, entity_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_ENTITY_COLUMNS} FROM app.tracked_entities WHERE id = %s",
            (entity_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_entity(cols, row)


def upsert_tracked_entity(
    *,
    org_id: str,
    canonical_name: str,
    created_by: str,
    display_name: str | None = None,
    aliases: list[str] | None = None,
    status: str = "approved",
) -> dict:
    """UPSERT one ORG entity keyed on (org_id, canonical_name) + audit. Returns the row.

    validate_scope(SCOPE_ORG, org_id, None) first (reject a NULL org). Emits 'created' on
    the first write, 'upserted' when an existing row changed (idempotency via
    _entity_changed). Aliases are normalized/de-duplicated (order-stable) but stored
    VERBATIM -- the match layer (40.3) owns fuzzy matching; 40.1 stores the flat list only.
    The org owns exactly ONE canonical record per brand (a second create with the same
    (org_id, canonical_name) UPSERTs the SAME row -- no duplicate, E40-FR01/AC1)."""
    import json  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    validate_scope(SCOPE_ORG, org_id, None)
    new_id = _mint_id(_ENTITY_PREFIX)
    aliases_json = json.dumps(_dedupe_aliases(aliases))

    with get_connection() as conn:
        before = _select_entity(conn, org_id=org_id, canonical_name=canonical_name)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.tracked_entities
                    (id, org_id, scope_level, canonical_name, display_name, aliases,
                     entity_kind, status, created_by, created_at, updated_at)
                VALUES (%s, %s, 'ORG', %s, %s, %s::jsonb, 'brand', %s, %s, now(), now())
                ON CONFLICT (org_id, canonical_name)
                DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    aliases      = EXCLUDED.aliases,
                    status       = EXCLUDED.status,
                    updated_at   = now()
                RETURNING {_ENTITY_COLUMNS}
                """,
                (
                    new_id,
                    org_id,
                    canonical_name,
                    display_name,
                    aliases_json,
                    status,
                    created_by,
                ),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_entity(cols, row)

        if _entity_changed(before, after):
            _write_semantics_audit(
                conn,
                identity=created_by,
                action=ACTION_CREATED if before is None else ACTION_UPSERTED,
                entity_type=ENTITY_TYPE,
                entity_id=after["id"],
                scope_level=SCOPE_ORG,
                org_id=org_id,
                project_id=None,
                before=before,
                after=after,
            )
        conn.commit()
    return after


def get_tracked_entity(entity_id: str) -> dict | None:
    """Return the entity row for *entity_id*, or None."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        return _select_entity_by_id(conn, entity_id)


def assert_entity_in_org(entity_id: str, org_id: str) -> dict:
    """Return the entity IF it belongs to *org_id*, else raise EntityNotFound (S-2).

    404-SEMANTIC ownership check (existence-hiding, NOT 403 -- lesson IDOR 27.2 F-1,
    mirrors dimension_conformance.assert_mapping_in_org): a foreign-org entity is
    INDISTINGUISHABLE from an absent id (both raise EntityNotFound), so a caller cannot
    probe foreign ids. ANY surface (REST/MCP, 40.5) that mutates an entity BY ID MUST call
    this FIRST with the caller's authorised org."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        row = _select_entity_by_id(conn, entity_id)
    if row is None or row.get("org_id") != org_id:
        # Foreign or absent -> indistinguishable (existence-hiding).
        raise EntityNotFound(f"tracked_entity not found: {entity_id!r}")
    return row


def list_tracked_entities_by_org(org_id: str, *, status: str | None = None) -> list[dict]:
    """List the org's entities (ordered by canonical_name). The ORG-level enumeration --
    scoped to ONE org; there is NO all-orgs enumerator.

    This is the ORG-GUARDED entity picker (org-internal discovery); it is NOT the
    project-facing confidentiality surface. The confidentiality boundary (E40-NFR01) is
    between SIBLING PROJECTS, not between a project and its own org's entity master; a
    surface calling this MUST have checked org access first (40.5)."""
    from core.db import get_connection  # noqa: PLC0415

    clauses = ["org_id = %s"]
    params: list = [org_id]
    if status is not None:
        clauses.append("status = %s")
        params.append(status)
    where = " AND ".join(clauses)
    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {_ENTITY_COLUMNS}
                FROM app.tracked_entities
                WHERE {where}
                ORDER BY canonical_name
                """,
                params,
            )
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rows.append(_row_to_entity(cols, row))
    return rows


def add_alias(entity_id: str, alias: str, *, identity: str) -> dict:
    """Append *alias* to the entity's aliases (additive, de-duped on normalized form) +
    audit. Re-adding an existing alias (same normalized form) is a NO-OP (no audit).
    Negative aliases + match-driven alias curation are 40.3 -- NOT here.

    Raises EntityNotFound when the id is unknown (existence-hiding on reads is the
    ownership primitive's job; here an unknown id is simply absent)."""
    return _mutate_aliases(entity_id, identity=identity, add=alias, remove=None)


def remove_alias(entity_id: str, alias: str, *, identity: str) -> dict:
    """Remove *alias* (matched on normalized form) from the entity's aliases + audit.
    Removing an absent alias is a NO-OP (no audit). Raises EntityNotFound if id unknown."""
    return _mutate_aliases(entity_id, identity=identity, add=None, remove=alias)


def _mutate_aliases(
    entity_id: str, *, identity: str, add: str | None, remove: str | None
) -> dict:
    """Shared add/remove alias mutation (read before -> UPDATE aliases -> audit -> commit).

    Idempotent: an add of an already-present alias (or a remove of an absent one) leaves
    the list unchanged -> no UPDATE, no audit. Returns the (possibly unchanged) row."""
    import json  # noqa: PLC0415

    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        before = _select_entity_by_id(conn, entity_id)
        if before is None:
            raise EntityNotFound(f"tracked_entity not found: {entity_id!r}")

        current = list(before.get("aliases") or [])
        new_aliases = list(current)
        if add is not None:
            new_aliases = _dedupe_aliases([*new_aliases, add])
        if remove is not None:
            remove_key = normalize_alias(remove)
            new_aliases = [a for a in new_aliases if normalize_alias(a) != remove_key]

        if new_aliases == current:
            return before  # no-op: no UPDATE, no audit.

        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE app.tracked_entities
                SET aliases = %s::jsonb, updated_at = now()
                WHERE id = %s
                RETURNING {_ENTITY_COLUMNS}
                """,
                (json.dumps(new_aliases), entity_id),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_entity(cols, row)

        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_UPSERTED,
            entity_type=ENTITY_TYPE,
            entity_id=after["id"],
            scope_level=SCOPE_ORG,
            org_id=after["org_id"],
            project_id=None,
            before=before,
            after=after,
        )
        conn.commit()
    return after


def delete_tracked_entity(entity_id: str, *, identity: str) -> bool:
    """Delete + audit ('deleted', after=None). Returns True if a row went.

    ON DELETE CASCADE removes the entity's entity_project_roles too (a deleted brand
    carries its per-project roles with it)."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        before = _select_entity_by_id(conn, entity_id)
        if before is None:
            return False
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.tracked_entities WHERE id = %s", (entity_id,))
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_DELETED,
            entity_type=ENTITY_TYPE,
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
# B.4 Role CRUD (PROJECT-scoped) + cross-org guard + confidentiality boundary.
# ---------------------------------------------------------------------------


def _project_org_id(conn, project_id: str) -> str | None:
    """Read app.projects.org_id for *project_id* on an EXISTING connection (None if
    unknown). Mirrors dimension_conformance._project_org_id but stays on the caller's
    transaction so the cross-org guard reads entity + project org in one transaction."""
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _select_role(conn, *, entity_id, project_id) -> dict | None:
    """SELECT the role row at (entity_id, project_id), or None (unicity key)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_ROLE_COLUMNS}
            FROM app.entity_project_roles
            WHERE entity_id = %s AND project_id = %s
            """,
            (entity_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_role(cols, row)


def _select_role_by_id(conn, role_id: str) -> dict | None:
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {_ROLE_COLUMNS} FROM app.entity_project_roles WHERE id = %s",
            (role_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [desc[0] for desc in cur.description]
    return _row_to_role(cols, row)


def set_entity_project_role(
    *, entity_id: str, project_id: str, role: str, created_by: str
) -> dict:
    """UPSERT the (entity, project) role keyed on (entity_id, project_id) + audit.

    Guards, in order (fail-closed BEFORE any write):
      1. role in _ROLES else InvalidRole (mirrors the CHECK, unit-testable offline).
      2. the entity exists (else EntityNotFound) AND entity.org_id == project.org_id else
         CrossProjectDenied (a project only roles an entity of ITS org -- E40-NFR01 org
         integrity). An unknown project -> EntityNotFound (existence-hiding).
    The same entity + a different project = a new row (own here, competitor there --
    E40-FR02/AC2). Re-roling a pair to a new role UPSERTs (one 'upserted' audit);
    re-setting the SAME role emits no audit (idempotency via _role_changed)."""
    from core.db import get_connection  # noqa: PLC0415

    if role not in _ROLES:
        raise InvalidRole(f"role must be one of {sorted(_ROLES)}: {role!r}")

    new_id = _mint_id(_ROLE_PREFIX)

    with get_connection() as conn:
        entity = _select_entity_by_id(conn, entity_id)
        if entity is None:
            # Existence-hiding: an unknown entity is indistinguishable from a foreign one.
            raise EntityNotFound(f"tracked_entity not found: {entity_id!r}")
        project_org = _project_org_id(conn, project_id)
        if project_org is None:
            # Unknown project -> existence-hiding refusal (never leak whether it exists).
            raise EntityNotFound(f"project not found: {project_id!r}")
        if entity.get("org_id") != project_org:
            # Cross-org: the project's org differs from the entity's org (E40-NFR01).
            raise CrossProjectDenied(
                "a project can only role an entity of its own org "
                f"(entity org != project org): entity={entity_id!r} project={project_id!r}"
            )

        before = _select_role(conn, entity_id=entity_id, project_id=project_id)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO app.entity_project_roles
                    (id, entity_id, project_id, role, created_by, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (entity_id, project_id)
                DO UPDATE SET
                    role       = EXCLUDED.role,
                    updated_at = now()
                RETURNING {_ROLE_COLUMNS}
                """,
                (new_id, entity_id, project_id, role, created_by),
            )
            row = cur.fetchone()
            cols = [desc[0] for desc in cur.description]
        after = _row_to_role(cols, row)

        if _role_changed(before, after):
            _write_semantics_audit(
                conn,
                identity=created_by,
                action=ACTION_CREATED if before is None else ACTION_UPSERTED,
                entity_type=ROLE_ENTITY_TYPE,
                entity_id=after["id"],
                scope_level=SCOPE_PROJECT,
                org_id=None,
                project_id=project_id,
                before=before,
                after=after,
            )
        conn.commit()
    return after


def get_entity_project_role(entity_id: str, project_id: str) -> dict | None:
    """Return the role row at (entity_id, project_id), or None."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        return _select_role(conn, entity_id=entity_id, project_id=project_id)


def list_project_roles(project_id: str) -> list[dict]:
    """The CONFIDENTIALITY-SAFE listing (E40-NFR01, AC3): the entities THIS project roles,
    joined to their tracked_entities, ordered by canonical_name. Scoped on project_id
    ONLY -- it CANNOT return another project's tracked brands.

    This is the SOLE project-facing enumeration surface; there is deliberately NO
    list_projects_tracking_entity() confidentiality-crossing twin at the project layer.
    Returns ONLY roled rows (strict AC3): each dict carries the role fields plus the joined
    entity fields (entity_canonical_name, entity_display_name, entity_aliases,
    entity_status). The org's un-roled entities are NOT surfaced here -- the entity picker
    is list_tracked_entities_by_org, org-guarded at the surface (orchestrator decision,
    deferred to 40.5)."""
    from core.db import get_connection  # noqa: PLC0415

    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.id, r.entity_id, r.project_id, r.role, r.created_by,
                       r.created_at, r.updated_at,
                       e.canonical_name AS entity_canonical_name,
                       e.display_name   AS entity_display_name,
                       e.aliases        AS entity_aliases,
                       e.status         AS entity_status
                FROM app.entity_project_roles r
                JOIN app.tracked_entities e ON e.id = r.entity_id
                WHERE r.project_id = %s
                ORDER BY e.canonical_name
                """,
                (project_id,),
            )
            cols = [desc[0] for desc in cur.description]
            _ts = {"created_at", "updated_at"}
            for row in cur.fetchall():
                record: dict = {}
                for col, val in zip(cols, row):
                    record[col] = val.isoformat() if col in _ts and val is not None else val
                rows.append(record)
    return rows


def assert_role_visible_to_project(role_id: str, project_id: str) -> dict:
    """Return the role row IF role.project_id == project_id, else raise EntityNotFound
    (existence-hiding). The cross-PROJECT twin of assert_entity_in_org: a sibling
    project's role id is INDISTINGUISHABLE from absent -- a project cannot probe which
    entities another project tracks by guessing role ids (E40-NFR01)."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        row = _select_role_by_id(conn, role_id)
    if row is None or row.get("project_id") != project_id:
        # Sibling-project or absent -> indistinguishable (existence-hiding).
        raise EntityNotFound(f"entity_project_role not found: {role_id!r}")
    return row


def delete_entity_project_role(role_id: str, *, identity: str) -> bool:
    """Delete a role by id + audit ('deleted', after=None). True if a row went."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        before = _select_role_by_id(conn, role_id)
        if before is None:
            return False
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.entity_project_roles WHERE id = %s", (role_id,))
        _write_semantics_audit(
            conn,
            identity=identity,
            action=ACTION_DELETED,
            entity_type=ROLE_ENTITY_TYPE,
            entity_id=before["id"],
            scope_level=SCOPE_PROJECT,
            org_id=None,
            project_id=before["project_id"],
            before=before,
            after=None,
        )
        conn.commit()
    return True


# ---------------------------------------------------------------------------
# B.5 Pure resolution -- the project's flat VIEW of an entity (NOT a cascade).
# ---------------------------------------------------------------------------


def resolve_entity_for_project(project_id: str, entity_id: str) -> dict | None:
    """Return {'entity': <tracked_entity row>, 'role': <own|competitor|reference|None>}
    for a project's VIEW of an entity, or None if the entity is not visible to the project.

    Role is a FLAT PROJECT fact (NOT a PLATFORM>ORG>PROJECT cascade -- an entity is ORG, a
    role is PROJECT, there is one role per pair). An entity of the project's OWN org that
    the project has NOT roled resolves with role=None (visible-but-unroled). A foreign-org
    entity resolves to None (invisible -- never a leak of a sibling's role). Fail-soft on
    an unknown project (returns None, never a crash -- mirrors
    dimension_conformance._project_org_id)."""
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            entity = _select_entity_by_id(conn, entity_id)
            if entity is None:
                return None
            project_org = _project_org_id(conn, project_id)
            if project_org is None or entity.get("org_id") != project_org:
                # Unknown project or foreign-org entity -> invisible (no leak).
                return None
            role_row = _select_role(conn, entity_id=entity_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "tracked_entity_registry: resolve fell back to None project=%s entity=%s: %s",
            project_id,
            entity_id,
            exc,
        )
        return None

    return {"entity": entity, "role": role_row.get("role") if role_row else None}
