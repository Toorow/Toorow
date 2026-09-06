"""Ce qui fait sonner une alerte : sa definition, son seuil, sa portee.

AD-43, 2026-08-13. Une definition est ce qui DECLENCHE ; une destination est ce
par ou l alerte SORT. Deux objets, deux modules -- et ce n est pas une invention
de ce decoupage : `core/audit.py` porte deja cette phrase au-dessus de leurs
codes d action respectifs.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import (
    declare_action,
    write_audit_row,
)
from core.row_json import row_to_json

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _refuse_unless_project_allowed(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _refuse_unless_project_allowed as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

ACTION_ALERT_DEF_CREATED = declare_action("alert_def.created")

ACTION_ALERT_DEF_DELETED = declare_action("alert_def.deleted")

ACTION_ALERT_DEF_UPDATED = declare_action("alert_def.updated")

# Valid metric names: additive metrics from dim_metric + semantic view names.
# Driven by ALERT_SEMANTIC_METRICS env var at runtime (deferred import below).
_ALERT_ADDITIVE_METRICS = frozenset(
    [
        "sessions",
        "active_users",
        "conversions",
        "cost",
        "impressions",
        "clicks",
        "revenue",
        "average_position",
    ]
)

# Valid operators for alert threshold comparisons (whitelist enforced at CRUD).
_ALERT_OPERATOR_WHITELIST = {"<", ">", "<=", ">="}

async def _list_alert_definitions(request: Request) -> Response:
    """GET /api/alert-definitions?project_id=<id> -- list definitions for a project.

    Includes last firing date and value per definition via LEFT JOIN on alert_firings.

    Response (200):
        {"definitions": [{id, project_id, metric, operator, threshold, connector,
                          enabled, created_by, created_at, updated_at,
                          last_firing_date, last_firing_value}]}

    Error responses:
        400 -- missing project_id
        401 -- unauthorized
        404 -- project unknown OR not visible to this identity
        500 -- DB error

    AI-171 -- `WHERE d.project_id = %s` BORNE LA REQUETE, PAS L'APPELANT. Le
    parametre etait exige, jamais autorise : tout porteur d'un jeton valide, de
    n'importe quelle organisation, lisait les seuils d'alerte d'un autre tenant.
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
    denied = _refuse_unless_project_allowed(
        identity, project_id, "view", "list_alert_definitions"
    )
    if denied is not None:
        return denied

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        d.id, d.project_id, d.metric, d.operator, d.threshold,
                        d.connector, d.enabled, d.created_by, d.created_at, d.updated_at,
                        lf.last_firing_date,
                        lf.last_firing_value
                    FROM app.alert_definitions d
                    LEFT JOIN (
                        SELECT
                            definition_id,
                            MAX(window_date) AS last_firing_date,
                            (ARRAY_AGG(observed_value ORDER BY fired_at DESC))[1]
                                AS last_firing_value
                        FROM app.alert_firings
                        GROUP BY definition_id
                    ) lf ON lf.definition_id = d.id
                    WHERE d.project_id = %s
                    ORDER BY d.created_at DESC
                    """,
                    (project_id,),
                )
                definitions = [
                    _alert_definition_row(cur, row) for row in cur.fetchall()
                ]
    except Exception as exc:
        logger.error("admin_api: list_alert_definitions_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse({"definitions": definitions})

async def _create_alert_definition(request: Request) -> Response:
    """POST /api/alert-definitions -- create a new alert definition.

    Request body (JSON):
        {"project_id": str, "metric": str, "operator": str,
         "threshold": number, "connector": str?}

    Response (201):
        {id, project_id, metric, operator, threshold, connector, enabled,
         created_by, created_at, updated_at}

    Error responses:
        400 -- missing required fields
        401 -- unauthorized
        422 -- invalid operator or unknown metric
        500 -- DB error
    """
    from ulid import ULID  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes)
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    metric = (body.get("metric") or "").strip().lower()
    operator = (body.get("operator") or "").strip()
    threshold_raw = body.get("threshold")
    connector = (body.get("connector") or "").strip() or None

    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id is required"},
            status_code=400,
        )
    # AI-171 : le `project_id` vient du CORPS, et rien ne verifiait que l'appelant
    # avait le droit d'ecrire dans ce projet -- une definition d'alerte se creait
    # chez le tenant de son choix. Autorise avant toute validation de contenu :
    # un 422 sur un projet qu'on n'a pas le droit de voir en confirme l'existence.
    denied = _refuse_unless_project_allowed(
        identity, project_id, "edit", "create_alert_definition"
    )
    if denied is not None:
        return denied
    if not metric:
        return JSONResponse(
            {"code": "missing_field", "message": "metric is required"},
            status_code=400,
        )
    if not operator:
        return JSONResponse(
            {"code": "missing_field", "message": "operator is required"},
            status_code=400,
        )
    if threshold_raw is None:
        return JSONResponse(
            {"code": "missing_field", "message": "threshold is required"},
            status_code=400,
        )

    # Operator whitelist validation
    if operator not in _ALERT_OPERATOR_WHITELIST:
        return JSONResponse(
            {
                "code": "invalid_operator",
                "message": (
                    f"operator must be one of {sorted(_ALERT_OPERATOR_WHITELIST)}, "
                    f"got: {operator!r}"
                ),
            },
            status_code=422,
        )

    # Metric validation
    valid_metrics = _get_valid_alert_metrics()
    if metric not in valid_metrics:
        return JSONResponse(
            {
                "code": "unknown_metric",
                "message": (
                    f"metric {metric!r} is not a known metric. "
                    f"Valid metrics: {sorted(valid_metrics)}"
                ),
            },
            status_code=422,
        )

    # Threshold must be numeric
    try:
        threshold = float(threshold_raw)
    except (TypeError, ValueError):
        return JSONResponse(
            {"code": "invalid_threshold", "message": "threshold must be a number"},
            status_code=422,
        )

    alert_id = f"alrt_{ULID()}"
    created_by = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.alert_definitions
                        (id, project_id, metric, operator, threshold, connector,
                         enabled, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s)
                    RETURNING id, project_id, metric, operator, threshold, connector,
                              enabled, created_by, created_at, updated_at
                    """,
                    (alert_id, project_id, metric, operator, threshold, connector, created_by),
                )
                row = cur.fetchone()
                if row is None:  # pragma: no cover
                    raise RuntimeError("INSERT RETURNING returned no row")
                created_record = _alert_definition_row(cur, row)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: create_alert_definition_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=created_by,
        action=ACTION_ALERT_DEF_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "alert_def_id": alert_id,
            "project_id": project_id,
            "metric": metric,
            "operator": operator,
            "threshold": threshold,
        },
    )

    return JSONResponse(created_record, status_code=201)

