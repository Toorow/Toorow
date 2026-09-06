"""toorow -- the read surface of the MDM canonical vocabulary (lot A1, issue #68).

Exports MDM_CANONICAL_FIELD_ROUTES: list[Route] -- a flat list `admin_api.py`
splices into its router at startup. Never imported by `admin_api` at module level
(no circular import), the pattern of `cleanup_rules_api.py` and
`value_mapping_api.py`.

  GET /api/projects/{project_id}/mdm/canonical-fields

WHY THIS DOOR HAD TO EXIST BEFORE ANYTHING ELSE. `app.mdm_canonical_fields` is
read by six production modules -- `datastream_field_mapping` validates every
mdm-bound binding against it, plus `datastream_projection`, `datamodel`,
`datastream_daily_breakdown_api`, `file_source_producer` and
`canonical_field_registry` itself -- and measured 2026-08-08 it held ZERO rows at
both scopes. One route listed it (`file_source_template_api.py:610`), at an
address that belongs to the file-source Template wizard and returns neither the
value type, nor the object kind, nor the scope. So the vocabulary the whole
product binds against had no address of its own, and no screen could name it.

WHAT THIS ROUTE ADDS OVER THAT ONE. The scope, derived from `project_id IS NULL`
rather than stored; the value type and the aggregation, which decide how a field
is read and summed; and the object kind, which says what a field qualifies. The
SQL is not written here: `canonical_field_registry.list_visible_canonical_fields`
owns it, and the Governance lens reads through the same function, so a field
visible on the screen is a field a binding is accepted against.

AN EMPTY PROJECT IS 200, NEVER 404. Zero rows is what every project has today,
and it is a fact about the vocabulary, not about the address. A 404 would tell a
person the page does not exist when what is true is that nobody has declared a
field yet -- so the answer carries an empty list AND the reason it is empty, and
the screen says which gesture fills it. A read that FAILED is 503 and carries no
list at all: "I could not read the vocabulary" and "the vocabulary is empty" are
different facts and only one of them invites a person to start declaring.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/mdm/canonical-fields"
_ONE = _BASE + "/{field_id}"

#: The empty answer's reason, in the vocabulary of the person reading it. It
#: names neither a table nor a deployment state, and it names the gesture: a
#: field is declared for the object a Datastream feeds.
_EMPTY_REASON = {
    "code": "no_canonical_field_declared",
    "message": (
        "No field has been declared yet, at either scope. The shared vocabulary "
        "is set up with your provider; the fields your own objects need are "
        "declared from the file source that feeds them, one object at a time."
    ),
}


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
    )


def _not_found() -> Response:
    return JSONResponse({"code": "not_found", "message": "Project not found."}, status_code=404)


def _guard(project_id: str, identity: str) -> tuple[str | None, Response | None]:
    """Resolve the org of *project_id* and verify the caller may read it.

    A project the guarded org does not own answers 404 and never 403, so a
    non-member does not learn the Project exists -- lesson F-3 of story 27.2,
    stated in `dimension_lineage_api.py:14-19` and applied identically by
    `cleanup_rules_api._guard`.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import identity_has_org_access  # noqa: PLC0415

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
                row = cur.fetchone()
            if not row or not row[0]:
                return None, _not_found()
            org_id = str(row[0])
            allowed = identity_has_org_access(org_id, identity, conn)
    except Exception as exc:  # noqa: BLE001
        logger.error("mdm_canonical_fields_api: guard failed project=%s: %s", project_id, exc)
        return None, JSONResponse(
            {"code": "server_error", "message": "Server error."}, status_code=500
        )

    if not allowed:
        return None, _not_found()
    return org_id, None


