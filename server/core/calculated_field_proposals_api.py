"""Story 75-1 -- the REST door of the calculated-field promotion rail.

THIN BY CONSTRUCTION, like `answerable_topics_api`: every handler translates
HTTP into `core.calculated_field_proposals` and nothing else. No SQL, no
validation and no lifecycle rule lives here, so the console (75-2) and any
future adapter reach the same rail through the same rules -- and the MCP door
next to it cannot drift into a second product.

THE RANKS, AND WHERE THEY COME FROM. Filing a proposal is guarded at `member`,
resolving one at `member` too, and reading the queue at `viewer`.

  * Reading is `viewer` for the reason `context_api._list_review_requests`
    already states: a queue nobody may read is a queue nobody works.
  * Filing is `member` and NOT `view`, which is where this rail parts company
    with `context_api._request_review`. That route's reason is written at its
    line -- "any consumer of the knowledge can flag a node" -- and a remark
    proposes nothing typed. A calculated-field proposal carries a payload whose
    ACCEPTANCE opens a change-set on the Project's semantic model. A `viewer`
    who could fill that queue could make a Project's governance queue say
    whatever they wanted. The gesture is not the same gesture, so the rank is
    not the same rank.
  * Resolving is `member` because it opens the change-set.

NON-DISCLOSURE IS THE DEFAULT. Foreign, denied and absent all answer the same
404 envelope, and the denial answers before any work is done.

NOT REGISTERED HERE. `calculated_field_proposal_routes` is spliced into
`admin_api.router` at startup, exactly as `answerable_topic_routes` is; this
module owns no import of the app.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.calculated_field_proposals import (
    CalculatedFieldProposalError,
    ProposalAlreadyResolvedError,
    list_open,
    project_org_id,
    propose,
    resolve,
)

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/calculated-field-proposals"

#: One envelope for foreign, denied and absent.
_NOT_FOUND = {"code": "not_found", "message": "Not found"}


async def _authorize(request: Request, role: str = "viewer"):
    """Return (identity, org_id, project_id) or a Response. Denial answers first."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, 401
        )
    project_id = request.path_params["project_id"]
    with get_connection() as conn:
        denied = _require_datastream_role(project_id, identity, role, conn)
        if denied is not None:
            return denied
        # THE SERVICE READS, NOT THIS MODULE. Criterion 4 of
        # `module-boundaries.md`, measured by `scripts/api_sql_census.py --gate`:
        # a route module parses, authorizes and calls a service. One `SELECT`
        # here is how a route module becomes a second data layer.
        org_id = project_org_id(conn, project_id)
    if org_id is None:
        return JSONResponse(_NOT_FOUND, 404)
    return str(identity), org_id, str(project_id)


def _proposal_connection(identity: str):
    """The request-scoped connection of `core.db`, exactly as the MCP door takes it.

    THE SEAM, NOT A COPY OF IT. `core.db.request_connection` is the repository's
    one acquisition of an armed connection (story 21.6): it opens, it translates
    the caller's subject to its canonical identity, and it arms the Epic-36 floor
    for the SESSION rather than for one transaction. The three differences all
    bite this rail:

      * THE IDENTITY. `_check_auth` hands back whatever the token carried; the
        floor compares `toorow.identity` against `app.org_members.identity`,
        which holds the canonical `person_<ULID>`. An OIDC subject matches no
        membership row, so a member reads an empty database through their own
        console and every route here answers `not_found` about their own
        Project. `request_connection` translates; `arm_access_floor` does not.
      * THE LIFETIME. `arm_access_floor` uses `set_config(..., true)`, which
        reverts at the end of the transaction it ran in. `_create` and `_resolve`
        both `conn.commit()` mid-handler, so the floor is down for anything
        added after that commit -- the exact shape `install_access_context` was
        written for, and the reason it commits a SESSION setting instead.
      * THE POOLER. `request_connection` reads the context back after its own
        commit and REFUSES a connection that lost it (a transaction-mode pooler
        hands the backend to somebody else). A helper that arms a connection it
        borrowed cannot make that check.

    `propose_calculated_field` (`calculated_field_proposals_mcp.py`) already
    takes this seam. Two doors on one rail that armed the floor two ways would be
    two answers to one question.
    """
    from core.db import request_connection  # noqa: PLC0415

    return request_connection(identity)


