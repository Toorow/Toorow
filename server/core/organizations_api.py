"""L organisation comme objet : la lister, la creer, la lire, la changer, l effacer.

AD-43, 2026-08-12. `/api/organizations/...` portait DIX-NEUF routes et quatre
sujets. Comme `/api/projects` la veille, le prefixe d URL n etait pas la
responsabilite : une invitation, une adhesion et un droit d acces au dataset ne
changent jamais pour la meme raison que l organisation elle-meme.

Ce module tient l objet, sa creation (229 lignes, quatre requetes) et son
effacement -- dont la transaction vit dans `core.org_lifecycle` depuis le premier
pas d AD-43, parce qu effacer n est pas une porte.

Il garde aussi `_org_row_to_dict` et `_mint_org_member_id` : plus aucun lecteur
dans `admin_api`, et une forme de ligne appartient a l objet dont elle est la
ligne.
"""

from __future__ import annotations

import json
import logging
import os
import re

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import (
    declare_action,
    write_audit_row,
)
from core.org_lifecycle import (
    _count_active_memberships,
    _erase_org_transactional,
    _org_deletion_facts,
)
from core.projects_api import (
    # Le motif de slug a suivi son proprietaire ; `_create_org` le lit encore,
    # et ce module importe deja `projects_api` pour ses routes -- pas de cycle.
    _SLUG_RE,
)

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

