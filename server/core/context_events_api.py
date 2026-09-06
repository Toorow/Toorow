"""Ce qui s est passe ce jour-la, et que la donnee seule n explique pas.

AD-43, 2026-08-13. Deux routes seulement, mais 298 lignes : la creation valide
un type, une date, une portee et une etendue, et la lecture les rend dans le
vocabulaire de l ecran. Le service vit dans `core/context_events.py`.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import (
    ACTION_CONTEXT_EVENT_CREATED,
    insert_audit_row,
)

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _strict_project_capability_allowed(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _strict_project_capability_allowed as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

# ISO-8601 date pattern (reused from existing _ISO_DATE_RE above)
_CONTEXT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

async def _create_context_event(request: Request) -> Response:
    """POST /api/context-events -- create a context event (admin console).

    Query: project_id (required, authoritative route scope).
    Request body (JSON):
        {"project_id": str?, "event_date": str, "type": str, "label": str,
         "description": str?, "metric": str?}; body project_id, when present,
        must match the query.

    ``metric`` is optional and OPTIONAL ON PURPOSE (migration 322): an outage or
    a holiday is about every metric, and an absent metric keeps the event
    admissible under every claim. Naming one narrows where the event is ever
    offered as context. A metric the Project does not govern is refused with the
    gesture that repairs, never stored as written.

    Response (201):
        {"id", "project_id", "event_date", "type", "label", "metric",
         "created_at"}

    Response (422):
        {"error": "label is too long (max 120 characters)"} on validation failure,
        or {"code": "metric_not_governed", ...} on an unknown metric.

    HG-2: this endpoint is for the admin console only. The widget uses the
    add_context_event MCP tool (callServerTool). Do NOT call this from the widget.
    """
    from ulid import ULID  # noqa: PLC0415

    from core.context_events import (  # noqa: PLC0415
        ContextEventRefusal,
        assert_metric_is_governed,
        normalize_metric,
    )

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes)
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

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {
                "code": "missing_project",
                "message": "project_id query parameter is required",
            },
            status_code=422,
        )
    body_project_value = body.get("project_id")
    if body_project_value is not None and not isinstance(body_project_value, str):
        return JSONResponse(
            {"code": "invalid_body", "message": "body project_id must be a string"},
            status_code=422,
        )
    body_project_id = (body_project_value or "").strip()
    if body_project_id and body_project_id != project_id:
        return JSONResponse(
            {
                "code": "project_scope_mismatch",
                "message": "body project_id must match URL project_id",
            },
            status_code=422,
        )

    for field_name in ("event_date", "type", "label", "description", "metric"):
        field_value = body.get(field_name)
        if field_value is not None and not isinstance(field_value, str):
            return JSONResponse(
                {
                    "code": "invalid_field",
                    "message": f"{field_name} must be a string",
                },
                status_code=422,
            )

    event_date = (body.get("event_date") or "").strip()
    type_ = (body.get("type") or "").strip()
    label = (body.get("label") or "").strip()
    description = (body.get("description") or "").strip() or None
    # Shape here, catalogue below on the connection that already carries the
    # caller's access context -- the same split the service makes.
    metric = normalize_metric(body.get("metric"))

    if not event_date:
        return JSONResponse(
            {"code": "missing_field", "message": "event_date is required"},
            status_code=400,
        )
    if not type_:
        return JSONResponse(
            {"code": "missing_field", "message": "type is required"},
            status_code=400,
        )
    if not label:
        return JSONResponse(
            {"code": "missing_field", "message": "label is required"},
            status_code=400,
        )

    validation_error = _validate_context_event_input(label, event_date)
    if validation_error:
        return JSONResponse({"error": validation_error}, status_code=422)

    evt_id = f"evt_{ULID()}"
    created_by = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _context_event_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _context_event_not_found()
            assert_metric_is_governed(conn, metric, project_id)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.context_events
                        (id, project_id, event_date, type, label, description,
                         created_by, metric)
                    VALUES (%s, %s, %s::date, %s, %s, %s, %s, %s)
                    RETURNING id, project_id, event_date, type, label, metric,
                              created_at
                    """,
                    (
                        evt_id, project_id, event_date, type_, label, description,
                        created_by, metric,
                    ),
                )
                row = cur.fetchone()
                if row is None:  # pragma: no cover
                    raise RuntimeError("INSERT RETURNING returned no row")
                cols = [desc[0] for desc in cur.description]
                created_record: dict = {}
                for col, val in zip(cols, row):
                    if col == "created_at" and val is not None:
                        created_record[col] = val.isoformat()
                    elif col == "event_date" and val is not None:
                        created_record[col] = str(val)
                    else:
                        created_record[col] = val
            insert_audit_row(
                conn,
                identity=created_by,
                action=ACTION_CONTEXT_EVENT_CREATED,
                provider_account="",
                connection_ref="",
                metadata={
                    "event_id": evt_id,
                    "project_id": project_id,
                    "type": type_,
                    "label": label,
                },
            )
            conn.commit()
    except ContextEventRefusal as refusal:
        # `metric_not_governed` is a refusal a person repairs, not a server
        # failure. Dressing it as `db_error` would hide the only sentence that
        # says what to do about it.
        return _context_event_refusal(refusal)
    except Exception as exc:
        logger.error("admin_api: context_event_insert_error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Context event could not be saved"},
            status_code=500,
        )


    return JSONResponse(created_record, status_code=201)

