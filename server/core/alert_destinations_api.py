"""Par ou une alerte sort, et la preuve qu elle peut sortir.

AD-43, 2026-08-13. Cinq routes, dont le test d envoi -- une destination qui
n a jamais rien recu est une destination dont personne ne sait si elle marche.
Le pendant de `alert_definitions_api` : deux objets, deux modules.
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

ACTION_ALERT_DEST_CREATED = declare_action("alert_destination.created")

ACTION_ALERT_DEST_DELETED = declare_action("alert_destination.deleted")

ACTION_ALERT_DEST_TESTED = declare_action("alert_destination.tested")

ACTION_ALERT_DEST_UPDATED = declare_action("alert_destination.updated")

async def _list_alert_destinations(request: Request) -> Response:
    """GET /api/alert-destinations?project_id=<id>.

    Answers the whole screen in one call: the destinations (masked), the number
    of firings the project wrote in the last 24 hours -- which is what the empty
    state states instead of a zero -- the types a rule may name, and the four
    infrastructure signals that CANNOT be routed because they carry no project.
    """
    scope, _identity, refusal = await _alert_destination_scope(
        request, "view", "list_alert_destinations"
    )
    if refusal is not None:
        return refusal
    project_id, _ = scope

    # A read that could hand back a secret is not a read with a flag; it is a
    # different route, and this one refuses to become it.
    if request.query_params.get("include_secret"):
        return JSONResponse(
            {
                "code": "secret_is_write_only",
                "message": (
                    "A destination secret is written and never read back. "
                    "Replace it by sending a new one."
                ),
            },
            status_code=400,
        )

    try:
        from core import alert_destinations  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            destinations = alert_destinations.list_destinations(conn, project_id)
            firings_last_24h = alert_destinations.firing_count_since(conn, project_id, 24)
    except Exception as exc:
        logger.error("admin_api: list_alert_destinations_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    from core import alert_destinations as _destinations  # noqa: PLC0415

    return JSONResponse(
        {
            "destinations": destinations,
            "firings_last_24h": firings_last_24h,
            "routable_alert_types": list(_destinations.ROUTABLE_ALERT_TYPES),
            "console_only_signals": list(_destinations.CONSOLE_ONLY_SIGNALS),
            "kinds": list(_destinations.KINDS),
        }
    )

async def _create_alert_destination(request: Request) -> Response:
    """POST /api/alert-destinations -- create one destination for a project."""
    scope, identity, refusal = await _alert_destination_scope(
        request, "edit", "create_alert_destination"
    )
    if refusal is not None:
        return refusal
    project_id, body = scope

    try:
        from core import alert_destinations  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            record = alert_destinations.create_destination(
                conn,
                project_id=project_id,
                kind=body.get("kind"),
                label=body.get("label"),
                target=body.get("target"),
                alert_types=body.get("alert_types"),
                secret=body.get("secret"),
                created_by=identity,
            )
            conn.commit()
    except Exception as exc:
        from core.alert_destinations import AlertDestinationError  # noqa: PLC0415

        if isinstance(exc, AlertDestinationError):
            return _alert_destination_refusal(exc)
        logger.error("admin_api: create_alert_destination_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=identity,
        action=ACTION_ALERT_DEST_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "destination_id": record["id"],
            "project_id": project_id,
            "kind": record["kind"],
            # The masked target, never the address or the URL: an audit row is
            # read by more people than the screen is.
            "target_masked": record["target_masked"],
            "alert_types": record["alert_types"],
        },
    )
    return JSONResponse(record, status_code=201)

async def _test_alert_destination(request: Request) -> Response:
    """POST /api/alert-destinations/{id}/test -- the real transport, no firing.

    It depends on neither the scheduler nor `app.alert_firings`. A transport the
    deployment does not carry answers `transport_unavailable` and NAMES the
    variable that is missing -- today `SMTP_HOST`, which
    `infra/scripts/deploy.sh:148` does not push.
    """
    scope, identity, refusal = await _alert_destination_scope(
        request, "edit", "test_alert_destination"
    )
    if refusal is not None:
        return refusal
    project_id, _ = scope
    destination_id = request.path_params.get("id", "")

    try:
        from core import alert_destinations  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            verdict = alert_destinations.send_test(conn, destination_id, project_id)
    except Exception as exc:
        logger.error("admin_api: test_alert_destination_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    if verdict.get("code") == "not_found":
        return JSONResponse(
            {"code": "not_found", "message": f"Alert destination '{destination_id}' not found"},
            status_code=404,
        )

    write_audit_row(
        identity=identity,
        action=ACTION_ALERT_DEST_TESTED,
        provider_account="",
        connection_ref="",
        metadata={
            "destination_id": destination_id,
            "project_id": project_id,
            "outcome": verdict.get("code"),
        },
    )
    # 200 with a verdict, not a 5xx: "the transport is not deployed" is an
    # ANSWER about this deployment, not a failure of the request.
    return JSONResponse(verdict)

async def _update_alert_destination(request: Request) -> Response:
    """PATCH /api/alert-destinations/{id} -- label, target, rule, enabled, secret."""
    scope, identity, refusal = await _alert_destination_scope(
        request, "edit", "update_alert_destination"
    )
    if refusal is not None:
        return refusal
    project_id, body = scope
    destination_id = request.path_params.get("id", "")

    fields = {
        key: body[key]
        for key in ("label", "target", "alert_types", "enabled", "secret")
        if key in body
    }
    try:
        from core import alert_destinations  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            record = alert_destinations.update_destination(
                conn, destination_id, project_id, fields=fields
            )
            conn.commit()
    except Exception as exc:
        from core.alert_destinations import AlertDestinationError  # noqa: PLC0415

        if isinstance(exc, AlertDestinationError):
            return _alert_destination_refusal(exc)
        logger.error("admin_api: update_alert_destination_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    if record is None:
        return JSONResponse(
            {"code": "not_found", "message": f"Alert destination '{destination_id}' not found"},
            status_code=404,
        )

    write_audit_row(
        identity=identity,
        action=ACTION_ALERT_DEST_UPDATED,
        provider_account="",
        connection_ref="",
        metadata={
            "destination_id": destination_id,
            "project_id": project_id,
            # WHICH fields moved, never their values -- one of them is a secret.
            "updated_fields": sorted(fields),
        },
    )
    return JSONResponse(record)

async def _delete_alert_destination(request: Request) -> Response:
    """DELETE /api/alert-destinations/{id}?project_id=<id> -- hard delete.

    Unlike an alert definition, nothing references a destination the way a
    firing references its definition: its deliveries cascade with it, and a
    disabled row left behind would keep answering the confirmation's count.
    """
    scope, identity, refusal = await _alert_destination_scope(
        request, "edit", "delete_alert_destination"
    )
    if refusal is not None:
        return refusal
    project_id, _ = scope
    destination_id = request.path_params.get("id", "")

    try:
        from core import alert_destinations  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            record = alert_destinations.delete_destination(conn, destination_id, project_id)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: delete_alert_destination_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    if record is None:
        return JSONResponse(
            {"code": "not_found", "message": f"Alert destination '{destination_id}' not found"},
            status_code=404,
        )

    write_audit_row(
        identity=identity,
        action=ACTION_ALERT_DEST_DELETED,
        provider_account="",
        connection_ref="",
        metadata={
            "destination_id": destination_id,
            "project_id": project_id,
            "label": record["label"],
        },
    )
    return JSONResponse(record)

def _alert_destination_refusal(exc) -> JSONResponse:
    """One refusal vocabulary, shared by create, update and the domain module."""
    status = 404 if exc.code == "not_found" else 422
    if exc.code in ("no_update_fields",):
        status = 400
    return JSONResponse({"code": exc.code, "message": exc.message}, status_code=status)

async def _alert_destination_scope(request: Request, capability: str, surface: str):
    """(project_id, identity, refusal) -- the same gate every handler here uses.

    The project is read from the query string for the reads and from the body
    for the writes, and is authorized BEFORE anything else is validated: a 422
    on a project the caller may not see would confirm its existence.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return None, None, JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    project_id = (request.query_params.get("project_id") or "").strip()
    body: dict = {}
    if request.method in ("POST", "PATCH"):
        try:
            raw = await request.body()
            body = json.loads(raw) if raw.strip() else {}
        except Exception as exc:  # noqa: BLE001
            return None, None, JSONResponse(
                {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
                status_code=400,
            )
        project_id = project_id or str(body.get("project_id") or "").strip()
    if not project_id:
        return None, None, JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )
    denied = _refuse_unless_project_allowed(identity, project_id, capability, surface)
    if denied is not None:
        return None, None, denied
    return (project_id, body), identity or "anonymous", None


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

ALERT_DESTINATIONS_ROUTES_1 = [
    # Story 59.6: alert-destinations CRUD + test send. Same ordering rule as
    # above -- the static paths precede the parametrized ones.
    Route(
        "/api/alert-destinations",
        endpoint=_list_alert_destinations,
        methods=["GET"],
    ),
    Route(
        "/api/alert-destinations",
        endpoint=_create_alert_destination,
        methods=["POST"],
    ),
    Route(
        "/api/alert-destinations/{id}/test",
        endpoint=_test_alert_destination,
        methods=["POST"],
    ),
    Route(
        "/api/alert-destinations/{id}",
        endpoint=_update_alert_destination,
        methods=["PATCH"],
    ),
    Route(
        "/api/alert-destinations/{id}",
        endpoint=_delete_alert_destination,
        methods=["DELETE"],
    ),
]