async def _update_alert_definition(request: Request) -> Response:
    """PATCH /api/alert-definitions/{id} -- toggle enabled or update threshold.

    Request body (JSON, all fields optional):
        {"enabled": bool?, "threshold": number?}

    Response (200):
        {id, project_id, metric, operator, threshold, connector, enabled,
         created_by, created_at, updated_at}

    Error responses:
        400 -- empty body / no updatable fields
        401 -- unauthorized
        404 -- definition not found
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    alert_id = request.path_params.get("id", "")
    if not alert_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Alert definition id is required"},
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

    # Build the SET clause from allowed updatable fields
    set_clauses: list[str] = []
    params: list = []

    if "enabled" in body:
        set_clauses.append("enabled = %s")
        params.append(bool(body["enabled"]))

    if "threshold" in body:
        try:
            threshold_val = float(body["threshold"])
            set_clauses.append("threshold = %s")
            params.append(threshold_val)
        except (TypeError, ValueError):
            return JSONResponse(
                {"code": "invalid_threshold", "message": "threshold must be a number"},
                status_code=422,
            )

    if not set_clauses:
        return JSONResponse(
            {
                "code": "no_update_fields",
                "message": "Provide 'enabled' or 'threshold' to update",
            },
            status_code=400,
        )

    set_clauses.append("updated_at = NOW()")
    params.append(alert_id)

    sql = (
        "UPDATE app.alert_definitions SET "
        + ", ".join(set_clauses)
        + " WHERE id = %s RETURNING id, project_id, metric, operator, threshold,"
        " connector, enabled, created_by, created_at, updated_at"
    )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {
                            "code": "not_found",
                            "message": f"Alert definition '{alert_id}' not found",
                        },
                        status_code=404,
                    )
                updated_record = _alert_definition_row(cur, row)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: update_alert_definition_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ALERT_DEF_UPDATED,
        provider_account="",
        connection_ref="",
        metadata={
            "alert_def_id": alert_id,
            "updated_fields": list(k for k in ("enabled", "threshold") if k in body),
        },
    )

    return JSONResponse(updated_record)

async def _delete_alert_definition(request: Request) -> Response:
    """DELETE /api/alert-definitions/{id} -- soft-delete (sets enabled=false).

    Design decision (T5.5): soft-delete via enabled=false.
    Rationale: alert_firings rows reference alert_definitions via FK. A hard
    delete would violate the FK constraint unless firings are also deleted.
    Soft-delete preserves audit history and firing provenance (AD-9) while
    effectively disabling the alert. Hard delete is not used here.

    Response (200):
        {"id": ..., "deleted": true}

    Error responses:
        401 -- unauthorized
        404 -- definition not found
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    alert_id = request.path_params.get("id", "")
    if not alert_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Alert definition id is required"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.alert_definitions
                    SET enabled = FALSE, updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (alert_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {
                            "code": "not_found",
                            "message": f"Alert definition '{alert_id}' not found",
                        },
                        status_code=404,
                    )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: delete_alert_definition_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ALERT_DEF_DELETED,
        provider_account="",
        connection_ref="",
        metadata={"alert_def_id": alert_id},
    )

    return JSONResponse({"id": alert_id, "deleted": True})

