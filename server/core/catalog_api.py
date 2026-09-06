"""Ce qu un connecteur sait offrir, et ce que ce projet en retient.

AD-43, 2026-08-13. Cinq routes : les rapports disponibles et leur reglage, les
connecteurs disponibles et le leur, et les capacites d une source. Le catalogue
est livre AVEC le produit -- ces routes le LISENT et epinglent le choix du
projet ; elles ne le stockent pas.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _project_not_found_response(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _project_not_found_response as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _strict_project_capability_allowed(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _strict_project_capability_allowed as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _list_available_reports(request: Request) -> Response:
    """GET /api/reports/available?project_id=<id> -- merged report availability.

    Response (200):
        [{"module_name", "report_id", "display_name", "enabled", "display_order"}]
        enabled=false for reports not yet opted-in for the project (AC9).

    Error responses:
        400 -- missing project_id
        401 -- unauthorized
        500 -- DB error (module catalog still degrades to enabled=false)
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.query_params.get("project_id") or ""
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )

    # Tenant scope. This endpoint checked ONLY that project_id was non-empty, so
    # any authenticated identity got the full report catalogue for any string --
    # measured in production on 2026-07-27:
    #   GET /api/reports/available?project_id=proj_TOTALEMENT_INEXISTANT_12345
    #   -> 200, 16 reports across 12 modules
    # for a project that does not exist, from an identity that is a member of
    # nothing. It leaked which connector modules this deployment runs, and it is
    # the exact class R43-FR09 ("tenant-scope all resource lists, aggregates and
    # fallbacks") required closed.
    #
    # Same guard and same 404-not-403 answer as every other project-scoped list:
    # existence is itself sensitive, so a caller without access learns nothing.
    try:
        from core.db import get_connection as _get_connection  # noqa: PLC0415

        with _get_connection() as conn:
            allowed = _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            )
    except Exception as exc:
        logger.warning(
            "admin_api: reports-available access unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return _project_not_found_response()
    if not allowed:
        return _project_not_found_response()

    catalog = _connector_report_catalog()

    # Story 7.2 (AC4): load module enablement for this project so we can
    # exclude reports whose module is disabled. Default-enabled when no row exists.
    module_enabled: dict[str, bool] = {}
    # Load per-project enablement rows (project-scoped — never another project).
    enablement: dict[tuple[str, str], dict] = {}
    # Connector modules this project is ACTUALLY connected to (decision Jean,
    # 2026-07-27). A brand-new project has no connection and no row in
    # app.project_modules, and the enablement default is permissive -- so this
    # endpoint answered with the full deployment catalogue, offering reports built
    # on connectors the person does not have.
    #
    # None means the connection set could not be READ. That is not the same as
    # "no connections": on a failed read the filter is skipped entirely rather
    # than silently emptying the list.
    connected_modules: set[str] | None = None
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.module_enablement import is_module_enabled  # noqa: PLC0415

        with get_connection() as conn:
            # Collect distinct module names from catalog to check enablement.
            distinct_modules = {entry["module_name"] for entry in catalog}
            for mod_name in distinct_modules:
                module_enabled[mod_name] = is_module_enabled(mod_name, project_id, conn)

            with conn.cursor() as cur:
                # provider IS module_name -- stated by the per-provider connection
                # count below and by datastreams.py. enabled = TRUE mirrors that same
                # query: a disabled connection is not a source you can report on.
                cur.execute(
                    "SELECT DISTINCT provider FROM app.connection_ref "
                    "WHERE project_id = %s AND provider IS NOT NULL AND enabled",
                    (project_id,),
                )
                connected_modules = {str(r[0]).strip().lower() for r in cur.fetchall()}

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT module_name, report_id, enabled, display_order
                    FROM app.project_reports
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )
                for module_name, report_id, enabled, display_order in cur.fetchall():
                    enablement[(module_name, report_id)] = {
                        "enabled": bool(enabled),
                        "display_order": int(display_order),
                    }
    except Exception as exc:
        logger.warning("admin_api: list_available_reports db_error: %s", exc)
        # Degrade gracefully: return catalog with the opt-in default (disabled).

    reports = []
    for entry in catalog:
        mod_name = entry["module_name"]
        # Story 7.2 (AC4): skip reports for disabled modules.
        # module_enabled defaults to True when DB is unavailable (resilience).
        if not module_enabled.get(mod_name, True):
            continue
        # Only connectors this project actually has. Skipped entirely when the
        # connection set could not be read (None), so a database problem never
        # masquerades as "nothing connected".
        if connected_modules is not None and mod_name.strip().lower() not in connected_modules:
            continue
        key = (mod_name, entry["report_id"])
        row = enablement.get(key)
        reports.append(
            {
                "module_name": mod_name,
                "report_id": entry["report_id"],
                "display_name": entry["display_name"],
                "enabled": row["enabled"] if row else False,
                "display_order": row["display_order"] if row else 0,
            }
        )

    return JSONResponse(reports)