async def _list_canonical_fields(request: Request) -> Response:
    """GET {base} -- every canonical field visible to this project, both scopes."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    from core.canonical_field_registry import list_visible_canonical_fields  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            rows = list_visible_canonical_fields(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable vocabulary is not an empty one
        logger.error("mdm_canonical_fields_api: list failed project=%s: %s", project_id, exc)
        return JSONResponse(
            {
                "code": "canonical_fields_unavailable",
                "message": (
                    "The canonical vocabulary could not be read, so which fields "
                    "this Project can bind to is unknown. This is not a count of zero."
                ),
            },
            status_code=503,
        )

    fields = [
        {
            "id": row["id"],
            "canonical_name": row["canonical_name"],
            "concept_kind": row["concept_kind"],
            "value_type": row["value_type"],
            "aggregation": row["aggregation"],
            "object_kind": row["object_kind"],
            # Not asked for by the brief and carried anyway: a metric with no
            # aggregation is either non-additive or a row the database would have
            # refused, and a screen that showed an empty Aggregation cell without
            # this flag would read as "nobody filled it in".
            "non_additive": bool(row["non_additive"]),
            "unit": row["unit"],
            "description": row["description"],
            "scope": row["scope"],
        }
        for row in rows
    ]
    platform = sum(1 for field in fields if field["scope"] == "platform")
    return JSONResponse(
        {
            "project_id": project_id,
            "organization_id": org_id,
            "fields": fields,
            # Stated rather than inferred from the list, so a screen never has to
            # decide whether a missing scope was read and empty or not read.
            "scope_counts": {"platform": platform, "project": len(fields) - platform},
            "empty_reason": _EMPTY_REASON if not fields else None,
        }
    )


async def _declare_canonical_fields(request: Request) -> Response:
    """POST {base} -- mint the project fields this Project's objects bind to.

    THE DOOR THE GESTURE NEVER HAD. `canonical_field_registry.declare_many` had
    exactly ONE production caller -- `file_source_template_api.py:717` -- so
    declaring a canonical field was a side effect of describing a FILE. A Project
    fed by a connector pull had no door at all: measured 2026-08-13 on the YouTube
    channel of the derivation dossier, its visible vocabulary was the thirteen
    platform rows, of which zero name a video measure, and a Skill binding
    `mdm_tags: [views]` was refused for naming an ungoverned field. The only
    repair the product offered was to describe a file that does not exist.
    `governance.md` carries the amendment that opened this route.

    ALL OR NOTHING, and it is `declare_many` that holds it: one transaction, one
    commit at the end. Committing per field would leave a half-described object
    whose mapping validates against some of its own columns -- "this is partly
    wrong" instead of "I did not finish".

    A WRITE ASKS MORE THAN A READ. The list route resolves org access; minting
    demands the `member` role, because a viewer who could mint would be writing
    the vocabulary every binding of the Project is validated against.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a malformed body is a client error, not a 500
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required."}, status_code=422
        )
    fields = body.get("fields")
    if not isinstance(fields, list) or not fields:
        return JSONResponse(
            {
                "code": "invalid_body",
                "message": "`fields` must be a non-empty list of declarations.",
            },
            status_code=422,
        )

    from core.admin_api import _require_datastream_role  # noqa: PLC0415
    from core.canonical_field_registry import (  # noqa: PLC0415
        CanonicalFieldError,
        declare_many,
    )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            denied = _require_datastream_role(project_id, identity, "member", conn)
            if denied is not None:
                return denied
            try:
                minted = declare_many(
                    conn, project_id=project_id, fields=fields, actor=identity
                )
            except CanonicalFieldError as exc:
                #  The refusal carries the sentence that says where to repair, and
                #  the transaction is abandoned whole: nothing of a refused batch
                #  reaches the vocabulary.
                conn.rollback()
                return JSONResponse(
                    {"code": "invalid_declaration", "message": str(exc)}, status_code=422
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "mdm_canonical_fields_api: declare failed project=%s: %s", project_id, exc
        )
        return JSONResponse(
            {"code": "server_error", "message": "Server error."}, status_code=500
        )

    return JSONResponse(
        {
            "project_id": project_id,
            "organization_id": org_id,
            "minted": [
                {
                    "id": row["id"],
                    "canonical_name": row["canonical_name"],
                    "concept_kind": row["concept_kind"],
                    "value_type": row["value_type"],
                    "aggregation": row.get("aggregation"),
                    "non_additive": bool(row.get("non_additive")),
                    "unit": row.get("unit"),
                    "object_kind": row.get("object_kind"),
                }
                for row in minted
            ],
        },
        status_code=201,
    )




async def _archive_canonical_field(request: Request) -> Response:
    """DELETE {base}/{field_id} -- retire a project field. Nothing is deleted.

    THE CONTROL HAD NOWHERE TO REACH (AI-304). This route served POST and GET
    and nothing else, so a field declared by mistake stayed in the vocabulary
    for good and the only repair the product offered was SQL by hand. The
    mechanism was already there and unused:
    `canonical_field_registry.archive_project_field` was written and audited
    under `canonical_field.archived`, and had ZERO callers. It is the mirror
    image of the Common Keys table, where the `DELETE` was served and no control
    reached it -- and the verb is that sibling's, deliberately: one family, one
    gesture.

    RETIRING IS NOT DELETING. The row stays, so a published mapping that pins
    the id keeps resolving; no foreign key in this database points at this
    table, so archiving can never be refused by a relationship and there is no
    conflict to render. What changes is that the field leaves every surface at
    once -- `list_visible_canonical_fields` reads `status = 'active'` and is the
    single reader behind both the lens and the route -- and that its NAME is
    freed, which the audit row records under `released_name`.

    PROJECT SCOPE ONLY, without a branch of its own: `archive_project_field`
    matches on `project_id = %s`, so a platform row is not found here rather
    than refused here. That is the same posture as `declare_project_field`,
    which refuses a null project by name.

    A WRITE ASKS MORE THAN A READ -- `member`, like the mint.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    field_id = request.path_params["field_id"]
    _org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    from core.admin_api import _require_datastream_role  # noqa: PLC0415
    from core.canonical_field_registry import (  # noqa: PLC0415
        CanonicalFieldError,
        archive_project_field,
    )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            denied = _require_datastream_role(project_id, identity, "member", conn)
            if denied is not None:
                return denied
            try:
                archived = archive_project_field(
                    conn, project_id=project_id, field_id=field_id, actor=identity
                )
            except CanonicalFieldError:
                # ONE ANSWER FOR THREE STATES, on purpose: never declared,
                # already retired, and belongs to another Project are all 404
                # here. A caller who may read this Project learns that this id is
                # not one of its active fields, which is the whole truth they
                # need; telling the three apart would say whether an id exists
                # somewhere else.
                conn.rollback()
                return _not_found()
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "mdm_canonical_fields_api: archive failed project=%s field=%s: %s",
            project_id,
            field_id,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Server error."}, status_code=500
        )

    return JSONResponse(
        {
            "project_id": project_id,
            "canonical_field": {
                "id": archived["id"],
                "canonical_name": archived["canonical_name"],
                "status": archived["status"],
            },
        }
    )


MDM_CANONICAL_FIELD_ROUTES: list[Route] = [
    Route(_BASE, _list_canonical_fields, methods=["GET"]),
    Route(_BASE, _declare_canonical_fields, methods=["POST"]),
    Route(_ONE, _archive_canonical_field, methods=["DELETE"]),
]

__all__ = ["MDM_CANONICAL_FIELD_ROUTES"]