#: The JSON type each key of a create body carries when it is present, and the
#: gesture that repairs a value of another type.
#:
#: A BODY OF THE WRONG SHAPE IS THE CALLER'S DEFECT, NOT THE SERVER'S. Measured:
#: `{"description": {...}}` reached `propose`, met `(description or "").strip()`
#: and raised `AttributeError` -- which the blanket handler below turned into
#: `500 db_error`, "The promotion queue is unavailable." The queue was perfectly
#: available; the sentence sent a person to look at the platform for a typo in
#: their own request, and the 500 says a defect nobody can repair. Refusing by
#: name before the service is called says the gesture instead.
_BODY_SHAPE: tuple[tuple[str, type, str], ...] = (
    ("name", str, "Send `name` as text: the canonical name the field would be known by."),
    ("description", str, "Send `description` as text, or leave it out."),
    (
        "expression",
        dict,
        "Send `expression` as the typed expression tree object -- never a list, "
        "never a string of SQL.",
    ),
    (
        "provenance",
        dict,
        "Send `provenance` as an object naming the exploration this calculation "
        'came from, for example {"result_id": "qr_..."}.',
    ),
)


def _shape_refusal(body: dict) -> dict | None:
    """The named refusal a create body of the wrong SHAPE earns, or None."""
    for key, kind, gesture in _BODY_SHAPE:
        value = body.get(key)
        # Absent and null are not shape defects: the service decides whether the
        # key was required, once, for every door on this rail.
        if value is None:
            continue
        if not isinstance(value, kind):
            return {"code": "invalid_body", "message": gesture}
    return None


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


async def _create(request: Request) -> Response:
    """POST {base} -- offer a calculation found while exploring."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    # BEFORE the service call, and before a connection is opened: a value the
    # door cannot translate is refused by name, never by a 500 the caller reads
    # as an outage.
    shape = _shape_refusal(body)
    if shape is not None:
        return JSONResponse(shape, 400)

    try:
        with _proposal_connection(identity) as conn:
            filed = propose(
                conn,
                org_id=org_id,
                project_id=project_id,
                name=str(body.get("name") or ""),
                expression=body.get("expression"),
                provenance=body.get("provenance"),
                # THE ORIGIN IS THE DOOR'S, NEVER THE CALLER'S. A body that
                # could say `agent` would let a person file a machine's remark
                # -- which is the exact confusion `origin` exists to prevent.
                origin="human",
                requested_by=identity,
                description=body.get("description"),
            )
            conn.commit()
    except CalculatedFieldProposalError as exc:
        return JSONResponse(exc.as_dict(), 422)
    except Exception as exc:  # noqa: BLE001
        logger.warning("calculated_field_proposals_api: create failed: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "The promotion queue is unavailable."}, 500
        )
    return JSONResponse({"proposal": _serializable(filed)}, 201)


async def _list(request: Request) -> Response:
    """GET {base}?status=open -- the queue.

    `status` accepts `open` and nothing else today, and says so by name rather
    than silently answering the open queue for a word it did not understand.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    wanted = (request.query_params.get("status") or "open").strip()
    if wanted != "open":
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "status=open is the only queue this door serves; a "
                           "resolved proposal is read by its id.",
            },
            422,
        )

    try:
        with _proposal_connection(identity) as conn:
            rows = list_open(conn, org_id=org_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("calculated_field_proposals_api: list failed: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "The promotion queue is unavailable."}, 500
        )
    return JSONResponse(
        {
            "project_id": project_id,
            "status": "open",
            "proposals": [_serializable(row) for row in rows],
            "proposals_total": len(rows),
        }
    )


async def _resolve(request: Request) -> Response:
    """POST {base}/{id}/resolve -- accept (prepare a change-set) or decline."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, _project_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with _proposal_connection(identity) as conn:
            resolved = resolve(
                conn,
                proposal_id=request.path_params["proposal_id"],
                org_id=org_id,
                status=str(body.get("status") or ""),
                resolved_by=identity,
            )
            if resolved is None:
                return JSONResponse(_NOT_FOUND, 404)
            conn.commit()
    except ProposalAlreadyResolvedError as exc:
        # An id that exists and a state that refuses is a CONFLICT, never an
        # absence: answering 404 here would send a person looking for a row
        # that is right there.
        return JSONResponse(exc.as_dict(), 409)
    except CalculatedFieldProposalError as exc:
        return JSONResponse(exc.as_dict(), 422)
    except Exception as exc:  # noqa: BLE001
        logger.warning("calculated_field_proposals_api: resolve failed: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "The promotion queue is unavailable."}, 500
        )
    return JSONResponse({"proposal": _serializable(resolved)})


def _serializable(row: dict) -> dict:
    """Timestamps as ISO 8601 strings; everything else already is JSON."""
    out = dict(row)
    for key in ("created_at", "resolved_at"):
        value = out.get(key)
        if value is not None and hasattr(value, "isoformat"):
            out[key] = value.isoformat()
    return out


calculated_field_proposal_routes = [
    Route(_BASE, endpoint=_list, methods=["GET"]),
    Route(_BASE, endpoint=_create, methods=["POST"]),
    Route(
        f"{_BASE}/{{proposal_id}}/resolve", endpoint=_resolve, methods=["POST"]
    ),
]