async def _patch_report(request: Request) -> Response:
    """PATCH /api/reports/{project_id}/{connector_name}/{report_id} -- upsert enablement.

    Body (JSON): {"enabled": bool, "display_order": int?}

    Response (200): the upserted row.
    Error responses:
        400 -- missing path params
        401 -- unauthorized
        500 -- DB error
    """
    from ulid import ULID  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.path_params.get("project_id", "")
    connector_name = request.path_params.get("connector_name", "")
    report_id = request.path_params.get("report_id", "")
    if not project_id or not connector_name or not report_id:
        return JSONResponse(
            {"code": "missing_id", "message": "project_id, connector_name, report_id required"},
            status_code=400,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    enabled = bool(body.get("enabled", True))
    display_order = int(body.get("display_order", 0))
    rpt_id = f"rpt_{ULID()}"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            )
    except Exception as exc:
        logger.warning(
            "admin_api: report access unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return _project_not_found_response()
    if not allowed:
        return _project_not_found_response()

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.project_reports
                        (id, project_id, module_name, report_id, enabled, display_order)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, module_name, report_id) DO UPDATE
                        SET enabled = EXCLUDED.enabled,
                            display_order = EXCLUDED.display_order,
                            updated_at = NOW()
                    RETURNING project_id, module_name, report_id, enabled, display_order
                    """,
                    (rpt_id, project_id, connector_name, report_id, enabled, display_order),
                )
                row = cur.fetchone()
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: patch_report db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Report settings are temporarily unavailable"},
            status_code=500,
        )

    return JSONResponse(
        {
            "project_id": row[0],
            "module_name": row[1],
            "report_id": row[2],
            "enabled": bool(row[3]),
            "display_order": int(row[4]),
        }
    )

async def _list_available_connectors(request: Request) -> Response:
    """GET /api/connectors/available?project_id=<id> -- Connector enablement per project.

    Response (200):
        [
          {
            "connector_name": "<connector-kebab-name>",
            "display_name": "<Human-readable name>",
            "enabled": true,
            "explicitly_set": false,
            "active_connections": 1
          },
          ...
        ]
        enabled: True when enabled (including default-enabled).
        explicitly_set: False = default-enabled (no row in project_modules).
                        True  = row exists in project_modules.
        active_connections: count of active+enabled connections for this Connector.

    Error responses:
        400 -- missing project_id
        401 -- unauthorized
        503 -- Connector state unavailable
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )


    # Fetch per-project module enablement rows and active connection counts.
    pm_rows: dict[str, dict] = {}  # module_name -> {"enabled": bool}
    conn_counts: dict[str, int] = {}  # module_name -> active connection count
    default_enabled = True

    try:
        import os  # noqa: PLC0415

        from core.db import get_connection  # noqa: PLC0415

        default_enabled = os.environ.get("MODULE_DEFAULT_ENABLED", "true").lower() != "false"

        with get_connection() as conn:
            if not _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _project_not_found_response()
            discovery = _connector_discovery_catalog()
            with conn.cursor() as cur:
                # Fetch explicit module enablement rows.
                cur.execute(
                    """
                    SELECT module_name, enabled
                    FROM app.project_modules
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )
                for module_name, enabled in cur.fetchall():
                    pm_rows[module_name] = {"enabled": bool(enabled)}

                # Count active+enabled connections per provider (provider = module_name).
                cur.execute(
                    """
                    SELECT provider, COUNT(*) AS cnt
                    FROM app.connection_ref
                    WHERE project_id = %s
                      AND status = 'active'
                      AND enabled = TRUE
                    GROUP BY provider
                    """,
                    (project_id,),
                )
                for provider, cnt in cur.fetchall():
                    conn_counts[provider] = int(cnt)
    except Exception as exc:
        logger.error(
            "admin_api: list_available_connectors unavailable project=%s error=%s",
            project_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "db_error", "message": "Connector settings are temporarily unavailable"},
            status_code=503,
        )

    # WHICH consent flow reaches a module is a property of the module, and the
    # console needs it to offer "connect this" without carrying a list of Google
    # module names of its own (AD-2/AD-21). Derived from the one scope->module
    # map that already governs what a Google grant opens.
    from core.connection_tools import GOOGLE_SCOPE_CONNECTORS  # noqa: PLC0415

    google_direct_modules = set(GOOGLE_SCOPE_CONNECTORS.values())

    result = []
    for mod in discovery:
        mod_name = mod["name"]
        pm = pm_rows.get(mod_name)
        result.append(
            {
                "connector_name": mod_name,
                "display_name": mod["display_name"],
                "enabled": pm["enabled"] if pm is not None else default_enabled,
                "explicitly_set": pm is not None,
                "active_connections": conn_counts.get(mod_name, 0),
                "auth_path": (
                    "google_direct" if mod_name in google_direct_modules else "nango"
                ),
            }
        )

    return JSONResponse(result)

async def _patch_connector(request: Request) -> Response:
    """PATCH /api/connectors/{project_id}/{connector_name} -- upsert Connector enablement.

    Body (JSON): {"enabled": bool}

    Response (200): updated state.
      On disable with active connections: includes a warning field.
    Error responses:
        400 -- missing params or invalid body
        401 -- unauthorized
        503 -- Connector state unavailable
    """
    from ulid import ULID  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.path_params.get("project_id", "")
    connector_name = request.path_params.get("connector_name", "")
    if not project_id or not connector_name:
        return JSONResponse(
            {"code": "missing_id", "message": "project_id and connector_name required"},
            status_code=400,
        )

    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    if not isinstance(body, dict) or "enabled" not in body:
        return JSONResponse(
            {"code": "missing_field", "message": "enabled is required"},
            status_code=400,
        )

    if not isinstance(body["enabled"], bool):
        return JSONResponse(
            {"code": "invalid_field", "message": "enabled must be a boolean"},
            status_code=400,
        )

    enabled = body["enabled"]
    pmod_id = f"pmod_{ULID()}"
    updated_by = identity or "system"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _project_not_found_response()
            if connector_name not in {item["name"] for item in _connector_discovery_catalog()}:
                return _project_not_found_response()
            # Count active connections for the warning (before upsert).
            active_connections = 0
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM app.connection_ref
                    WHERE project_id = %s AND provider = %s
                      AND status = 'active' AND enabled = TRUE
                    """,
                    (project_id, connector_name),
                )
                row = cur.fetchone()
                if row:
                    active_connections = int(row[0])

            # Upsert the enablement row.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.project_modules
                        (id, project_id, module_name, enabled, enabled_at, disabled_at, updated_by)
                    VALUES (
                        %s, %s, %s, %s,
                        CASE WHEN %s THEN NOW() ELSE NULL END,
                        CASE WHEN NOT %s THEN NOW() ELSE NULL END,
                        %s
                    )
                    ON CONFLICT (project_id, module_name) DO UPDATE
                        SET enabled     = EXCLUDED.enabled,
                            enabled_at  = CASE WHEN EXCLUDED.enabled THEN NOW()
                                               ELSE app.project_modules.enabled_at END,
                            disabled_at = CASE WHEN NOT EXCLUDED.enabled THEN NOW()
                                               ELSE app.project_modules.disabled_at END,
                            updated_by  = EXCLUDED.updated_by
                    RETURNING module_name, enabled
                    """,
                    (
                        pmod_id,
                        project_id,
                        connector_name,
                        enabled,
                        enabled,  # enabled_at CASE
                        enabled,  # disabled_at CASE (NOT enabled)
                        updated_by,
                    ),
                )
                upserted = cur.fetchone()
            conn.commit()
    except Exception as exc:
        logger.error(
            "admin_api: patch_connector unavailable project=%s error=%s",
            project_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "db_error", "message": "Connector settings are temporarily unavailable"},
            status_code=503,
        )

    response: dict = {
        "project_id": project_id,
        "connector_name": upserted[0] if upserted else connector_name,
        "enabled": bool(upserted[1]) if upserted else enabled,
    }

    # On disable with active connections: include a warning (no auto-revoke).
    if not enabled and active_connections > 0:
        response["warning"] = (
            f"Connector disabled. {active_connections} active connection(s) will no longer "
            "be pulled."
        )

    return JSONResponse(response)

async def _source_capabilities(request: Request) -> Response:
    """Return the governed capability catalog for one project-owned connection."""

    ok, identity = await _check_auth(request)
    if not ok:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    project_id = request.query_params.get("project_id", "").strip()
    connection_ref_id = request.query_params.get("connection_ref_id", "").strip()
    # Which Connector of the authorization. Only meaningful where one
    # authorization opens several (Google direct); absent for every
    # single-Connector connection.
    module_name = request.query_params.get("connector", "").strip() or None
    if not project_id or not connection_ref_id:
        return JSONResponse(
            {
                "error": "invalid_input",
                "message": "project_id and connection_ref_id are required",
            },
            status_code=400,
        )

    from core import db as _core_db  # noqa: PLC0415
    from core.main import get_loaded_modules  # noqa: PLC0415
    from core.source_capabilities import (  # noqa: PLC0415
        SourceCapabilitiesNotFound,
        SourceCapabilitiesUnavailable,
        get_scoped_source_capabilities,
    )

    try:
        with _core_db.get_connection() as conn:
            catalog = get_scoped_source_capabilities(
                project_id=project_id,
                connection_ref_id=connection_ref_id,
                identity=identity,
                loaded_modules=get_loaded_modules(),
                conn=conn,
                module_name=module_name,
            )
    except SourceCapabilitiesNotFound:
        return JSONResponse({"error": "source_capabilities_not_found"}, status_code=404)
    except SourceCapabilitiesUnavailable:
        return JSONResponse({"error": "source_capabilities_unavailable"}, status_code=503)
    except Exception as exc:  # noqa: BLE001 -- stable public failure contract
        logger.warning("admin_api: source_capabilities_unavailable: %s", type(exc).__name__)
        return JSONResponse({"error": "source_capabilities_unavailable"}, status_code=503)

    return JSONResponse(catalog)

def _connector_discovery_catalog() -> list[dict]:
    """Return [{name, display_name}] from the globally loaded Connector registry.

    Deferred import of core.main to avoid circular imports (same pattern as
    _connector_report_catalog).
    """
    from core.main import get_loaded_modules  # noqa: PLC0415

    return [
        {
            "name": loaded.name,
            "display_name": loaded.manifest.get("display_name", loaded.name),
        }
        for loaded in get_loaded_modules()
    ]

def _connector_report_catalog() -> list[dict]:
    """Return [{connector_name, report_id, display_name}] from the Connector registry.

    Deferred import of core.main avoids a circular import (main imports admin_api
    at startup via build_asgi_app). Mirrors the _health_proxy pattern.
    """
    from core.main import get_loaded_modules  # noqa: PLC0415

    catalog: list[dict] = []
    for loaded in get_loaded_modules():
        for report in loaded.reports:
            catalog.append(
                {
                    "module_name": loaded.name,
                    "report_id": report.get("id"),
                    "display_name": report.get("display_name"),
                }
            )
    return catalog


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : memes
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

CATALOG_ROUTES_1 = [
    Route("/api/source-capabilities", endpoint=_source_capabilities, methods=["GET"]),
]

CATALOG_ROUTES_2 = [
    # Story 6.1 (AC9): report management endpoints.
    # IMPORTANT: the static /available route precedes the parametrized PATCH
    # route so Starlette does not absorb "available" as a project_id param.
    Route("/api/reports/available", endpoint=_list_available_reports, methods=["GET"]),
    Route(
        "/api/reports/{project_id}/{connector_name}/{report_id}",
        endpoint=_patch_report,
        methods=["PATCH"],
    ),
    # Story 7.2 (AC7): Connector enablement endpoints.
    # IMPORTANT: the static /available route precedes the parametrized PATCH
    # route so Starlette does not absorb "available" as a project_id param.
    Route(
        "/api/connectors/available",
        endpoint=_list_available_connectors,
        methods=["GET"],
    ),
    Route(
        "/api/connectors/{project_id}/{connector_name}",
        endpoint=_patch_connector,
        methods=["PATCH"],
    ),
]
