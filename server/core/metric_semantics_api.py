"""toorow -- Metric-semantics curation REST API (Story 27.2).

Provides METRIC_SEMANTICS_ROUTES: list[Route] -- a flat list that admin_api.py
splices into admin_api.router at startup.  This module is NEVER imported by
admin_api.py at module level (no circular import -- same pattern as datamodel_api.py).

Routes:
  GET    /api/metric-semantics/reference            resolved merged layer (page contract)
  GET    /api/metric-semantics/mappings             list source_metric_mappings
  POST   /api/metric-semantics/mappings/{id}/confirm   confirm a proposed mapping
  POST   /api/metric-semantics/mappings/{id}/rename    re-target a mapping
  POST   /api/metric-semantics/mappings/{id}/reject    reject a proposed mapping
  GET    /api/metric-semantics/definitions           list definitions by scope
  POST   /api/metric-semantics/definitions           create/upsert a definition (ORG/PROJECT only)
  DELETE /api/metric-semantics/definitions/{canonical_name}  delete a definition
  POST   /api/metric-semantics/bootstrap             trigger manifest bootstrap for an org

Auth: same _check_auth from core.admin_api (Bearer token via core.api_auth).
Org-scoped access (Epic 21): manage for mutations, read for reads (404 on non-member).
PLATFORM scope FORBIDDEN via API (seeds are the authority).
French error messages throughout (house style).
ASCII-only stdout (AI-03).
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth + org-guard helpers
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api (same as datamodel_api)."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _require_org_read(org_id: str, identity: str, conn) -> bool:
    """Return True if *identity* may read resources of *org_id*.

    Uses identity_has_org_access: any active role (or open org) -> True.
    Non-member of an enrolled org -> False (caller returns 404 to hide existence).
    """
    from core.project_access import identity_has_org_access  # noqa: PLC0415

    return identity_has_org_access(org_id, identity, conn)


def _require_org_manage(org_id: str, identity: str, conn) -> bool:
    """Return True if *identity* is owner/admin of *org_id*.

    Uses identity_can_manage_org: owner/admin (or open org) -> True.
    """
    from core.project_access import identity_can_manage_org  # noqa: PLC0415

    return identity_can_manage_org(org_id, identity, conn)


# ---------------------------------------------------------------------------
# Local read helpers (SELECT only, no store logic duplicated).
# ---------------------------------------------------------------------------


def _list_org_mappings(
    *,
    org_id: str,
    status_filter: str | None = None,
    connector_filter: str | None = None,
) -> list[dict]:
    """List source_metric_mappings at ORG scope, joined with canonical_name.

    Local SELECT only -- no store function duplicated.  Returns rows enriched with
    canonical_name (joined from metric_definitions).
    """
    from core.db import get_connection  # noqa: PLC0415

    params: list = [org_id]
    extra = ""
    if status_filter is not None:
        extra += " AND m.status = %s"
        params.append(status_filter)
    if connector_filter is not None:
        extra += " AND m.connector = %s"
        params.append(connector_filter)

    sql = f"""
        SELECT m.id, m.connector, m.source_field_path, m.extraction_note,
               m.status, m.scope_level, m.org_id, m.project_id,
               m.metric_definition_id, m.created_by,
               m.created_at, m.updated_at,
               d.canonical_name
        FROM app.source_metric_mappings m
        LEFT JOIN app.metric_definitions d ON d.id = m.metric_definition_id
        WHERE m.scope_level = 'ORG' AND m.org_id = %s{extra}
        ORDER BY m.connector, m.source_field_path
    """
    rows: list[dict] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                rec: dict = {}
                for col, val in zip(cols, row):
                    if col in ("created_at", "updated_at") and val is not None:
                        rec[col] = val.isoformat()
                    else:
                        rec[col] = val
                rows.append(rec)
    return rows


def _get_mapping_by_id(mapping_id: str) -> dict | None:
    """Fetch a single source_metric_mapping row by id, joined with canonical_name."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.id, m.connector, m.source_field_path, m.extraction_note,
                       m.status, m.scope_level, m.org_id, m.project_id,
                       m.metric_definition_id, m.created_by,
                       m.created_at, m.updated_at,
                       d.canonical_name
                FROM app.source_metric_mappings m
                LEFT JOIN app.metric_definitions d ON d.id = m.metric_definition_id
                WHERE m.id = %s
                """,
                (mapping_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [desc[0] for desc in cur.description]
            rec: dict = {}
            for col, val in zip(cols, row):
                if col in ("created_at", "updated_at") and val is not None:
                    rec[col] = val.isoformat()
                else:
                    rec[col] = val
    return rec


def _resolve_definition_id(
    canonical_name: str,
    org_id: str | None = None,
    project_id: str | None = None,
) -> str | None:
    """Return a definition id for *canonical_name*, checking ORG then PLATFORM.

    Used by the rename handler to re-target a mapping.
    """
    from core.metric_semantics import (  # noqa: PLC0415
        SCOPE_ORG,
        SCOPE_PLATFORM,
        get_metric_definition,
    )

    # Try ORG first, then PLATFORM.
    if org_id is not None:
        row = get_metric_definition(
            scope_level=SCOPE_ORG, canonical_name=canonical_name, org_id=org_id
        )
        if row is not None:
            return row["id"]
    row = get_metric_definition(scope_level=SCOPE_PLATFORM, canonical_name=canonical_name)
    if row is not None:
        return row["id"]
    return None


def _build_mapping_row(rec: dict) -> dict:
    """Serialise a DB mapping record into the B.2 API shape."""
    return {
        "id": rec.get("id"),
        "connector": rec.get("connector"),
        "source_field_path": rec.get("source_field_path"),
        "canonical_name": rec.get("canonical_name"),
        "metric_definition_id": rec.get("metric_definition_id"),
        "status": rec.get("status"),
        "extraction_note": rec.get("extraction_note"),
        "scope_level": rec.get("scope_level"),
        "org_id": rec.get("org_id"),
        "project_id": rec.get("project_id"),
        "created_by": rec.get("created_by"),
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
    }


# ---------------------------------------------------------------------------
# B.1 GET /api/metric-semantics/reference
# ---------------------------------------------------------------------------


async def _reference(request: Request) -> Response:
    """GET /api/metric-semantics/reference?org_id=&project_id=

    Returns the merged layer (cascade PLATFORM -> ORG -> PROJECT) with per-metric
    reconciliation and source_mappings.  This is the contract the future Metrics page
    consumes -- schema frozen here (Story 27.2 §B.1).

    Auth: any org member (identity_has_org_access); non-member -> 404.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise."},
            status_code=401,
        )

    org_id = (request.query_params.get("org_id") or "").strip() or None
    project_id = (request.query_params.get("project_id") or "").strip() or None

    if org_id is None:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id est requis."},
            status_code=400,
        )

    # Org-read guard.
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _require_org_read(org_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Organisation introuvable."},
                    status_code=404,
                )
    except Exception as exc:
        logger.error("metric_semantics_api: reference guard failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    # F-3: project_id was passed straight to _load_definition_rows, letting a member of
    # org A read the PROJECT definitions of a project belonging to org B.  Verify the
    # project belongs to the guarded org before use.  Absent/foreign project => 404.
    if project_id is not None:
        try:
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                    )
                    row = cur.fetchone()
        except Exception as exc:
            logger.error("metric_semantics_api: reference project lookup failed: %s", exc)
            return JSONResponse(
                {"code": "server_error", "message": "Erreur serveur."},
                status_code=500,
            )
        if not row or row[0] != org_id:
            return JSONResponse(
                {"code": "not_found", "message": "Projet introuvable."},
                status_code=404,
            )

    # Resolve definitions via the cascade.
    try:
        from core.metric_semantics import (  # noqa: PLC0415
            _load_definition_rows,
            _load_reconciliation_rows,
            reduce_definitions_by_specificity,
            reduce_reconciliation_by_specificity,
        )

        # Load definitions: PLATFORM + ORG + (PROJECT if provided).
        def_rows = _load_definition_rows(org_id=org_id, project_id=project_id)
        definitions = reduce_definitions_by_specificity(def_rows)

        # Load source_mappings: ORG scope for this org.
        from core.db import get_connection  # noqa: PLC0415

        mappings_by_definition: dict[str, list[dict]] = {}
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT m.metric_definition_id, m.connector, m.source_field_path,
                           m.status
                    FROM app.source_metric_mappings m
                    WHERE m.scope_level = 'ORG' AND m.org_id = %s
                    ORDER BY m.connector, m.source_field_path
                    """,
                    (org_id,),
                )
                for row in cur.fetchall():
                    defid = row[0]
                    if defid not in mappings_by_definition:
                        mappings_by_definition[defid] = []
                    mappings_by_definition[defid].append(
                        {
                            "connector": row[1],
                            "source_field_path": row[2],
                            "status": row[3],
                        }
                    )

        # Build the metrics list.
        metrics = []
        for canonical_name, defn in sorted(definitions.items()):
            # Reconciliation: most specific rule covering this metric.
            rec_rows = _load_reconciliation_rows(
                org_id=org_id, project_id=project_id, metric=canonical_name
            )
            rec_rule = reduce_reconciliation_by_specificity(rec_rows, canonical_name)

            reconciliation = None
            if rec_rule is not None:
                reconciliation = {
                    "method": rec_rule.get("method"),
                    "priority_order": rec_rule.get("priority_order"),
                    "join_key": rec_rule.get("join_key"),
                    "truth_connector": rec_rule.get("truth_connector"),
                    "resolved_scope": rec_rule.get("scope_level"),
                }

            source_mappings = mappings_by_definition.get(defn.get("id", ""), [])

            metrics.append(
                {
                    "canonical_name": canonical_name,
                    "display_name": defn.get("display_name"),
                    "aggregation_type": defn.get("aggregation_type"),
                    "additive": defn.get("additive"),
                    "ratio_numerator": defn.get("ratio_numerator"),
                    "ratio_denominator": defn.get("ratio_denominator"),
                    "format": defn.get("format"),
                    "unit": defn.get("unit"),
                    "currency_mode": defn.get("currency_mode"),
                    "non_additive_dimensions": defn.get("non_additive_dimensions") or [],
                    "synonyms": defn.get("synonyms") or [],
                    "ai_context": defn.get("ai_context"),
                    "certified": defn.get("certified", False),
                    "resolved_scope": defn.get("scope_level"),
                    "reconciliation": reconciliation,
                    "source_mappings": source_mappings,
                }
            )

    except Exception as exc:
        logger.error("metric_semantics_api: reference failed org=%s: %s", org_id, exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    return JSONResponse(
        {
            "scope": {"org_id": org_id, "project_id": project_id},
            "metrics": metrics,
        }
    )


# ---------------------------------------------------------------------------
# B.2 GET /api/metric-semantics/mappings
# ---------------------------------------------------------------------------


async def _list_mappings(request: Request) -> Response:
    """GET /api/metric-semantics/mappings?org_id=&status=&connector=

    Lists source_metric_mappings at ORG scope, optionally filtered.
    Auth: any org member (identity_has_org_access); non-member -> 404.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise."},
            status_code=401,
        )

    org_id = (request.query_params.get("org_id") or "").strip() or None
    status_filter = (request.query_params.get("status") or "").strip() or None
    connector_filter = (request.query_params.get("connector") or "").strip() or None

    if org_id is None:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id est requis."},
            status_code=400,
        )

    _VALID_STATUSES = frozenset({"proposed", "confirmed", "renamed", "rejected"})
    if status_filter is not None and status_filter not in _VALID_STATUSES:
        return JSONResponse(
            {"code": "invalid_param", "message": "Statut invalide."},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _require_org_read(org_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Organisation introuvable."},
                    status_code=404,
                )
    except Exception as exc:
        logger.error("metric_semantics_api: mappings guard failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    try:
        rows = _list_org_mappings(
            org_id=org_id,
            status_filter=status_filter,
            connector_filter=connector_filter,
        )
    except Exception as exc:
        logger.error("metric_semantics_api: list_mappings failed org=%s: %s", org_id, exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    return JSONResponse({"mappings": [_build_mapping_row(r) for r in rows]})


# ---------------------------------------------------------------------------
# B.3a POST /api/metric-semantics/mappings/{id}/confirm
# ---------------------------------------------------------------------------


async def _confirm_mapping(request: Request) -> Response:
    """POST /api/metric-semantics/mappings/{id}/confirm  body: {"org_id": "..."}

    Transitions a proposed mapping to confirmed.
    Auth: manage (owner/admin).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise."},
            status_code=401,
        )

    mapping_id = request.path_params.get("id", "")
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"code": "invalid_json", "message": "Corps JSON invalide."},
            status_code=400,
        )

    org_id = (body.get("org_id") or "").strip() or None
    if org_id is None:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id est requis."},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _require_org_manage(org_id, identity, conn):
                return JSONResponse(
                    {"code": "forbidden", "message": "Droits insuffisants."},
                    status_code=403,
                )
    except Exception as exc:
        logger.error("metric_semantics_api: confirm guard failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    existing = _get_mapping_by_id(mapping_id)
    # F-1: the manage guard was checked against org_id from the BODY; ensure the
    # fetched mapping actually belongs to that org and is ORG-scoped, otherwise an
    # admin of org A could mutate a mapping of org B (IDOR). 404 hides existence.
    if (
        existing is None
        or existing.get("org_id") != org_id
        or existing.get("scope_level") != "ORG"
    ):
        return JSONResponse(
            {"code": "not_found", "message": "Mapping introuvable."},
            status_code=404,
        )

    try:
        from core.metric_semantics import upsert_source_metric_mapping  # noqa: PLC0415

        updated = upsert_source_metric_mapping(
            metric_definition_id=existing["metric_definition_id"],
            connector=existing["connector"],
            source_field_path=existing.get("source_field_path"),
            extraction_note=existing.get("extraction_note"),
            scope_level=existing["scope_level"],
            org_id=existing.get("org_id"),
            project_id=existing.get("project_id"),
            status="confirmed",
            created_by=identity,
        )
    except Exception as exc:
        logger.error(
            "metric_semantics_api: confirm failed id=%s: %s", mapping_id, exc
        )
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    # Re-fetch to include canonical_name in response.
    refreshed = _get_mapping_by_id(updated["id"])
    return JSONResponse(_build_mapping_row(refreshed or updated))


# ---------------------------------------------------------------------------
# B.3b POST /api/metric-semantics/mappings/{id}/rename
# ---------------------------------------------------------------------------


async def _rename_mapping(request: Request) -> Response:
    """POST /api/metric-semantics/mappings/{id}/rename
    body: {"org_id": "...", "canonical_name": "..."}

    Re-targets a mapping to another canonical metric (status -> renamed).
    Auth: manage (owner/admin).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise."},
            status_code=401,
        )

    mapping_id = request.path_params.get("id", "")
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"code": "invalid_json", "message": "Corps JSON invalide."},
            status_code=400,
        )

    org_id = (body.get("org_id") or "").strip() or None
    new_canonical = (body.get("canonical_name") or "").strip() or None

    if org_id is None:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id est requis."},
            status_code=400,
        )
    if new_canonical is None:
        return JSONResponse(
            {"code": "missing_param", "message": "canonical_name est requis."},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _require_org_manage(org_id, identity, conn):
                return JSONResponse(
                    {"code": "forbidden", "message": "Droits insuffisants."},
                    status_code=403,
                )
    except Exception as exc:
        logger.error("metric_semantics_api: rename guard failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    existing = _get_mapping_by_id(mapping_id)
    # F-1: verify the fetched mapping belongs to the guarded org and is ORG-scoped
    # (guard was on body org_id) -- else org A admin could re-target org B mapping.
    if (
        existing is None
        or existing.get("org_id") != org_id
        or existing.get("scope_level") != "ORG"
    ):
        return JSONResponse(
            {"code": "not_found", "message": "Mapping introuvable."},
            status_code=404,
        )

    # Resolve the target definition (ORG then PLATFORM).
    target_definition_id = _resolve_definition_id(
        new_canonical, org_id=existing.get("org_id"), project_id=existing.get("project_id")
    )
    if target_definition_id is None:
        return JSONResponse(
            {
                "code": "not_found",
                "message": "Metrique cible introuvable.",
            },
            status_code=422,
        )

    try:
        from core.metric_semantics import upsert_source_metric_mapping  # noqa: PLC0415

        updated = upsert_source_metric_mapping(
            metric_definition_id=target_definition_id,
            connector=existing["connector"],
            source_field_path=existing.get("source_field_path"),
            extraction_note=existing.get("extraction_note"),
            scope_level=existing["scope_level"],
            org_id=existing.get("org_id"),
            project_id=existing.get("project_id"),
            status="renamed",
            created_by=identity,
        )
    except Exception as exc:
        logger.error(
            "metric_semantics_api: rename failed id=%s: %s", mapping_id, exc
        )
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    refreshed = _get_mapping_by_id(updated["id"])
    return JSONResponse(_build_mapping_row(refreshed or updated))


# ---------------------------------------------------------------------------
# B.3c POST /api/metric-semantics/mappings/{id}/reject
# ---------------------------------------------------------------------------


async def _reject_mapping(request: Request) -> Response:
    """POST /api/metric-semantics/mappings/{id}/reject  body: {"org_id": "..."}

    Rejects a proposed mapping (status -> rejected).
    Auth: manage (owner/admin).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise."},
            status_code=401,
        )

    mapping_id = request.path_params.get("id", "")
    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"code": "invalid_json", "message": "Corps JSON invalide."},
            status_code=400,
        )

    org_id = (body.get("org_id") or "").strip() or None
    if org_id is None:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id est requis."},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _require_org_manage(org_id, identity, conn):
                return JSONResponse(
                    {"code": "forbidden", "message": "Droits insuffisants."},
                    status_code=403,
                )
    except Exception as exc:
        logger.error("metric_semantics_api: reject guard failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    existing = _get_mapping_by_id(mapping_id)
    # F-1: verify the fetched mapping belongs to the guarded org and is ORG-scoped
    # (guard was on body org_id) -- else org A admin could reject org B mapping.
    if (
        existing is None
        or existing.get("org_id") != org_id
        or existing.get("scope_level") != "ORG"
    ):
        return JSONResponse(
            {"code": "not_found", "message": "Mapping introuvable."},
            status_code=404,
        )

    try:
        from core.metric_semantics import upsert_source_metric_mapping  # noqa: PLC0415

        updated = upsert_source_metric_mapping(
            metric_definition_id=existing["metric_definition_id"],
            connector=existing["connector"],
            source_field_path=existing.get("source_field_path"),
            extraction_note=existing.get("extraction_note"),
            scope_level=existing["scope_level"],
            org_id=existing.get("org_id"),
            project_id=existing.get("project_id"),
            status="rejected",
            created_by=identity,
        )
    except Exception as exc:
        logger.error(
            "metric_semantics_api: reject failed id=%s: %s", mapping_id, exc
        )
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    refreshed = _get_mapping_by_id(updated["id"])
    return JSONResponse(_build_mapping_row(refreshed or updated))


# ---------------------------------------------------------------------------
# B.4a GET /api/metric-semantics/definitions
# ---------------------------------------------------------------------------


async def _list_definitions(request: Request) -> Response:
    """GET /api/metric-semantics/definitions?org_id=&project_id=&scope_level=

    Lists metric definitions at the requested scope.
    Auth: any org member for reads; PLATFORM scope is readable.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise."},
            status_code=401,
        )

    org_id = (request.query_params.get("org_id") or "").strip() or None
    project_id = (request.query_params.get("project_id") or "").strip() or None
    scope_level = (request.query_params.get("scope_level") or "PLATFORM").strip().upper()

    _VALID_SCOPES = frozenset({"PLATFORM", "ORG", "PROJECT"})
    if scope_level not in _VALID_SCOPES:
        return JSONResponse(
            {"code": "invalid_param", "message": "scope_level invalide."},
            status_code=422,
        )

    # N-1: for non-PLATFORM scopes the org-read guard must ALWAYS run.  The former
    # `and org_id is not None` short-circuit let an authenticated caller OMIT org_id
    # with scope=PROJECT&project_id=<foreign project> (PROJECT rows carry org_id NULL
    # in DB) and read another org's PROJECT definitions unchecked.  Same F-3 pattern as
    # /reference: never let org_id=None bypass the guard.
    if scope_level != "PLATFORM":
        # Resolve the org to guard on.  PROJECT: from the project row (404 if
        # absent/irresolvable).  ORG: org_id is mandatory (404 if absent).  When org_id
        # IS given: verify the project (if any) belongs to that org (F-3 class defect).
        guard_org_id = org_id
        try:
            from core.db import get_connection  # noqa: PLC0415

            if project_id is not None:
                with get_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT org_id FROM app.projects WHERE id = %s",
                            (project_id,),
                        )
                        row = cur.fetchone()
                if not row or not row[0]:
                    return JSONResponse(
                        {"code": "not_found", "message": "Projet introuvable."},
                        status_code=404,
                    )
                project_org_id = row[0]
                if guard_org_id is None:
                    guard_org_id = project_org_id
                elif project_org_id != guard_org_id:
                    # org_id supplied but the project belongs to another org -> 404.
                    return JSONResponse(
                        {"code": "not_found", "message": "Projet introuvable."},
                        status_code=404,
                    )
        except Exception as exc:
            logger.error("metric_semantics_api: definitions project lookup failed: %s", exc)
            return JSONResponse(
                {"code": "server_error", "message": "Erreur serveur."},
                status_code=500,
            )

        # No resolvable org (scope=ORG without org_id, or nothing to resolve) -> 404.
        if guard_org_id is None:
            return JSONResponse(
                {"code": "not_found", "message": "Organisation introuvable."},
                status_code=404,
            )

        try:
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                if not _require_org_read(guard_org_id, identity, conn):
                    return JSONResponse(
                        {"code": "not_found", "message": "Organisation introuvable."},
                        status_code=404,
                    )
        except Exception as exc:
            logger.error("metric_semantics_api: definitions guard failed: %s", exc)
            return JSONResponse(
                {"code": "server_error", "message": "Erreur serveur."},
                status_code=500,
            )

    try:
        from core.metric_semantics import list_metric_definitions_by_scope  # noqa: PLC0415

        rows = list_metric_definitions_by_scope(
            scope_level=scope_level, org_id=org_id, project_id=project_id
        )
    except Exception as exc:
        logger.error("metric_semantics_api: list_definitions failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    return JSONResponse({"definitions": rows})


# ---------------------------------------------------------------------------
# B.4b POST /api/metric-semantics/definitions
# ---------------------------------------------------------------------------


async def _create_definition(request: Request) -> Response:
    """POST /api/metric-semantics/definitions

    Creates or updates a metric definition at ORG or PROJECT scope.
    PLATFORM scope is FORBIDDEN via the API (seeds are the authority) -> 403.
    Auth: manage (owner/admin of the target org).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise."},
            status_code=401,
        )

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"code": "invalid_json", "message": "Corps JSON invalide."},
            status_code=400,
        )

    scope_level = (body.get("scope_level") or "").strip().upper()
    if scope_level == "PLATFORM":
        return JSONResponse(
            {
                "code": "forbidden",
                "message": "La portee PLATFORM ne peut pas etre modifiee via l'API.",
            },
            status_code=403,
        )

    _VALID_SCOPES = frozenset({"ORG", "PROJECT"})
    if scope_level not in _VALID_SCOPES:
        return JSONResponse(
            {"code": "invalid_param", "message": "scope_level doit etre ORG ou PROJECT."},
            status_code=422,
        )

    org_id = (body.get("org_id") or "").strip() or None
    project_id = (body.get("project_id") or "").strip() or None
    canonical_name = (body.get("canonical_name") or "").strip() or None
    aggregation_type = (body.get("aggregation_type") or "").strip() or None

    if canonical_name is None:
        return JSONResponse(
            {"code": "missing_param", "message": "canonical_name est requis."},
            status_code=422,
        )
    if aggregation_type is None:
        return JSONResponse(
            {"code": "missing_param", "message": "aggregation_type est requis."},
            status_code=422,
        )

    additive = body.get("additive", True)
    if not isinstance(additive, bool):
        return JSONResponse(
            {"code": "invalid_param", "message": "additive doit etre un booleen."},
            status_code=422,
        )

    # Manage guard: require org.  F-2: NEVER fall through to the write when the manage
    # decision cannot be rendered -- if guard_org_id stays None after resolution the
    # write must NOT happen unguarded.  404 (existence-hiding) in that case.
    guard_org_id = org_id
    if guard_org_id is None and project_id is not None:
        # Resolve org from project.  Do NOT swallow the exception into a fall-through:
        # a failed lookup must 404, never let the write proceed guardless.
        try:
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                    )
                    row = cur.fetchone()
            if row and row[0]:
                guard_org_id = row[0]
        except Exception as exc:
            logger.error(
                "metric_semantics_api: create_definition project->org lookup failed: %s",
                exc,
            )
            return JSONResponse(
                {"code": "not_found", "message": "Projet introuvable."},
                status_code=404,
            )

    # F-2: no resolvable org (project missing, org_id NULL legacy, or no org given)
    # => refuse before any write, existence-hiding.
    if guard_org_id is None:
        return JSONResponse(
            {"code": "not_found", "message": "Projet introuvable."},
            status_code=404,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _require_org_manage(guard_org_id, identity, conn):
                return JSONResponse(
                    {"code": "forbidden", "message": "Droits insuffisants."},
                    status_code=403,
                )
    except Exception as exc:
        logger.error("metric_semantics_api: create_definition guard failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    try:
        from core.metric_semantics import (  # noqa: PLC0415
            InvalidReconciliationRule,
            InvalidScope,
            upsert_metric_definition,
        )

        row = upsert_metric_definition(
            canonical_name=canonical_name,
            aggregation_type=aggregation_type,
            additive=additive,
            scope_level=scope_level,
            org_id=org_id,
            project_id=project_id,
            created_by=identity,
            display_name=body.get("display_name") or None,
            description=body.get("description") or None,
            ratio_numerator=body.get("ratio_numerator") or None,
            ratio_denominator=body.get("ratio_denominator") or None,
            format=body.get("format") or None,
            unit=body.get("unit") or None,
            currency_mode=body.get("currency_mode") or None,
            non_additive_dimensions=body.get("non_additive_dimensions") or None,
            synonyms=body.get("synonyms") or None,
            ai_context=body.get("ai_context") or None,
            certified=bool(body.get("certified", False)),
        )
    except (InvalidScope, InvalidReconciliationRule) as exc:
        # S-1: never propagate the internal exception text to the caller (it can leak
        # scope/id internals). Constant FR message out; the detail goes to the log only.
        logger.warning("metric_semantics_api: create_definition rejected scope: %s", exc)
        return JSONResponse(
            {"code": "invalid_scope", "message": "Scope invalide."},
            status_code=422,
        )
    except Exception as exc:
        logger.error("metric_semantics_api: create_definition failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    return JSONResponse(row, status_code=201)


# ---------------------------------------------------------------------------
# B.4c DELETE /api/metric-semantics/definitions/{canonical_name}
# ---------------------------------------------------------------------------


async def _delete_definition(request: Request) -> Response:
    """DELETE /api/metric-semantics/definitions/{canonical_name}
       ?org_id=&project_id=&scope_level=

    Deletes a metric definition at ORG or PROJECT scope.
    PLATFORM scope is FORBIDDEN via the API -> 403.
    Auth: manage (owner/admin).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise."},
            status_code=401,
        )

    canonical_name = request.path_params.get("canonical_name", "")
    org_id = (request.query_params.get("org_id") or "").strip() or None
    project_id = (request.query_params.get("project_id") or "").strip() or None
    scope_level = (request.query_params.get("scope_level") or "").strip().upper()

    if scope_level == "PLATFORM":
        return JSONResponse(
            {
                "code": "forbidden",
                "message": "La portee PLATFORM ne peut pas etre supprimee via l'API.",
            },
            status_code=403,
        )

    _VALID_SCOPES = frozenset({"ORG", "PROJECT"})
    if scope_level not in _VALID_SCOPES:
        return JSONResponse(
            {"code": "invalid_param", "message": "scope_level doit etre ORG ou PROJECT."},
            status_code=422,
        )

    # F-2: org resolution is mandatory before deleting -- a delete without a resolvable
    # org must NOT proceed unguarded.  Resolve org from project when org_id absent.
    guard_org_id = org_id
    if guard_org_id is None and project_id is not None:
        try:
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                    )
                    row = cur.fetchone()
            if row and row[0]:
                guard_org_id = row[0]
        except Exception as exc:
            logger.error(
                "metric_semantics_api: delete_definition project->org lookup failed: %s",
                exc,
            )
            return JSONResponse(
                {"code": "not_found", "message": "Projet introuvable."},
                status_code=404,
            )

    # F-2: no resolvable org => refuse before delete, existence-hiding.
    if guard_org_id is None:
        return JSONResponse(
            {"code": "not_found", "message": "Definition introuvable."},
            status_code=404,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _require_org_manage(guard_org_id, identity, conn):
                return JSONResponse(
                    {"code": "forbidden", "message": "Droits insuffisants."},
                    status_code=403,
                )
    except Exception as exc:
        logger.error("metric_semantics_api: delete_definition guard failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    try:
        from core.metric_semantics import delete_metric_definition  # noqa: PLC0415

        deleted = delete_metric_definition(
            scope_level=scope_level,
            canonical_name=canonical_name,
            org_id=org_id,
            project_id=project_id,
            identity=identity,
        )
    except Exception as exc:
        logger.error("metric_semantics_api: delete_definition failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    if not deleted:
        return JSONResponse(
            {"code": "not_found", "message": "Definition introuvable."},
            status_code=404,
        )

    return JSONResponse({"deleted": True})


# ---------------------------------------------------------------------------
# B.5 POST /api/metric-semantics/bootstrap
# ---------------------------------------------------------------------------


async def _trigger_bootstrap(request: Request) -> Response:
    """POST /api/metric-semantics/bootstrap  body: {"org_id": "...", "connectors": [...]}

    Triggers the manifest bootstrap for an org.  Calls import_platform_defaults()
    first (idempotent) so the first bootstrap works without a prior manual seed import.
    Auth: manage (owner/admin).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise."},
            status_code=401,
        )

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"code": "invalid_json", "message": "Corps JSON invalide."},
            status_code=400,
        )

    org_id = (body.get("org_id") or "").strip() or None
    if org_id is None:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id est requis."},
            status_code=400,
        )

    connectors = body.get("connectors")
    if connectors is not None and not isinstance(connectors, list):
        return JSONResponse(
            {"code": "invalid_param", "message": "connectors doit etre une liste."},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _require_org_manage(org_id, identity, conn):
                return JSONResponse(
                    {"code": "forbidden", "message": "Droits insuffisants."},
                    status_code=403,
                )
    except Exception as exc:
        logger.error("metric_semantics_api: bootstrap guard failed: %s", exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    try:
        # Ensure PLATFORM defaults exist (idempotent -- no audit on identical re-import).
        from core.metric_semantics import import_platform_defaults  # noqa: PLC0415

        import_platform_defaults(identity="system")
    except Exception as exc:
        logger.warning(
            "metric_semantics_api: import_platform_defaults failed (non-fatal): %s", exc
        )

    try:
        from core.metric_semantics_bootstrap import (  # noqa: PLC0415
            bootstrap_org_source_mappings,
        )

        report = bootstrap_org_source_mappings(
            org_id=org_id,
            connectors=connectors,
            identity=identity,
        )
    except Exception as exc:
        logger.error("metric_semantics_api: bootstrap failed org=%s: %s", org_id, exc)
        return JSONResponse(
            {"code": "server_error", "message": "Erreur serveur."},
            status_code=500,
        )

    return JSONResponse(report)


# ---------------------------------------------------------------------------
# B.6 Route list (order matters: static sub-resources before parametrized routes).
# ---------------------------------------------------------------------------

METRIC_SEMANTICS_ROUTES: list[Route] = [
    # B.1 -- merged reference layer
    Route("/api/metric-semantics/reference", _reference, methods=["GET"]),
    # B.2 -- list mappings
    Route("/api/metric-semantics/mappings", _list_mappings, methods=["GET"]),
    # B.3 -- curation sub-resources (static paths before any future {id} bare route)
    Route("/api/metric-semantics/mappings/{id}/confirm", _confirm_mapping, methods=["POST"]),
    Route("/api/metric-semantics/mappings/{id}/rename", _rename_mapping, methods=["POST"]),
    Route("/api/metric-semantics/mappings/{id}/reject", _reject_mapping, methods=["POST"]),
    # B.4 -- definitions CRUD
    Route("/api/metric-semantics/definitions", _list_definitions, methods=["GET"]),
    Route("/api/metric-semantics/definitions", _create_definition, methods=["POST"]),
    Route(
        "/api/metric-semantics/definitions/{canonical_name}",
        _delete_definition,
        methods=["DELETE"],
    ),
    # B.5 -- bootstrap trigger
    Route("/api/metric-semantics/bootstrap", _trigger_bootstrap, methods=["POST"]),
]
