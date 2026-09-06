"""The Project object: list it, create it, read it, change it, archive it.

AD-43, 2026-08-12. `/api/projects/…` was carried in AD-40's order as one family.
Reading it before touching it said otherwise: **21 routes, four subjects sharing
a URL prefix.** Splitting on the prefix would have put the first-report surface
in the same file as the Project CRUD because both addresses start with the same
word -- which is the mistake AD-42 named on `ContentRouter`, in the other
direction. A URL prefix is not a responsibility.

This module is the object itself, and the two handlers that carry the most
business logic in the repository live here: `_create_project` (345 lines, six SQL
statements) and `_patch_project` (284, four). They moved VERBATIM. Repairing them
-- so that a handler parses, authorizes and calls a service -- is the third step
of AD-43 and edits bodies, which is why it does not travel with an extraction.
"""

from __future__ import annotations

import json
import logging
import os
import re
from functools import lru_cache

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core import nango_client
from core.audit import (
    ACTION_CROSS_SCOPE_ATTEMPT,
    declare_action,
    write_audit_row,
)
from core.country_vocabulary import CountryVocabularyError, get_country_vocabulary
from core.geographic_reporting import (
    GeographicPosture,
    fetch_project_geographic_posture,
)
from core.row_json import row_to_json
from core.verification_prefs import (
    _fetch_verification_prefs,
    _upsert_verification_prefs,
)

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._check_auth` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _country_vocabulary_error(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._country_vocabulary_error` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _country_vocabulary_error as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _project_not_found_response(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._project_not_found_response` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _project_not_found_response as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _refuse_unless_project_allowed(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._refuse_unless_project_allowed` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _refuse_unless_project_allowed as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _slugify(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._slugify` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _slugify as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _strict_project_capability_allowed(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent
    `core.admin_api._strict_project_capability_allowed` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _strict_project_capability_allowed as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

ACTION_KEY_CREATED = declare_action("key_created")

ACTION_KEY_DELETED = declare_action("key_deleted")

ACTION_KEY_ROTATED = declare_action("key_rotated")

ACTION_PROJECT_ARCHIVED = declare_action("project_archived")

ACTION_PROJECT_RESTORED = declare_action("project_restored")

ACTION_PROJECT_CREATED = declare_action("project_created")


# slug pattern (AC3): kebab-case, starts alphanumeric.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Story 17.1: valeurs autorisées pour verification_source_type.
# 'stripe' est inclus comme préférence déclarative ; son connecteur est
# prévu post-story 15.7 mais la préférence est stockable dès maintenant.
_VERIFICATION_SOURCE_TYPES = frozenset({"ga4", "shopify", "stripe"})

async def _list_projects(request: Request) -> Response:
    """GET /api/projects -- list active projects, ordered by name ASC (AC3)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT p.id, p.name, p.slug, p.status, p.description,
                           p.org_id, p.created_at, p.updated_at
                    FROM app.projects p
                    JOIN app.organizations o ON o.id = p.org_id
                    JOIN app.org_members m
                      ON m.org_id = p.org_id AND m.identity = %s
                     AND m.status = 'active'
                    WHERE p.status = 'active' AND o.status = 'active'
                      AND (
                          m.role = 'owner'
                          OR EXISTS (
                              SELECT 1 FROM app.resource_grants g
                              WHERE g.org_id = p.org_id AND g.identity = %s
                                AND g.scope_type = 'project'
                                AND g.scope_id = p.id
                                AND g.capability IN ('view', 'edit', 'manage')
                          )
                      )
                    ORDER BY p.name ASC
                    """,
                    (identity, identity),
                )
                cols = [d[0] for d in cur.description]
                projects = [_project_row_to_dict(cols, r) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("admin_api: list_projects db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"projects": projects}, status_code=200)

async def _create_project(request: Request) -> Response:
    """POST /api/projects -- create a project (AC3).

    Body: {"name": str, "slug": str?, "currency": str?, "timezone": str?,
           "timezone_suggestion": str?,
           "verification_source_type": str?, "verification_source_id": str?,
           "lead_event_name": str?}
    Returns 201 with the created project. Auto-generates a unique slug from name
    when not provided (appends -1, -2, ... on collision).

    Story 17.1: accepte les 3 champs source de vérification (optionnels).
    Les champs sont écrits dans app.project_preferences (AD-8 : sole-writer Postgres).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

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

    # AI-77/AI-81: the value and its provenance are decided in one place. Nothing
    # here suggests EUR, so an absent currency travels as `default`, never as a
    # `suggestion` the row never earned.
    from core.project_provenance import decide_currency, decide_timezone  # noqa: PLC0415

    currency_decision = decide_currency(body.get("currency"))
    currency = currency_decision.value
    if currency not in _project_currencies():
        return JSONResponse(
            {"code": "invalid_input", "message": f"unsupported currency: {currency}"},
            status_code=422,
        )

    # The browser zone is a SUGGESTION, and the two ENTRY doors
    # (`hosted_entry_scope`, `self_hosted_instance_claim`) already say so. This
    # one did not read the key at all, so a screen that sent an honest
    # suggestion had it dropped and the project landed on the platform fallback
    # -- while a screen that sent it as `timezone` had a browser guess recorded
    # as an operator decision. Same key, same evidence, all three doors.
    timezone_suggestion = body.get("timezone_suggestion")
    timezone_decision = decide_timezone(
        body.get("timezone"),
        suggestion=(timezone_suggestion or None),
        suggestion_evidence=(
            "browser IANA zone reported at sign-up" if timezone_suggestion else None
        ),
    )
    timezone_str = timezone_decision.value
    if not _valid_timezone(timezone_str):
        return JSONResponse(
            {"code": "invalid_input", "message": f"invalid timezone: {timezone_str}"},
            status_code=422,
        )

    slug_in = (body.get("slug") or "").strip()
    base_slug = slug_in or _slugify(name)
    if not base_slug or not _SLUG_RE.match(base_slug) or len(base_slug) > 50:
        return JSONResponse(
            {"code": "invalid_input", "message": f"invalid slug: {base_slug!r}"},
            status_code=422,
        )

    # Story 17.1: valider les champs source de vérification avant d'écrire en DB.
    vsfields, vs_err = _validate_verification_source_fields(body)
    if vs_err is not None:
        return vs_err
    # Validation de cohérence ga4 + lead_event_name pour la création
    # (les deux champs peuvent être fournis ensemble ou séparément).
    effective_vstype = vsfields.get("verification_source_type") if vsfields else None
    effective_len = vsfields.get("lead_event_name") if vsfields else None
    if effective_vstype == "ga4" and "lead_event_name" not in body:
        # Non fourni dans ce body : OK pour CREATE — NULL est persisté tel quel
        # (AD-9 : pas de valeur fantôme injectée en DB). C'est le CONSOMMATEUR (17.2)
        # qui appliquera le défaut 'generate_lead' à la lecture quand type='ga4' et
        # lead_event_name est NULL (review-17-1 F-5 : le défaut est applicatif côté
        # lecture, jamais écrit). L'AC exige seulement : fourni et vide → 422.
        pass
    coherence_err = _validate_verification_source_complete(effective_vstype, effective_len)
    if coherence_err is not None and "lead_event_name" in body:
        # Seulement bloquer si le champ a été explicitement fourni et est vide.
        return coherence_err

    # The retired posture is refused at creation too: a project born with markets
    # written here would look decided and be governed by nothing.
    if any(key in body for key in GEOGRAPHIC_POSTURE_KEYS):
        return _geographic_posture_retired_response()

    # The default posture, which is what the response echoes. It is not persisted:
    # `global` with no market IS the absence of a published country projection, and
    # writing the absence into a retired column would re-open the door just closed.
    geographic_posture = GeographicPosture()

    proj_id = _mint_project_id()
    created_by = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Resolve slug collisions by appending a counter (Dev Notes). When a
            # slug was explicitly supplied, a collision is a 409 (AC8).
            slug = base_slug
            with conn.cursor() as cur:
                if slug_in:
                    cur.execute("SELECT 1 FROM app.projects WHERE slug = %s", (slug,))
                    if cur.fetchone() is not None:
                        return JSONResponse(
                            {"code": "conflict", "message": "slug already exists"},
                            status_code=409,
                        )
                else:
                    counter = 1
                    while True:
                        cur.execute("SELECT 1 FROM app.projects WHERE slug = %s", (slug,))
                        if cur.fetchone() is None:
                            break
                        slug = f"{base_slug}-{counter}"
                        counter += 1

                # Rattacher le projet a son organisation. Sans org_id, la
                # resolution d'entrepot (warehouse_tenancy) ne trouve aucune org
                # et retombe sur le nommage legacy : les datasets provisionnes a
                # la creation de l'org ne servent JAMAIS, et le cloisonnement par
                # org (P4) ne s'applique pas. La colonne existait, l'INSERT ne
                # l'ecrivait simplement pas.
                org_id_in = (body.get("org_id") or "").strip()
                if org_id_in:
                    cur.execute(
                        "SELECT 1 FROM app.org_members "
                        "WHERE org_id = %s AND identity = %s AND status = 'active'",
                        (org_id_in, created_by),
                    )
                    if cur.fetchone() is None:
                        conn.rollback()
                        return JSONResponse(
                            {
                                "code": "forbidden",
                                "message": ("You are not an active member of this organization."),
                            },
                            status_code=403,
                        )
                    org_id_for_project = org_id_in
                else:
                    # Non fourni : deduire quand c'est SANS AMBIGUITE (une seule
                    # org active pour l'appelant). Deux orgs ou plus : exiger le
                    # choix plutot que d'en deviner une.
                    cur.execute(
                        "SELECT org_id FROM app.org_members "
                        "WHERE identity = %s AND status = 'active' LIMIT 2",
                        (created_by,),
                    )
                    memberships = [r[0] for r in cur.fetchall()]
                    if len(memberships) > 1:
                        conn.rollback()
                        return JSONResponse(
                            {
                                "code": "org_id_required",
                                "message": (
                                    "You belong to several organizations: specify "
                                    "org_id for this project."
                                ),
                            },
                            status_code=422,
                        )
                    if not memberships:
                        # Aucune org : REFUSER plutot que de creer un orphelin.
                        # Un projet sans org n'a pas d'entrepot ou atterrir et
                        # echappe au cloisonnement : ce n'est pas un projet
                        # degrade, c'est un projet impossible. L'onboarding dit
                        # deja que l'organisation est la premiere etape et que
                        # tout le reste en decoule -- l'API doit tenir le meme
                        # discours au lieu de fabriquer l'objet incoherent.
                        conn.rollback()
                        return JSONResponse(
                            {
                                "code": "org_required",
                                "message": (
                                    "Create or join an organization before creating a project."
                                ),
                            },
                            status_code=422,
                        )
                    org_id_for_project = memberships[0]

                cur.execute(
                    """
                    INSERT INTO app.projects
                        (id, name, slug, status, created_by, org_id)
                    VALUES (%s, %s, %s, 'active', %s, %s)
                    RETURNING id, name, slug, status, description, created_at
                    """,
                    (
                        proj_id,
                        name,
                        slug,
                        created_by,
                        org_id_for_project,
                    ),
                )
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
                created = _project_row_to_dict(cols, row)
                from core.project_provenance import (  # noqa: PLC0415
                    insert_project_preferences,
                )

                insert_project_preferences(
                    cur, proj_id, currency_decision, timezone_decision
                )

                # A Project is born with its configuration version 1. Without it
                # the Add Datastream funnel dies at Preview on a prerequisite no
                # screen asks for -- see `ensure_active_configuration_version`.
                from core.project_settings import (  # noqa: PLC0415
                    ensure_active_configuration_version,
                )

                ensure_active_configuration_version(conn, proj_id, created_by or "system")

                from core.getting_started import bootstrap_project_journey

                bootstrap_project_journey(
                    conn,
                    org_id=org_id_for_project,
                    project_id=proj_id,
                    actor_identity=created_by,
                )

            # Story 17.1: écrire les préférences source de vérification dans
            # app.project_preferences (AD-8 sole-writer Postgres).
            if vsfields:
                _upsert_verification_prefs(proj_id, vsfields, conn)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: create_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=created_by,
        action=ACTION_PROJECT_CREATED,
        provider_account="",
        connection_ref="",
        metadata={"project_id": proj_id, "slug": created["slug"], "name": name},
    )

    # Story 7.3 (AC6): provision per-tenant key immediately after project creation.
    # If key provisioning fails, roll back the project insert and return 500.
    try:
        from core.tenant_keys import get_tenant_key_backend, write_key_audit_row  # noqa: PLC0415

        backend = get_tenant_key_backend()
        backend.get_or_create_key(proj_id)
        write_key_audit_row(
            project_id=proj_id,
            action="key_created",
            performed_by=created_by,
            details={"backend": os.environ.get("TENANT_KEY_BACKEND", "local")},
        )
        write_audit_row(
            identity=created_by,
            action=ACTION_KEY_CREATED,
            provider_account="",
            connection_ref="",
            metadata={"project_id": proj_id},
        )
    except Exception as exc:
        logger.error("admin_api: key_provision_failed project=%s err=%s", proj_id, exc)
        # Roll back: delete the project row just inserted.
        try:
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM app.projects WHERE id = %s", (proj_id,))
                conn.commit()
        except Exception as rb_exc:
            logger.error("admin_api: rollback_failed project=%s err=%s", proj_id, rb_exc)
        return JSONResponse(
            {
                "code": "key_provision_failed",
                "message": f"Failed to provision tenant key: {exc}",
            },
            status_code=500,
        )

    # Story 44.2: amorcer la couche de connaissance jour-0 APRES le commit du
    # projet (helper dedie, unit-testable sans Postgres -- re-review vague 2).
    _seed_new_project(proj_id)

    # Story 17.1: enrichir la réponse avec les champs source de vérification.
    created.update(
        {
            "verification_source_type": vsfields.get("verification_source_type")
            if vsfields
            else None,
            "verification_source_id": vsfields.get("verification_source_id") if vsfields else None,
            "lead_event_name": vsfields.get("lead_event_name") if vsfields else None,
        }
    )
    created.update(geographic_posture.as_dict())
    return JSONResponse(created, status_code=201)

async def _get_project(request: Request) -> Response:
    """GET /api/projects/{project_id} -- single project; 404 if not found (AC3)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    project_id = request.path_params["project_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            actor = identity or "anonymous"
            if not _strict_project_capability_allowed(
                conn,
                identity=actor,
                project_id=project_id,
                minimum_capability="view",
                # AI-363: an archived Project answers 200 with status "archived"
                # to identities that may see it -- never a 404 that reads as
                # "someone deleted it" (project-settings.md, 2026-09-02).
                include_archived_project=True,
            ):
                return _project_not_found_response()
            can_edit = _strict_project_capability_allowed(
                conn,
                identity=actor,
                project_id=project_id,
                minimum_capability="edit",
            )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, slug, status, description, org_id,
                           active_configuration_version_id,
                           created_at, updated_at, archived_at
                    FROM app.projects WHERE id = %s
                    """,
                    (project_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Project not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                project = _project_row_to_dict(cols, row)
                project["can_edit"] = can_edit
            # Story 17.1: enrichir la réponse avec les préférences source de vérification.
            vsprefs = _fetch_verification_prefs(project_id, conn)
            project.update(vsprefs)
            geographic_posture = _fetch_geographic_prefs(project_id, conn)
            project.update(geographic_posture.as_dict())
    except CountryVocabularyError:
        return _country_vocabulary_error()
    except Exception as exc:
        logger.error("admin_api: get_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(project, status_code=200)

async def _patch_project(request: Request) -> Response:
    """PATCH /api/projects/{project_id} -- update mutable fields (AC3).

    Body: {"name": str?, "description": str?,
           "verification_source_type": str?, "verification_source_id": str?,
           "lead_event_name": str?}
    id and slug are immutable. Updates updated_at.

    Story 17.1: accepte les 3 champs source de vérification.
    Écrits dans app.project_preferences (AD-8). Un type='ga4' sans lead_event_name
    dans le state final (existant + patch) → 422 (message français).
    Dénégation cross-projet pour verification_source_id : l'id doit appartenir au
    projet patché (vérifié via app.datastreams, AD-5).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    project_id = request.path_params["project_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON body must be an object"},
            status_code=422,
        )

    set_clauses: list[str] = []
    params: list = []
    if "name" in body:
        if not isinstance(body.get("name"), str):
            return JSONResponse(
                {"code": "invalid_input", "message": "name must be a string"},
                status_code=422,
            )
        name = body["name"].strip()
        if not name or len(name) > 100:
            return JSONResponse(
                {"code": "invalid_input", "message": "name must be 1..100 chars"},
                status_code=422,
            )
        set_clauses.append("name = %s")
        params.append(name)
    if "description" in body:
        description = body.get("description")
        if description is not None and not isinstance(description, str):
            return JSONResponse(
                {"code": "invalid_input", "message": "description must be a string or null"},
                status_code=422,
            )
        normalized_description = description.strip() if isinstance(description, str) else None
        if normalized_description is not None and len(normalized_description) > 1000:
            return JSONResponse(
                {"code": "invalid_input", "message": "description must be at most 1000 chars"},
                status_code=422,
            )
        set_clauses.append("description = %s")
        params.append(normalized_description or None)

    # Story 17.1: valider les champs source de vérification.
    vsfields, vs_err = _validate_verification_source_fields(body)
    if vs_err is not None:
        return vs_err

    has_project_fields = bool(set_clauses)
    has_vs_fields = bool(vsfields)
    if any(key in body for key in GEOGRAPHIC_POSTURE_KEYS):
        return _geographic_posture_retired_response()

    if not has_project_fields and not has_vs_fields:
        return JSONResponse(
            {"code": "invalid_input", "message": "no updatable fields provided"},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            actor = identity or "anonymous"
            if not _strict_project_capability_allowed(
                conn,
                identity=actor,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _project_not_found_response()
            # Read-only: the response still echoes what the retired columns hold for
            # an existing project, and no branch below writes them. The impact
            # preview that used to gate a change here went with the door -- there is
            # no change to preview when the posture cannot be changed here at all.
            geographic_posture = _fetch_geographic_prefs(project_id, conn)
            # Story 17.1 AD-5: vérifier que verification_source_id appartient bien
            # à ce projet (cross-project scope denial).
            if vsfields and vsfields.get("verification_source_id"):
                vs_id = vsfields["verification_source_id"]
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT project_id FROM app.datastreams WHERE id = %s",
                        (vs_id,),
                    )
                    ds_row = cur.fetchone()
                # On accepte aussi qu'un id ne soit pas dans app.datastreams
                # (peut être un profil GA4 ou une connexion non encore migrée).
                # Mais si le datastream existe et appartient à un AUTRE projet : refus.
                if ds_row is not None and ds_row[0] != project_id:
                    # review-17-1 F-3 (AD-5/FR12): every cross-scope refusal is AUDITED,
                    # like the notebook/datastream refusals in this file.
                    write_audit_row(
                        identity=identity or "anonymous",
                        action=ACTION_CROSS_SCOPE_ATTEMPT,
                        provider_account="",
                        connection_ref="",
                        metadata={
                            "claimed_project_id": project_id,
                            "datastream_id": vs_id,
                            "datastream_project_id": ds_row[0],
                            "operation": "patch_project_verification_source",
                        },
                    )
                    logger.warning(
                        "admin_api: cross_project_vsid project=%s claimed_ds=%s ds_project=%s",
                        project_id,
                        vs_id,
                        ds_row[0],
                    )
                    return JSONResponse(
                        {
                            "code": "forbidden",
                            "message": (
                                "The Datastream designated as verification source "
                                "does not belong to this project."
                            ),
                        },
                        status_code=403,
                    )

            # Si verification_source_type='ga4' dans le PATCH mais pas lead_event_name,
            # lire l'état courant pour vérifier la cohérence finale.
            if (
                vsfields
                and vsfields.get("verification_source_type") == "ga4"
                and "lead_event_name" not in body
            ):
                current_prefs = _fetch_verification_prefs(project_id, conn)
                coherence_err = _validate_verification_source_complete(
                    "ga4", current_prefs.get("lead_event_name")
                )
                if coherence_err is not None:
                    return coherence_err

            updated: dict = {}
            if has_project_fields:
                set_clauses.append("updated_at = NOW()")
                params.append(project_id)
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE app.projects SET " + ", ".join(set_clauses) + " WHERE id = %s "
                        "RETURNING id, name, slug, status, description, org_id, "
                        "active_configuration_version_id, created_at, updated_at",
                        params,
                    )
                    row = cur.fetchone()
                    if row is None:
                        return JSONResponse(
                            {"code": "not_found", "message": "Project not found"},
                            status_code=404,
                        )
                    cols = [d[0] for d in cur.description]
                    updated = _project_row_to_dict(cols, row)
            else:
                # Seuls des champs vs ont été fournis : vérifier que le projet existe.
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, name, slug, status, description, org_id, "
                        "active_configuration_version_id, created_at, updated_at "
                        "FROM app.projects WHERE id = %s",
                        (project_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return JSONResponse(
                            {"code": "not_found", "message": "Project not found"},
                            status_code=404,
                        )
                    cols = [d[0] for d in cur.description]
                    updated = _project_row_to_dict(cols, row)

            # Story 17.1: persister les préférences source de vérification.
            if has_vs_fields:
                # review-17-1 F-6: si le type est explicitement remis à NULL, nettoyer
                # aussi l'id et le lead_event_name (pas de préférences orphelines en DB).
                if (
                    "verification_source_type" in vsfields
                    and vsfields["verification_source_type"] is None
                ):
                    vsfields.setdefault("verification_source_id", None)
                    vsfields.setdefault("lead_event_name", None)
                _upsert_verification_prefs(project_id, vsfields, conn)
            # Lire l'état final des préférences pour la réponse.
            final_prefs = _fetch_verification_prefs(project_id, conn)

            # review-17-1 F-2: la cohérence se valide sur l'ÉTAT FINAL (existant + patch),
            # pas seulement sur le body — un PATCH {"lead_event_name": ""} seul sur un
            # projet déjà en type='ga4' produirait sinon un état ga4 + NULL incohérent.
            coherence_err = _validate_verification_source_complete(
                final_prefs.get("verification_source_type"),
                final_prefs.get("lead_event_name"),
            )
            if coherence_err is not None:
                conn.rollback()
                return coherence_err

            updated.update(final_prefs)
            updated.update(geographic_posture.as_dict())
            updated["can_edit"] = True

            conn.commit()
    except CountryVocabularyError:
        return _country_vocabulary_error()
    except Exception as exc:
        logger.error("admin_api: patch_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(updated, status_code=200)

async def _delete_project(request: Request) -> Response:
    """DELETE /api/projects/{project_id} -- archive + revoke connections (AC4).

    NEVER hard-deletes. Sets status='archived', archived_at=NOW(). Marks every
    non-revoked connection_ref for the project as revoked, writes an audit row,
    and returns {"status": "archived", "connections_revoked": N}.
    404 if not found, if already-not-visible to this identity; 409 if archived.

    AI-171 -- C'ETAIT LA PLUS LOURDE DES SIX. Tout jeton valide, de n'importe
    quelle organisation, archivait le projet nomme dans l'URL et revoquait AU
    PASSAGE toutes ses connexions -- un tenant coupait la collecte d'un autre par
    un seul appel. Aucune garde : ni role, ni portee, ni RLS (`get_connection()`
    n'installe pas de contexte d'identite). `manage` est exige, comme pour les
    autres actes destructifs de ce fichier.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    project_id = request.path_params["project_id"]
    subject = identity or "anonymous"

    denied = _refuse_unless_project_allowed(
        identity, project_id, "manage", "delete_project", include_archived_project=True
    )
    if denied is not None:
        return denied

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # 1. Verify exists + active (single transaction; row-lock).
                cur.execute(
                    "SELECT status FROM app.projects WHERE id = %s FOR UPDATE",
                    (project_id,),
                )
                prow = cur.fetchone()
                if prow is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Project not found"},
                        status_code=404,
                    )
                if prow[0] == "archived":
                    return JSONResponse(
                        {
                            "code": "conflict",
                            "message": (
                                "Project already archived. Restore it first: "
                                "POST /api/projects/{project_id}/restore."
                            ),
                        },
                        status_code=409,
                    )

                # 2. Archive the project (soft-delete; data remains readable).
                cur.execute(
                    "UPDATE app.projects "
                    "SET status = 'archived', archived_at = NOW(), updated_at = NOW() "
                    "WHERE id = %s",
                    (project_id,),
                )

                # 3. Revoke every still-active connection for the project.
                cur.execute(
                    "UPDATE app.connection_ref "
                    "SET status = 'revoked', revoked_at = NOW(), updated_at = NOW() "
                    "WHERE project_id = %s AND status != 'revoked' "
                    "RETURNING id, provider, nango_connection_id",
                    (project_id,),
                )
                revoked = cur.fetchall()
            from core.account_topology import invalidate_credential_exposures  # noqa: PLC0415

            for revoked_id, _provider, _nango_id in revoked:
                invalidate_credential_exposures(revoked_id, "revocation", conn)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: delete_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    connections_revoked = len(revoked)

    # 3b. Best-effort Nango token revocation per connection. Failure logs a
    # structured warning but never blocks the archive (AC4 step 3).
    # AI-41: on revocation failure, emit a type='nango_revoke_failed' infra firing
    # so the event is surfaced by the normal alert pipeline.  Never raises; archival
    # outcome is unchanged (best-effort semantics preserved).
    for conn_id, provider, nango_conn_id in revoked:
        try:
            nango_client.revoke_connection(provider, nango_conn_id)
        except AttributeError:
            # No revoke helper in nango_client at P3-dev: DB revoke is the record
            # of truth; token cleanup is a Phase B concern. Log once per conn.
            logger.warning(
                "delete_project: nango_revoke_unavailable conn=%s provider=%s",
                conn_id,
                provider,
            )
        except Exception as exc:
            logger.warning(
                "delete_project: nango_revoke_failed conn=%s provider=%s err=%s",
                conn_id,
                provider,
                exc,
            )
            # AI-41: emit infra firing so the failure is observable via alert delivery.
            try:
                from core import infra_alerts as _ia  # noqa: PLC0415

                _ia.write_infra_firing(
                    alert_type="nango_revoke_failed",
                    project_id=project_id,
                    metric="nango_revoke",
                    severity="error",
                    message=(
                        f"Nango token revocation failed during project archival: "
                        f"conn={conn_id} provider={provider}"
                    ),
                    metadata={
                        "project_id": project_id,
                        "connection_ref_id": conn_id,
                        "nango_connection_id": nango_conn_id,
                        "provider": provider,
                        "error": str(exc),
                    },
                )
            except Exception as fire_exc:  # noqa: BLE001
                logger.debug(
                    "delete_project: nango_revoke_failed_firing_error conn=%s: %s",
                    conn_id,
                    fire_exc,
                )

    # 3c. Story 7.3 (AC4, T5): delete tenant key after all connections are revoked.
    # Failure logs a warning but DOES NOT abort the archive (graceful degradation).
    try:
        from core.tenant_keys import get_tenant_key_backend, write_key_audit_row  # noqa: PLC0415

        backend = get_tenant_key_backend()
        backend.delete_key(project_id)
        write_key_audit_row(
            project_id=project_id,
            action="key_deleted",
            performed_by=subject,
            details={
                "backend": os.environ.get("TENANT_KEY_BACKEND", "local"),
                "via": "project_archive",
            },
        )
        write_audit_row(
            identity=subject,
            action=ACTION_KEY_DELETED,
            provider_account="",
            connection_ref="",
            metadata={"project_id": project_id, "via": "project_archive"},
        )
    except Exception as exc:
        logger.warning(
            "delete_project: key_deletion_failed project=%s err=%s",
            project_id,
            exc,
        )

    # 4. Audit row (AC4 step 4). Attach a revoked connection_ref when present so
    # the row satisfies the audit_log FK; falls back to "" like other project-
    # scoped audit events (context_events / notebooks) when no connection exists.
    audit_conn_ref = revoked[0][0] if revoked else ""
    write_audit_row(
        identity=subject,
        action=ACTION_PROJECT_ARCHIVED,
        provider_account="",
        connection_ref=audit_conn_ref,
        metadata={"project_id": project_id, "connections_revoked": connections_revoked},
    )

    return JSONResponse(
        {"status": "archived", "connections_revoked": connections_revoked},
        status_code=200,
    )



async def _restore_project(request: Request) -> Response:
    """POST /api/projects/{project_id}/restore -- archived back to active (AI-363).

    The mirror of ``_delete_project``, ratified in project-settings.md
    (amendment 2026-09-02): an archived Project is an address its org managers
    still hold, and this is the door the 409 of a second DELETE names. It sets
    ``status='active'`` and clears ``archived_at`` -- and it does NOT resurrect
    the connections the archive revoked: a revoked consent is a fact about the
    provider, so the Sources screen shows them revoked with reconnection as the
    gesture. The warehouse zones were never dropped (execution-substrate.md).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    project_id = request.path_params["project_id"]
    subject = identity or "anonymous"

    denied = _refuse_unless_project_allowed(
        identity, project_id, "manage", "restore_project", include_archived_project=True
    )
    if denied is not None:
        return denied

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT status FROM app.projects WHERE id = %s FOR UPDATE",
                    (project_id,),
                )
                prow = cur.fetchone()
                if prow is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Project not found"},
                        status_code=404,
                    )
                if prow[0] != "archived":
                    return JSONResponse(
                        {"code": "conflict", "message": "Project is not archived"},
                        status_code=409,
                    )
                cur.execute(
                    "UPDATE app.projects "
                    "SET status = 'active', archived_at = NULL, updated_at = NOW() "
                    "WHERE id = %s",
                    (project_id,),
                )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: restore_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=subject,
        action=ACTION_PROJECT_RESTORED,
        provider_account="",
        connection_ref="",
        metadata={"project_id": project_id},
    )
    return JSONResponse({"status": "active"}, status_code=200)

async def _rotate_project_key(request: Request) -> Response:
    """POST /api/projects/{project_id}/rotate-key -- rotate per-tenant encryption key.

    Story 7.3, AC5. Rotation does NOT require re-encrypting stored data (Phase A
    does not encrypt connection_ref payloads -- the OAuth tokens live in Nango under
    Nango's global key). The new key replaces the old one for future operations.

    AI-171 -- LA ROTATION D'UNE CLE DE TENANT N'APPARTENAIT A PERSONNE. Le
    handler verifiait que le projet EXISTE, jamais que l'appelant y a droit :
    n'importe quel jeton valide faisait tourner la cle de chiffrement d'un autre
    tenant, et signait la ligne d'audit de son propre nom. `manage`, comme
    l'archivage.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.path_params.get("project_id", "")
    subject = identity or "anonymous"

    if not project_id:
        return JSONResponse(
            {"code": "missing_id", "message": "project_id is required"},
            status_code=400,
        )

    denied = _refuse_unless_project_allowed(identity, project_id, "manage", "rotate_project_key")
    if denied is not None:
        return denied

    # Verify project exists
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM app.projects WHERE id = %s AND status = 'active'",
                    (project_id,),
                )
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Project not found or archived"},
                        status_code=404,
                    )
    except Exception as exc:
        logger.error("admin_api: rotate_key db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    try:
        from core.tenant_keys import get_tenant_key_backend, write_key_audit_row  # noqa: PLC0415

        backend = get_tenant_key_backend()
        backend.rotate_key(project_id)
        rotated_at = datetime.now(tz=timezone.utc).isoformat()
        write_key_audit_row(
            project_id=project_id,
            action="key_rotated",
            performed_by=subject,
            details={"backend": os.environ.get("TENANT_KEY_BACKEND", "local")},
        )
        write_audit_row(
            identity=subject,
            action=ACTION_KEY_ROTATED,
            provider_account="",
            connection_ref="",
            metadata={"project_id": project_id},
        )
    except Exception as exc:
        logger.error("admin_api: rotate_key failed project=%s err=%s", project_id, exc)
        return JSONResponse(
            {"code": "rotate_key_failed", "message": f"Key rotation failed: {exc}"},
            status_code=500,
        )

    return JSONResponse(
        {"status": "rotated", "rotated_at": rotated_at},
        status_code=200,
    )

def _country_codes() -> frozenset[str]:
    return frozenset(country.code for country in get_country_vocabulary())

def _fetch_geographic_prefs(
    project_id: str,
    conn: object,
    *,
    for_update: bool = False,
):
    return fetch_project_geographic_posture(project_id, conn, for_update=for_update)

#: The three body keys of the RETIRED geographic posture. `country_registry.py:12`
#: declares the columns they wrote -- `app.project_preferences.geographic_mode`,
#: `.local_markets`, `.local_market_country_codes` -- "replaced outright", and the
#: governed readers prove it: `country_activation.governed_posture` reads the
#: project capability plus the PUBLISHED country projection and never looks here.
#: So a write through this door changed no governed answer while emitting a real
#: audit row and a real mirror update -- a door that reports success and moves
#: nothing. Story 48.2 closed the console half (`CountryHasOneEditor.test.ts`);
#: this closes the API half.
GEOGRAPHIC_POSTURE_KEYS = ("geographic_mode", "local_market_country_codes", "local_markets")


def _geographic_posture_retired_response() -> JSONResponse:
    """Refuse a retired-posture write, and name the gesture that works.

    Not a silent ignore: a caller who sent markets and got 200 would believe the
    project now reports by market. Not a 404 either -- the project exists and the
    caller may well have the right to change its geography, just not here.
    """
    return JSONResponse(
        {
            "code": "geographic_posture_retired",
            "message": (
                "Countries and markets are governed master data and are no longer "
                "set on the project. Open the project's Country registry "
                "(Governance > Registry > Hierarchy) and publish the change there, "
                "or use the edit_country_master_data, "
                "prepare_country_master_data_publish and publish_country_master_data "
                "tools. A change published there is what every governed reader reads."
            ),
            "details": {"retired_fields": list(GEOGRAPHIC_POSTURE_KEYS)},
        },
        status_code=422,
    )


def _mint_project_id() -> str:
    """Mint a new prefixed ULID 'proj_<ULID>' for a project."""
    from ulid import ULID  # noqa: PLC0415

    return f"proj_{ULID()}"

@lru_cache(maxsize=1)
def _project_currencies() -> frozenset[str]:
    # The decorator was DROPPED on the way out of `admin_api` in the AD-43
    # extraction of 2026-08-12, whose own docstring says these bodies "moved
    # VERBATIM". Nothing saw it for three weeks: `load_currency_vocabulary`
    # opens and parses `dim_currency.csv` on every call, so every project
    # creation and every patch that validates a currency re-read the seed from
    # disk. Restored 2026-09-02 by `python scripts/division_proof.py --moved
    # --since 6a40c1c1~1 --until 6a40c1c1`, the first thing that instrument
    # found -- which is what criterion 5 means by "the diff cannot be read as
    # 'this code moved'".
    from core.currency_vocabulary import load_currency_vocabulary  # noqa: PLC0415

    return frozenset(currency.code.upper() for currency in load_currency_vocabulary())

def _project_row_to_dict(cols: list[str], row: tuple) -> dict:
    """Serialise a projects row; every temporal column, named or not (AI-219)."""
    return row_to_json(cols, row)

def _seed_new_project(proj_id: str) -> None:
    """Story 44.2 -- day-0 knowledge seed hook for a freshly created project.

    Best-effort : un echec d'amorcage ne doit JAMAIS faire echouer la creation
    du projet (le seed est un confort, pas un invariant). A la creation il n'y
    a encore aucun datastream : l'appel est un no-op peu couteux, les topics
    connecteurs arrivent au premier atterrissage.

    ensure_schema=False explicite (decision revue vague 2) : un hook ne
    declenche JAMAIS le generateur de schema docs 11.2. Ici il profilerait
    tout l'entrepot -- y compris des relations d'autres tenants -- pour un
    projet qui n'a encore aucune donnee. Les schema docs restent produites
    par le passage nocturne (SCHEMA_CONTEXT_ENABLED).

    Extrait de _create_project pour etre unit-testable sans Postgres
    (re-review vague 2) : le test patch core.context_seed et appelle ce helper.
    """
    try:
        from core.context_seed import seed_project_context_best_effort  # noqa: PLC0415

        seed_project_context_best_effort(proj_id, ensure_schema=False)
    except Exception as exc:  # noqa: BLE001 -- seeding never fails project creation
        logger.warning("admin_api: context_seed_failed project=%s err=%s", proj_id, exc)

def _valid_timezone(tz: str) -> bool:
    """Return True if *tz* is a valid IANA timezone (window_rule.py pattern)."""
    try:
        import zoneinfo  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        from backports import zoneinfo  # type: ignore[no-redef]  # noqa: PLC0415
    try:
        zoneinfo.ZoneInfo(tz)
        return True
    except Exception:
        return False

def _validate_verification_source_complete(
    vstype: str | None,
    lead_event_name: str | None,
) -> Response | None:
    """Story 17.1: valide la cohérence globale après fusion PATCH.

    Appelé quand verification_source_type='ga4' est l'état final (après merge),
    pour s'assurer que lead_event_name est non nul même si non fourni dans ce PATCH.
    Retourne None si valide, ou une Response 422 si incohérent.
    """
    if vstype == "ga4" and not lead_event_name:
        return JSONResponse(
            {
                "code": "invalid_input",
                "message": (
                    "The lead event name (lead_event_name) is required "
                    "lorsque le type de source est 'ga4'."
                ),
            },
            status_code=422,
        )
    return None

def _validate_verification_source_fields(
    body: dict,
) -> tuple[dict | None, Response | None]:
    """Story 17.1: valide et extrait les 3 champs source de vérification du body.

    Retourne (fields_dict, None) si valide, ou (None, error_response) si invalide.

    Règles (ACs 17.1) :
    - verification_source_type doit être dans {'ga4','shopify','stripe'} ou None/absent.
    - Si verification_source_type = 'ga4', lead_event_name doit être non vide.
    - verification_source_id est libre (validation cross-projet faite ailleurs, AD-5).
    - Les champs sont optionnels : absent = inchangé (PATCH) ou NULL (CREATE).
    - Messages d'erreur en français, code HTTP 422.
    """
    fields: dict = {}
    has_vstype = "verification_source_type" in body
    has_vsid = "verification_source_id" in body
    has_len = "lead_event_name" in body

    if not (has_vstype or has_vsid or has_len):
        return {}, None  # aucun champ source de vérification

    vstype: str | None = None
    if has_vstype:
        raw = body.get("verification_source_type")
        if raw is None or raw == "":
            vstype = None
        elif not isinstance(raw, str):
            return None, JSONResponse(
                {
                    "code": "invalid_input",
                    "message": "verification_source_type must be a string",
                },
                status_code=422,
            )
        else:
            vstype = raw.strip().lower()
            if vstype not in _VERIFICATION_SOURCE_TYPES:
                return None, JSONResponse(
                    {
                        "code": "invalid_input",
                        "message": (
                            f"Invalid verification source type: '{vstype}'. "
                            f"Accepted values: ga4, shopify, stripe."
                        ),
                    },
                    status_code=422,
                )
        fields["verification_source_type"] = vstype

    if has_vsid:
        raw_id = body.get("verification_source_id")
        if raw_id is not None and not isinstance(raw_id, str):
            return None, JSONResponse(
                {"code": "invalid_input", "message": "verification_source_id must be a string"},
                status_code=422,
            )
        fields["verification_source_id"] = raw_id.strip() if raw_id else None
    if has_len:
        raw_len = body.get("lead_event_name")
        if raw_len is not None and not isinstance(raw_len, str):
            return None, JSONResponse(
                {"code": "invalid_input", "message": "lead_event_name must be a string"},
                status_code=422,
            )
        fields["lead_event_name"] = raw_len.strip() if raw_len else None
    # Règle : type='ga4' exige lead_event_name non vide.
    # On évalue avec la valeur fournie OU la valeur présente dans fields.
    effective_type = fields.get("verification_source_type", None) if has_vstype else None
    if effective_type == "ga4":
        effective_len = fields.get("lead_event_name", None)
        if has_len and (not effective_len):
            return None, JSONResponse(
                {
                    "code": "invalid_input",
                    "message": (
                        "The lead event name (lead_event_name) is required "
                        "lorsque le type de source est 'ga4'."
                    ),
                },
                status_code=422,
            )

    return fields, None


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est epissee
# par `admin_api` a la position que ses routes occupaient : memes chemins,
# memes methodes, meme ordre. La preuve est un dump avant/apres, pas une
# lecture de diff.

PROJECTS_ROUTES_1 = [
    Route("/api/projects", endpoint=_list_projects, methods=["GET"]),
    Route("/api/projects", endpoint=_create_project, methods=["POST"]),
    # Restored 2026-07-30 (Epic 46 review, C-8), then RETIRED 2026-08-04 by
    # Story 48.2, Task 4. `85b1deb` had deleted the PATCH line together with
    # the geographic preview/confirm pair while leaving their handlers in
    # place; the PATCH line stays restored below, the pair does not.
    #
    # The pair was the LAST standalone geographic prepare/confirm surface.
    # Country now has one owner and one canonical route -- the governed
    # Master Data registry, `POST /api/projects/{project_id}/governance/
    # master-data/country` (prepare then publish, `governance_surface_api`
    # and `country_workspace_commands`), which is what `CountryWorkspace`
    # calls. Keeping a second prepare/confirm pair alive would be exactly
    # the parallel geographic authority AC1 and AC8 forbid. `_seed_new_project`
    # `_seed_new_project` is unaffected. `core/geographic_change.py` itself was
    # retired on 2026-08-17: with the routes gone and this file no longer writing
    # the posture, its preview -> confirm cycle had no production caller left.
    #
    # The PATCH route MUST stay ahead of the generic `{project_id}` GET/DELETE
    # pair, for the same reason `rotate-key` does below.
    Route("/api/projects/{project_id}", endpoint=_get_project, methods=["GET"]),
    Route("/api/projects/{project_id}", endpoint=_patch_project, methods=["PATCH"]),
    Route("/api/projects/{project_id}", endpoint=_delete_project, methods=["DELETE"]),
    # Story 7.3 (AC5): key rotation endpoint.
    # MUST be declared before the generic {project_id} routes to avoid
    # Starlette absorbing "rotate-key" as a path param on the nested routes.
    Route(
        "/api/projects/{project_id}/rotate-key",
        endpoint=_rotate_project_key,
        methods=["POST"],
    ),
    # AI-363: the gesture the 409 of a second DELETE names.
    Route(
        "/api/projects/{project_id}/restore",
        endpoint=_restore_project,
        methods=["POST"],
    ),
]