def _alert_definition_row(cur, row) -> dict:
    """One alert-definition row, as all three of its endpoints render it.

    AI-219: list, create and update each held their own copy of
    ``if col in ("created_at", "updated_at")`` -- three lists to keep in step
    with one table, which is two more than anybody was going to remember. The
    date rule now comes from `row_json`; what stays here is the part that really
    is specific to this row: `NUMERIC` columns become `float`, because
    `Decimal` is not JSON and rendering it as a string would change a payload the
    console arithmetic depends on.
    """
    record = row_to_json(cur.description, row)
    for column in ("threshold", "last_firing_value"):
        if record.get(column) is not None:
            record[column] = float(record[column])
    return record

def _get_valid_alert_metrics() -> frozenset[str]:
    """Return all valid metric names for alert definitions."""
    from core.business_alerts import _get_semantic_metrics  # noqa: PLC0415

    return _ALERT_ADDITIVE_METRICS | _get_semantic_metrics()


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

ALERT_DEFINITIONS_ROUTES_1 = [
    # Story 5.3 (AC5): alert-definitions CRUD
    # IMPORTANT: /api/alert-definitions must precede /api/alert-definitions/{id}
    # so Starlette does not absorb the list/create routes as ID parameters.
    Route(
        "/api/alert-definitions",
        endpoint=_list_alert_definitions,
        methods=["GET"],
    ),
    Route(
        "/api/alert-definitions",
        endpoint=_create_alert_definition,
        methods=["POST"],
    ),
    Route(
        "/api/alert-definitions/{id}",
        endpoint=_update_alert_definition,
        methods=["PATCH"],
    ),
    Route(
        "/api/alert-definitions/{id}",
        endpoint=_delete_alert_definition,
        methods=["DELETE"],
    ),
]