async def _list_context_events(request: Request) -> Response:
    """GET /api/context-events -- list context events for a project.

    Query params:
        project_id       (required)
        start            (optional ISO date, inclusive)
        end              (optional ISO date, inclusive)
        include_retired  (optional, "true" to also list withdrawn events)

    Response (200):
        {"events": [{id, project_id, event_date, type, label, description,
                     created_by, created_at, source, retired_at, ...}, ...]}
        Order: event_date DESC, created_at DESC. No pagination (admin console view).

    Response (400): missing project_id.

    Migration 286: a RETIRED event is withdrawn from this list by default -- it no
    longer describes what the project observes. It is NOT silently dropped: the
    caller can ask for it, and it then arrives carrying its retirement, so a list
    that claims to be complete can be obtained by asking for one.

    Each event carries `capabilities.can_correct`: whether the server will accept
    a correction or a retirement of THIS row. A connector-emitted row and a
    retired one will both be refused, and a screen that renders a button the
    server refuses is a screen that teaches people not to trust its buttons.
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

    start = request.query_params.get("start") or None
    end = request.query_params.get("end") or None
    include_retired = (request.query_params.get("include_retired") or "").lower() in (
        "1", "true", "yes",
    )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _context_event_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _context_event_not_found()
            can_write = _context_event_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            )
            with conn.cursor() as cur:
                params: list = [project_id]
                # Migration 328: how many earlier wordings this annotation has.
                # A scalar subquery rather than a second round trip -- a screen
                # that must ask N times whether a row has a history is a screen
                # that will stop asking.
                sql = """
                    SELECT e.id, e.project_id, e.event_date, e.type, e.label,
                           e.description, e.created_by, e.created_at, e.source,
                           e.metric, e.retired_at, e.retired_by, e.retired_reason,
                           (SELECT count(*)
                              FROM app.context_event_revisions r
                             WHERE r.event_id = e.id) AS revision_count
                    FROM app.context_events e
                    WHERE project_id = %s
                """
                if start is not None:
                    sql += " AND event_date >= %s::date"
                    params.append(start)
                if end is not None:
                    sql += " AND event_date <= %s::date"
                    params.append(end)
                if not include_retired:
                    sql += " AND retired_at IS NULL"
                sql += " ORDER BY event_date DESC, created_at DESC"
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                events: list[dict] = []
                for row in cur.fetchall():
                    record: dict = {}
                    for col, val in zip(cols, row):
                        if col in ("created_at", "retired_at") and val is not None:
                            record[col] = val.isoformat()
                        elif col == "event_date" and val is not None:
                            record[col] = str(val)
                        elif col == "revision_count":
                            record[col] = int(val or 0)
                        else:
                            record[col] = val
                    # An absent `source` is a legacy row, which migration 055
                    # defines as manual -- the same reading `persist_context_event`
                    # applies. It is never a row of unknown origin.
                    is_manual = (record.get("source") or "manual") == "manual"
                    record["capabilities"] = {
                        "can_write": can_write,
                        "can_correct": can_write and is_manual
                        and record.get("retired_at") is None,
                        # Migration 328: the server answers a revision history
                        # for any row it lists -- an annotation never corrected
                        # answers with an empty one, which is a fact and not an
                        # absence of feature. `revision_count` above says whether
                        # there is anything to open.
                        "version_history": True,
                        "usage": False,
                    }
                    events.append(record)
    except Exception as exc:
        logger.error("admin_api: list_context_events db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse(
        {
            "events": events,
            "capabilities": {
                "can_write": can_write,
                "version_history": True,
                "usage": False,
            },
        }
    )

#: The fields a PATCH body may carry, and the type each must be. `value` is the
#: MMM regressor magnitude (numeric) -- everything else is text. A field ABSENT
#: from the body is untouched; a field present and `null` is CLEARED, which is why
#: the service takes a sentinel rather than reading `None` as "not given".
_CORRECTABLE_FIELDS: dict[str, tuple[tuple, str]] = {
    "event_date": ((str,), "string"),
    "type": ((str,), "string"),
    "label": ((str,), "string"),
    "description": ((str, type(None)), "string"),
    "platform": ((str, type(None)), "string"),
    "value": ((int, float, type(None)), "number"),
    "entity_key": ((str, type(None)), "string"),
    "entity_kind": ((str, type(None)), "string"),
    # Migration 322. Nullable like `platform`: clearing a wrongly narrowed event
    # back to "about every metric" is exactly the repair someone needs, and it is
    # unreachable if `null` cannot be written.
    "metric": ((str, type(None)), "string"),
}

#: The three fields that must not arrive blank. A blank label or type is not a
#: correction, and the create door refuses them the same way at the same layer
#: (`_create_context_event` above) -- one rule, one place per door.
_NON_BLANK_FIELDS = ("event_date", "type", "label")


async def _read_json_object(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """The JSON body as an object, or the refusal to return instead."""
    try:
        body = json.loads(await request.body())
    except Exception as exc:
        return None, JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )
    if not isinstance(body, dict):
        return None, JSONResponse(
            {"code": "invalid_body", "message": "JSON body must be an object"},
            status_code=422,
        )
    return body, None


def _context_event_refusal(exc) -> JSONResponse:
    """A `ContextEventRefusal`, rendered for HTTP.

    `not_found` goes through `_context_event_not_found` rather than through the
    generic branch, so an event of another project answers with the SAME envelope
    an unreadable project answers with -- an id that exists elsewhere is not
    something a caller may learn by trying.
    """
    if exc.code == "not_found":
        return _context_event_not_found()
    return JSONResponse(exc.payload, status_code=exc.status)


def _context_event_invalid_input(exc) -> JSONResponse:
    """A shared validator's refusal, rendered for HTTP.

    `validate_event_input` / `validate_event_type` raise `ToolError` carrying a
    JSON `{code, message}` because the MCP door is their first caller. Re-reading
    that payload here is what lets ONE validator serve both doors -- writing a
    second one for HTTP is exactly the drift `_validate_context_event_input`
    already documents ("Both must stay in sync").
    """
    try:
        payload = json.loads(str(exc))
    except Exception:  # noqa: BLE001 -- a validator that stopped carrying JSON
        payload = {"code": "invalid_input", "message": str(exc)}
    return JSONResponse(payload, status_code=422)


async def _update_context_event(request: Request) -> Response:
    """PATCH /api/context-events/{event_id} -- correct a manual context event.

    Query: project_id (required, authoritative route scope).
    Body: any of event_date, type, label, description, platform, value,
          entity_key, entity_kind, metric. A field ABSENT is untouched; a
          nullable field present and null is cleared.

    Response (200): the corrected event.
    Response (404): unknown project, unknown event, or an event of another
                    project -- one envelope, so none of them is disclosed.
    Response (409): the event is connector-emitted, or already retired. The
                    message names the gesture that repairs.
    Response (422): `metric_not_governed` -- the metric named is not one this
                    Project governs.
    """
    from core.context_events import (  # noqa: PLC0415
        UNSET,
        ContextEventRefusal,
        update_manual_event,
    )

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_project", "message": "project_id query parameter is required"},
            status_code=422,
        )

    body, refusal = await _read_json_object(request)
    if refusal is not None:
        return refusal

    corrections = {}
    for field_name, (allowed, kind) in _CORRECTABLE_FIELDS.items():
        if field_name not in body:
            continue
        field_value = body[field_name]
        # `bool` is a subclass of `int`, so `true` would slip into `value` and be
        # stored as 1 -- a magnitude nobody wrote.
        if not isinstance(field_value, allowed) or isinstance(field_value, bool):
            return JSONResponse(
                {
                    "code": "invalid_field",
                    "message": f"{field_name} must be a {kind}",
                },
                status_code=422,
            )
        if field_name in _NON_BLANK_FIELDS and not str(field_value).strip():
            return JSONResponse(
                {"code": "missing_field", "message": f"{field_name} cannot be empty"},
                status_code=422,
            )
        corrections[field_name] = (
            field_value.strip() if isinstance(field_value, str) else field_value
        )

    if not corrections:
        return JSONResponse(
            {"code": "no_change", "message": "Name at least one field to correct."},
            status_code=422,
        )

    event_id = request.path_params["event_id"]
    try:
        from core.db import request_connection  # noqa: PLC0415

        # THE ARMED CONNECTION, for the two NEW write routes only. `db.py:310-328`
        # states why the acquisition is the place, and the 2026-08-17 amendment of
        # `context-hub.md` states that the read which FOLLOWS an access decision
        # runs on the connection carrying it. The two legacy routes above keep
        # `get_connection`: changing them is a separate, measurable act, not a
        # drive-by inside this one.
        with request_connection(identity or "anonymous") as conn:
            if not _context_event_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _context_event_not_found()
            corrected = update_manual_event(
                project_id=project_id,
                event_id=event_id,
                updated_by=identity or "anonymous",
                conn=conn,
                **{key: corrections.get(key, UNSET) for key in _CORRECTABLE_FIELDS},
            )
    except ContextEventRefusal as exc:
        return _context_event_refusal(exc)
    except Exception as exc:
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        if isinstance(exc, ToolError):
            return _context_event_invalid_input(exc)
        logger.error("admin_api: context_event_update_error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Context event could not be corrected"},
            status_code=500,
        )

    return JSONResponse(corrected, status_code=200)


async def _retire_context_event(request: Request) -> Response:
    """POST /api/context-events/{event_id}/retire -- withdraw a manual event.

    NOT a delete, and the route is named for what it does: the row stays for audit
    and stops being served (migration 286). A `DELETE` verb here would promise a
    destruction the server does not perform.

    Query: project_id (required). Body: {"reason": str} -- required, non-blank.
    """
    from core.context_events import (  # noqa: PLC0415
        ContextEventRefusal,
        retire_manual_event,
    )

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_project", "message": "project_id query parameter is required"},
            status_code=422,
        )

    body, refusal = await _read_json_object(request)
    if refusal is not None:
        return refusal

    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        return JSONResponse(
            {"code": "invalid_field", "message": "reason must be a string"},
            status_code=422,
        )

    event_id = request.path_params["event_id"]
    try:
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity or "anonymous") as conn:
            if not _context_event_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _context_event_not_found()
            retired = retire_manual_event(
                project_id=project_id,
                event_id=event_id,
                retired_by=identity or "anonymous",
                reason=reason or "",
                conn=conn,
            )
    except ContextEventRefusal as exc:
        return _context_event_refusal(exc)
    except Exception as exc:
        logger.error("admin_api: context_event_retire_error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Context event could not be retired"},
            status_code=500,
        )

    return JSONResponse(retired, status_code=200)


async def _list_context_event_revisions(request: Request) -> Response:
    """GET /api/context-events/{event_id}/revisions -- the earlier wordings.

    Query: project_id (required). Response (200):
        {"event": {id, label, retired_at, ...},
         "revisions": [{revision_no, superseded_at, corrected_by,
                        corrected_fields, event_date, type, label, description,
                        platform, value, metric, entity_key, entity_kind}, ...]}
        Oldest first, so a reader walks the annotation forward in time.

    Migration 328. The event is returned BESIDE its history for one reason: a
    history is a list of what the annotation NO LONGER says, and it is unreadable
    without what it says now. Two round trips would let a screen render the
    superseded wordings against a current one it fetched at another moment.

    Response (404): unknown project, unknown event, or an event of another
    project -- one envelope, so none of them is disclosed. `view` is enough: a
    withdrawal and a correction are both part of what the project observed, and
    reading history is not editing it.
    """
    from core.context_events import (  # noqa: PLC0415
        fetch_event_revisions,
        fetch_event_row,
    )

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_project", "message": "project_id query parameter is required"},
            status_code=422,
        )

    event_id = request.path_params["event_id"]
    try:
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity or "anonymous") as conn:
            if not _context_event_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _context_event_not_found()
            event = fetch_event_row(conn, project_id=project_id, event_id=event_id)
            if event is None:
                # An event of another project answers exactly as one that exists
                # nowhere -- the same non-disclosure the write doors already
                # serve.
                return _context_event_not_found()
            revisions = fetch_event_revisions(
                project_id=project_id, event_id=event_id, conn=conn
            )
    except Exception as exc:
        logger.error("admin_api: context_event_revisions_error: %s", type(exc).__name__)
        logger.debug("admin_api: context_event_revisions_error detail: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Context event history could not be read"},
            status_code=500,
        )

    return JSONResponse({"event": event, "revisions": revisions}, status_code=200)


def _context_event_not_found() -> JSONResponse:
    return JSONResponse(
        {"code": "not_found", "message": "Project not found"}, status_code=404
    )

def _context_event_project_allowed(
    conn, *, identity: str, project_id: str, minimum_capability: str
) -> bool:
    """Resolve Context Events access on the caller's evidence transaction."""
    try:
        return _strict_project_capability_allowed(
            conn,
            identity=identity,
            project_id=project_id,
            minimum_capability=minimum_capability,
            hold_access=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "admin_api: context_event_access_unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return False

def _validate_context_event_input(label: str, event_date: str) -> str | None:
    """Validate context event inputs. Returns error message string or None if valid.

    Mirrors _validate_event_input in main.py. Both must stay in sync.
    Returns a French error message on failure, None on success.
    """
    if len(label) > 120:
        return "label is too long (max 120 characters)"
    if not _CONTEXT_DATE_RE.match(event_date):
        return f"event_date invalide (format attendu YYYY-MM-DD) : {event_date!r}"
    try:
        date.fromisoformat(event_date)
    except ValueError:
        return f"event_date invalide (date calendrier impossible) : {event_date!r}"
    return None


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : memes
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

CONTEXT_EVENTS_ROUTES_1 = [
    # Story 4.3 (AC4): context events CRUD (admin console only — widget uses MCP tool)
    Route("/api/context-events", endpoint=_create_context_event, methods=["POST"]),
    Route("/api/context-events", endpoint=_list_context_events, methods=["GET"]),
    # Migration 286 / audit 2026-08-17 P1.2 -- the human correction path. APPENDED
    # rather than inserted: the two lines above keep the exact positions the
    # router dump recorded, so the proof stays a comparison and not a re-reading.
    # The `/retire` path is declared before the bare `{event_id}` one because a
    # reader should not have to know that a Starlette parameter never spans a
    # slash to be sure which route answers.
    Route(
        "/api/context-events/{event_id}/retire",
        endpoint=_retire_context_event,
        methods=["POST"],
    ),
    # Migration 328 -- the earlier wordings. Declared before the bare
    # `{event_id}` route for the same reason `/retire` is, and APPENDED after it
    # so the positions the router dump recorded do not move.
    Route(
        "/api/context-events/{event_id}/revisions",
        endpoint=_list_context_event_revisions,
        methods=["GET"],
    ),
    Route(
        "/api/context-events/{event_id}",
        endpoint=_update_context_event,
        methods=["PATCH"],
    ),
]