async def _check_invitation_identity(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_invitation_identity as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _enforce_org_manage(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _enforce_org_manage as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _slugify(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _slugify as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

ACTION_ORG_CREATED = declare_action("org_created")

ACTION_ORG_SCHEMAS_PROVISIONED = declare_action("org_schemas_provisioned")

ACTION_ORG_UPDATED = declare_action("org_updated")

# Story 21.2: hex colour #RRGGBB for org branding.
_HEX_COLOUR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

_ORG_BRAND_COLOUR_FIELDS = ("brand_primary", "brand_secondary", "brand_accent")

async def _list_orgs(request: Request) -> Response:
    """GET /api/organizations -- active orgs the caller may see, name ASC.

    Story 21.5 follow-up (reads scoping): an identity sees an org it belongs to
    (active member) OR an org with zero active members (open, default-open-until-
    enrolled) -- never another tenant's enrolled org. The dev disabled-auth
    "anonymous" subject sees all (single-tenant compat).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    ident = identity or "anonymous"
    mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    show_all = mode == "disabled" and ident == "anonymous"

    production_access = True
    cols_sql = (
        "id, name, slug, status, billing_ref, created_at, updated_at, archived_at, "
        "brand_primary, brand_secondary, brand_accent, logo_url"
    )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                if show_all:
                    cur.execute(
                        f"SELECT {cols_sql} FROM app.organizations "
                        "WHERE status = 'active' ORDER BY name ASC"
                    )
                elif production_access:
                    cur.execute(
                        f"SELECT {cols_sql} FROM app.organizations o "
                        "WHERE o.status = 'active' AND EXISTS ("
                        "  SELECT 1 FROM app.org_members m "
                        "  WHERE m.org_id = o.id AND m.identity = %s "
                        "    AND m.status = 'active'"
                        ") ORDER BY o.name ASC",
                        (ident,),
                    )
                else:
                    # Open orgs (no active member) OR orgs where the caller is an
                    # active member.
                    cur.execute(
                        f"SELECT {cols_sql} FROM app.organizations o "
                        "WHERE o.status = 'active' AND ("
                        "  NOT EXISTS (SELECT 1 FROM app.org_members m "
                        "              WHERE m.org_id = o.id AND m.status = 'active')"
                        "  OR EXISTS (SELECT 1 FROM app.org_members m "
                        "             WHERE m.org_id = o.id AND m.identity = %s "
                        "             AND m.status = 'active')"
                        ") ORDER BY o.name ASC",
                        (ident,),
                    )
                cols = [d[0] for d in cur.description]
                orgs = [_org_row_to_dict(cols, r) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("admin_api: list_orgs db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"organizations": orgs}, status_code=200)

async def _create_org(request: Request) -> Response:
    """POST /api/organizations -- create an organization (AC4).

    Body: {"name": str, "slug": str?, "billing_ref": str?}. Returns 201.
    Auto-generates a unique slug from name when not provided (appends -1, -2 on
    collision); an explicitly supplied duplicate slug is a 409.

    ONE ORGANIZATION PER PERSON -- their own (decision Jean, 2026-07-25). A 409
    ``organization_limit_reached`` when the caller already belongs to one.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    # Shape BEFORE authority: a malformed body is a 422 that no deployment mode,
    # membership cap or database round-trip can change. This validation used to sit
    # after the caps, so a blank name reached the org-membership count and answered
    # 500 "db_error" when Postgres was not reachable -- an input error reported as
    # an outage.
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    name = (body.get("name") or "").strip()
    if not name or len(name) > 100:
        return JSONResponse(
            {"code": "invalid_input", "message": "name is required (max 100 chars)"},
            status_code=422,
        )

    slug_in = (body.get("slug") or "").strip()
    base_slug = slug_in or _slugify(name)
    if not base_slug or not _SLUG_RE.match(base_slug) or len(base_slug) > 50:
        return JSONResponse(
            {"code": "invalid_input", "message": f"invalid slug: {base_slug!r}"},
            status_code=422,
        )

    # Story 21.2 : le branding est de la FORME, comme le nom et le slug -- et il
    # etait reste sous les gardes. Un `#ZZZ` traversait donc le plafond
    # d'organisations et repondait 500 "db_error" quand Postgres n'etait pas
    # joignable, alors que le commentaire ci-dessus promet le contraire depuis sa
    # propre reparation. Meme classe, meme place.
    brand, brand_err = _extract_brand_fields(body)
    if brand_err is not None:
        return brand_err

    from core.deployment_mode import deployment_mode  # noqa: PLC0415

    mode = deployment_mode()
    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if mode == "self_hosted" or auth_mode != "disabled":
        code = "not_found" if mode == "self_hosted" else "entry_scope_required"
        status = 404 if mode == "self_hosted" else 409
        return JSONResponse(
            {
                "code": code,
                "message": (
                    "Not found"
                    if mode == "self_hosted"
                    else "Create the first organization through the hosted ENTRY scope command."
                ),
            },
            status_code=status,
        )
    # Self-service creation is what a newcomer does once, and only once. Being
    # attached to a SECOND organization is an administrative act: a platform
    # admin does it, exactly as the CRM does. Allowing multi-org ACCESS later
    # does not change this -- the cap is on CREATION, not on access.
    #
    # Before this gate, POST /api/organizations checked nothing beyond a valid
    # bearer: one identity could mint organizations without limit, each one
    # provisioning its own warehouse datasets.
    #
    # Counted on BOTH identity keys on purpose. The two paths that create a
    # membership disagree on what they store: this handler writes the token
    # SUBJECT (below), while invitation acceptance writes the verified EMAIL
    # (core/invitations.py). Counting one key would miss memberships created by
    # the other, and the cap would be bypassed by whoever joined by invitation.
    membership_keys = {identity}
    _, verified_email = await _check_invitation_identity(request)
    if verified_email:
        membership_keys.add(verified_email)

    # Self-hosted callers are refused unconditionally just above: claiming a
    # self-hosted instance goes through the one-time bootstrap exchange and
    # /api/instance/claim, never through this legacy endpoint. (The old
    # is_self_hosted() claim block that used to sit here was unreachable behind
    # that guard -- removed under AI-129.)
    from core.super_admin import identity_is_super_admin  # noqa: PLC0415

    # THE one resolution (audit 12, P1-2): the subject is translated to its
    # verified email through `app.person_identities`, and the token's own
    # verified email rides along as a stronger extra key. The membership COUNT
    # below keeps its own two keys -- it answers a different question.
    if not identity_is_super_admin(identity, extra=(verified_email,)):
        existing = _count_active_memberships(membership_keys)
        if existing is None:
            # Fail CLOSED: an unverifiable membership count must not open the
            # gate. Creating here would provision a warehouse we cannot justify.
            return JSONResponse(
                {
                    "code": "db_error",
                    "message": (
                        "Could not verify existing organization membership. Nothing was created."
                    ),
                },
                status_code=500,
            )
        if existing > 0:
            return JSONResponse(
                {
                    "code": "organization_limit_reached",
                    "message": (
                        "You already belong to an organization. A person creates "
                        "their own organization once; being added to another one "
                        "is done by an administrator."
                    ),
                },
                status_code=409,
            )

    billing_ref = body.get("billing_ref")
    billing_ref = str(billing_ref).strip() if billing_ref else None

    org_id = _mint_org_id()
    created_by = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            slug = base_slug
            with conn.cursor() as cur:
                # Story 24.1: collision check on the SANITISED form ('-' -> '_')
                # because the slug names the org's warehouse datasets
                # (org_<wslug>_*, BigQuery charset [A-Za-z0-9_]). _SLUG_RE
                # forbids '_', so two API-created slugs can never collide once
                # sanitised -- this guard is DEFENSIVE depth against slugs that
                # bypassed the API (direct SQL, legacy import) and contain '_'.
                # The slug is immutable after creation (422 slug_immutable).
                _COLLIDES_SQL = (
                    "SELECT 1 FROM app.organizations "
                    "WHERE REPLACE(slug, '-', '_') = REPLACE(%s, '-', '_')"
                )
                if slug_in:
                    cur.execute(_COLLIDES_SQL, (slug,))
                    if cur.fetchone() is not None:
                        return JSONResponse(
                            {
                                "code": "conflict",
                                "message": "slug already exists (or collides once "
                                "sanitised for warehouse dataset naming)",
                            },
                            status_code=409,
                        )
                else:
                    counter = 1
                    while True:
                        cur.execute(_COLLIDES_SQL, (slug,))
                        if cur.fetchone() is None:
                            break
                        slug = f"{base_slug}-{counter}"
                        counter += 1

                cur.execute(
                    """
                    INSERT INTO app.organizations
                        (id, name, slug, status, billing_ref, created_by,
                         brand_primary, brand_secondary, brand_accent, logo_url)
                    VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s, %s)
                    RETURNING id, name, slug, status, billing_ref,
                              created_at, updated_at, archived_at,
                              brand_primary, brand_secondary, brand_accent, logo_url
                    """,
                    (
                        org_id,
                        name,
                        slug,
                        billing_ref,
                        created_by,
                        brand.get("brand_primary"),
                        brand.get("brand_secondary"),
                        brand.get("brand_accent"),
                        brand.get("logo_url"),
                    ),
                )
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
                created = _org_row_to_dict(cols, row)

                # Story 21.5: AUTO-ENROLL the creator as an owner member so the
                # org is immediately scoped to its creator (default-open-until-
                # enrolled). The creator then passes every subsequent manage-check.
                cur.execute(
                    "INSERT INTO app.org_members "
                    "(id, org_id, identity, role, status, joined_at) "
                    "VALUES (%s, %s, %s, 'owner', 'active', NOW()) "
                    "ON CONFLICT (org_id, identity) DO NOTHING",
                    (_mint_org_member_id(), org_id, created_by),
                )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: create_org db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=created_by,
        action=ACTION_ORG_CREATED,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, "slug": created["slug"], "name": name},
    )

    # Story 24.2 (AC1): provision DuckDB schemas non-blocking -- the 201 is
    # always emitted even when DuckDB is unavailable (CI, Cloud Run cold-start).
    # Schema names come from resolve_org_schemas inside provision_org_schemas,
    # never composed inline here (naming guard invariant).
    try:
        from core import warehouse_tenancy as _wt  # noqa: PLC0415

        result = _wt.provision_org_schemas(org_id=org_id, conn=None)
        logger.info("admin_api: provision_schemas org=%s result=%s", org_id, result)
    except Exception as exc:  # noqa: BLE001 -- non-blocking degradation (AC1)
        logger.warning("admin_api: provision_schemas_failed org=%s error=%s", org_id, exc)

    return JSONResponse(created, status_code=201)

async def _get_org(request: Request) -> Response:
    """GET /api/organizations/{org_id} -- single org.

    404 when not found OR when the caller may not see it (reads scoping: a
    non-member of an enrolled org gets 404 -- existence not disclosed, 7.4 pattern).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_org_access  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_org_access(org_id, identity or "anonymous", conn):
                return JSONResponse(
                    {"code": "not_found", "message": "organization not found"},
                    status_code=404,
                )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, slug, status, billing_ref,
                           created_at, updated_at, archived_at,
                           brand_primary, brand_secondary, brand_accent, logo_url
                    FROM app.organizations WHERE id = %s
                    """,
                    (org_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                org = _org_row_to_dict(cols, row)
    except Exception as exc:
        logger.error("admin_api: get_org db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(org, status_code=200)

async def _patch_org(request: Request) -> Response:
    """PATCH /api/organizations/{org_id} -- update name/slug/billing_ref (AC4)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    updates: dict = {}
    if "name" in body:
        name = (body.get("name") or "").strip()
        if not name or len(name) > 100:
            return JSONResponse(
                {"code": "invalid_input", "message": "name must be 1..100 chars"},
                status_code=422,
            )
        updates["name"] = name
    if "slug" in body:
        # Story 24.1 (epic 24, decision 6): the slug names the org's warehouse
        # datasets (org_<wslug>_raw / org_<wslug>_marts) -- immutable after
        # creation. Renaming would orphan the client's data plane.
        return JSONResponse(
            {
                "code": "slug_immutable",
                "message": "slug cannot be changed: it names the organization's "
                "warehouse datasets (epic 24). Create a new organization instead.",
            },
            status_code=422,
        )
    if "billing_ref" in body:
        raw = body.get("billing_ref")
        updates["billing_ref"] = str(raw).strip() if raw else None

    # Story 21.2: branding fields (validated hex; absent = unchanged).
    brand, brand_err = _extract_brand_fields(body)
    if brand_err is not None:
        return brand_err
    updates.update(brand)

    if not updates:
        return JSONResponse(
            {"code": "invalid_input", "message": "no updatable fields provided"},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Existence 404 FIRST (do not disclose manage-state of a missing org),
                # THEN Story 21.5 manage-gate (owner/admin required on an enrolled org).
                cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (org_id,))
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                denied = _enforce_org_manage(org_id, identity, conn, "patch_org")
                if denied is not None:
                    return denied
                set_parts = [f"{col} = %s" for col in updates]
                params = list(updates.values()) + [org_id]
                cur.execute(
                    "UPDATE app.organizations SET "
                    + ", ".join(set_parts)
                    + " WHERE id = %s "
                    + "RETURNING id, name, slug, status, billing_ref, "
                    + "created_at, updated_at, archived_at, "
                    + "brand_primary, brand_secondary, brand_accent, logo_url",
                    params,
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                org = _org_row_to_dict(cols, row)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: patch_org db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ORG_UPDATED,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, "fields": sorted(updates.keys())},
    )
    return JSONResponse(org, status_code=200)

async def _delete_org(request: Request) -> Response:
    """DELETE /api/organizations/{org_id} -- human-gated RGPD drop (Story 24.2 AC5).

    Requires header ``X-Confirm-Delete: drop-warehouse-data`` (422 otherwise).
    Blocks if active projects exist (409).  Drops warehouse schemas first (RGPD:
    if the drop cannot be confirmed, the Postgres row is NOT deleted -- no partial
    deletion).  Two audit entries emitted in order: org_schemas_dropped then
    org_deleted.  The dismantling order itself lives in
    ``_erase_org_transactional``; this handler owns auth, the 404/gate, and the
    single commit.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]

    confirm = request.headers.get("X-Confirm-Delete", "")
    if confirm != "drop-warehouse-data":
        return JSONResponse(
            {
                "code": "confirmation_required",
                "message": (
                    "Include header X-Confirm-Delete: drop-warehouse-data to confirm "
                    "permanent deletion of the organization and its warehouse data."
                ),
            },
            status_code=422,
        )

    # Phase 1: read org metadata + pre-check active projects in one connection.
    # The connection is kept open (not committed) so we can reuse it for the
    # transactional DELETE below, eliminating the TOCTOU window (F-2).
    from core.db import get_connection  # noqa: PLC0415

    # `get_connection()` is a @contextmanager. Calling `.__enter__()` on a
    # TEMPORARY discards the context-manager object itself: the generator is then
    # finalised by the garbage collector, its `finally: conn.close()` fires, and
    # the connection is dead before the first cursor is opened. Holding the
    # reference in `pg_cm` is what keeps it alive until this handler's own
    # `finally` closes it. Symptom before the fix: every DELETE returned 500 with
    # `psycopg.OperationalError: the connection is closed`, so the RGPD org drop
    # was entirely non-functional in production.
    try:
        pg_cm = get_connection()
        pg_conn = pg_cm.__enter__()
    except Exception:
        logger.exception("admin_api: delete_org db_open_failed org=%s", org_id)
        return JSONResponse(
            {"code": "db_error", "message": "database connection failed"},
            status_code=500,
        )

    # ONE try/finally around every path below. The 404, the manage refusal and
    # each 409 return early, and every one of them must still hand the connection
    # back to the context manager that owns it -- before this, a refused delete
    # leaked its connection until the garbage collector noticed.
    try:
        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, slug FROM app.organizations WHERE id = %s",
                    (org_id,),
                )
                org_row = cur.fetchone()
                if org_row is None:
                    pg_conn.rollback()
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                org_name = org_row[1]
                org_slug = org_row[2]

                denied = _enforce_org_manage(org_id, identity, pg_conn, "delete_org")
                if denied is not None:
                    pg_conn.rollback()
                    return denied
        except Exception:
            try:
                pg_conn.rollback()
            except Exception:
                pass
            logger.exception("admin_api: delete_org db_error org=%s", org_id)
            return JSONResponse(
                {"code": "db_error", "message": "database operation failed"},
                status_code=500,
            )

        # Phases 2-4: dismantle the tenant tree, delete the row, drop the
        # warehouse, audit -- all staged on this open transaction (see
        # _erase_org_transactional for the order and why it is that order). It
        # rolls back itself on refusal/failure, so the org stays whole.
        try:
            result, error = _erase_org_transactional(
                pg_conn,
                org_id,
                name=org_name,
                slug=org_slug,
                identity=identity or "anonymous",
            )
            if error is not None:
                return error
            # Single commit: state change AND audit evidence land together.
            pg_conn.commit()
        except Exception:
            try:
                pg_conn.rollback()
            except Exception:
                pass
            logger.exception("admin_api: delete_org audit_commit_failed org=%s", org_id)
            return JSONResponse(
                {"code": "db_error", "message": "database operation failed"},
                status_code=500,
            )
    finally:
        # Close through the context manager that owns the connection, so its own
        # `finally` runs exactly once and nothing is left to the garbage collector.
        try:
            pg_cm.__exit__(None, None, None)
        except Exception:
            pass

    return JSONResponse(
        {"deleted": True, "org_id": org_id, "removed": result["removed"]},
        status_code=200,
    )

async def _provision_org_warehouse(request: Request) -> Response:
    """POST /api/organizations/{org_id}/provision-warehouse -- manual provision (AC4).

    Auth: owner or admin of the org.  Idempotent: safe to call multiple times.
    Emits audit ACTION_ORG_SCHEMAS_PROVISIONED on success.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (org_id,))
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                denied = _enforce_org_manage(org_id, identity, conn, "provision_org_warehouse")
                if denied is not None:
                    return denied
    except Exception:
        logger.exception("admin_api: provision_org_warehouse db_error org=%s", org_id)
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )

    from core import warehouse_tenancy as _wt  # noqa: PLC0415

    try:
        result = _wt.provision_org_schemas(org_id=org_id, conn=None)
    except Exception:
        logger.exception("admin_api: provision_org_warehouse failed org=%s", org_id)
        return JSONResponse(
            {"code": "provision_failed", "message": "warehouse operation failed"},
            status_code=500,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ORG_SCHEMAS_PROVISIONED,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, **result},
    )
    return JSONResponse({"org_id": org_id, **result}, status_code=200)

async def _org_deletion_preview(request: Request) -> Response:
    """GET /api/organizations/{org_id}/deletion-preview -- what would disappear.

    Same owner/admin gate as the deletion itself: the composition of an org is
    not public information. Read-only, no side effect, safe to poll from an
    onboarding/settings screen before showing the confirmation.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Existence 404 FIRST (same discipline as _patch_org: do not
                # disclose the manage-state of an org that does not exist).
                cur.execute(
                    "SELECT id, name, slug FROM app.organizations WHERE id = %s",
                    (org_id,),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse(
                    {"code": "not_found", "message": "organization not found"},
                    status_code=404,
                )
            denied = _enforce_org_manage(org_id, identity, conn, "org_deletion_preview")
            if denied is not None:
                return denied
            facts = _org_deletion_facts(conn, org_id, name=row[1], slug=row[2])
            # Read-only path: release the snapshot without writing anything.
            conn.rollback()
    except Exception:
        logger.exception("admin_api: org_deletion_preview db_error org=%s", org_id)
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )
    return JSONResponse(facts, status_code=200)

def _extract_brand_fields(body: dict) -> tuple[dict | None, Response | None]:
    """Story 21.2: validate + extract org branding fields present in *body*.

    Returns (fields, None) on success or (None, 422) on an invalid hex colour.
    Only keys present in the body are returned (absent = unchanged on PATCH).
    The 3 colours must match #RRGGBB; logo_url is a free string (nullable).
    """
    fields: dict = {}
    for col in _ORG_BRAND_COLOUR_FIELDS:
        if col in body:
            raw = body.get(col)
            if raw in (None, ""):
                fields[col] = None
            else:
                val = str(raw).strip()
                if not _HEX_COLOUR_RE.match(val):
                    return None, JSONResponse(
                        {
                            "code": "invalid_input",
                            "message": (
                                f"Invalid colour for {col}: '{val}'. "
                                f"Expected format: #RRGGBB (hexadecimal)."
                            ),
                        },
                        status_code=422,
                    )
                fields[col] = val
    if "logo_url" in body:
        raw_logo = body.get("logo_url")
        fields["logo_url"] = str(raw_logo).strip() if raw_logo else None
    return fields, None

def _mint_org_id() -> str:
    """Mint a new prefixed ULID 'org_<ULID>' for an organization."""
    from ulid import ULID  # noqa: PLC0415

    return f"org_{ULID()}"

def _org_row_to_dict(cols: list[str], row: tuple) -> dict:
    """Serialise an organizations/org_members row, ISO-formatting timestamps."""
    _ts_cols = {"created_at", "updated_at", "archived_at", "invited_at", "joined_at"}
    out: dict = {}
    for col, val in zip(cols, row):
        out[col] = val.isoformat() if (col in _ts_cols and val is not None) else val
    return out

def _mint_org_member_id() -> str:
    """Mint a new prefixed ULID 'omem_<ULID>' for a membership row."""
    from ulid import ULID  # noqa: PLC0415

    return f"omem_{ULID()}"


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

ORGANIZATIONS_ROUTES_1 = [
    Route("/api/organizations", endpoint=_list_orgs, methods=["GET"]),
    Route("/api/organizations", endpoint=_create_org, methods=["POST"]),
]

ORGANIZATIONS_ROUTES_2 = [
    Route("/api/organizations/{org_id}", endpoint=_get_org, methods=["GET"]),
    Route("/api/organizations/{org_id}", endpoint=_patch_org, methods=["PATCH"]),
    # Story 24.2: org data-plane lifecycle (human-gated delete + per-org provision
    # + platform backfill).  provision-warehouse before {org_id} DELETE so
    # Starlette resolves the sub-resource before the bare id route.
    Route(
        "/api/organizations/{org_id}/provision-warehouse",
        endpoint=_provision_org_warehouse,
        methods=["POST"],
    ),
    # Same ordering rule: the sub-resource is declared before the bare id.
    Route(
        "/api/organizations/{org_id}/deletion-preview",
        endpoint=_org_deletion_preview,
        methods=["GET"],
    ),
    Route("/api/organizations/{org_id}", endpoint=_delete_org, methods=["DELETE"]),
]
