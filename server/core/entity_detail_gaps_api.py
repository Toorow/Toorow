"""Chantier C -- the door that lists the observed entities with no detail.

ON DEMAND, AND THAT IS A DECISION. The inventory costs a `DISTINCT` over a
published relation, so it is not folded into every Result read: a screen that
paid for it on every open would make reading a figure expensive to punish nobody.
It is one address, asked when the question is asked.

`entity_kind` IS THE CONCEPT'S NAME. The Connector writes `entity_kind` in its own
vocabulary (`video`, `product`, `campaign`) and the governed dimension carries the
same name -- the same equality that turns a mapping's canonical target into a
member everywhere else in this product. Letting a caller supply the kind would let
it compare two sets that were never the same set.

Contract: docs/product-architecture/data.md, "Amendment, chantier C".
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.entity_detail_gaps import inventory

_PATH = "/api/projects/{project_id}/analyze/entity-gaps"

_NOT_FOUND = {"code": "not_found", "message": "Not found"}


async def _entity_gaps(request: Request) -> Response:
    """GET -- observed, detailed, missing, for one governed dimension."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.query_execution import resolve_physical_plan  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)

    project_id = request.path_params["project_id"]
    view_version_id = request.query_params.get("semantic_view_version_id") or ""
    member_id = request.query_params.get("member_id") or ""
    if not view_version_id or not member_id:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "Name the Semantic View version and the dimension to inventory.",
            },
            400,
        )

    with get_connection() as conn:
        denied = _require_datastream_role(project_id, identity, "viewer", conn)
        if denied is not None:
            return denied

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT cv.name
                FROM app.semantic_view_version_bindings b
                JOIN app.semantic_concept_versions cv ON cv.concept_id = b.concept_id
                WHERE b.view_version_id = %s AND b.project_id = %s AND b.concept_id = %s
                """,
                (view_version_id, project_id, member_id),
            )
            rows = cur.fetchall()
        if len(rows) != 1:
            return JSONResponse(_NOT_FOUND, 404)
        member_name = str(rows[0][0])

        # THE SAME RESOLVER THE EXECUTOR USES. A second way of reaching a
        # relation is a second answer to "what does this Datastream hold".
        plan = resolve_physical_plan(
            conn,
            project_id=project_id,
            semantic_view_version_id=view_version_id,
            spec={"measures": [], "dimensions": [{"id": member_id}]},
        )
        if "unavailable_reason" in plan:
            return JSONResponse(
                {
                    "member_id": member_id,
                    "member_name": member_name,
                    "entity_kind": member_name,
                    "state": "unavailable",
                    "unavailable_reason": plan["unavailable_reason"],
                    "observed_count": None,
                    "detailed_count": None,
                    "missing_count": None,
                    "missing": [],
                    "missing_truncated": False,
                    "observed_truncated": False,
                    "next_gesture": (
                        "What this Semantic View measures for this dimension could "
                        "not be resolved, so nothing is claimed about what is missing."
                    ),
                },
                headers={"Cache-Control": "no-store"},
            )

        return JSONResponse(
            inventory(
                conn,
                project_id=project_id,
                plan=plan,
                member_id=member_id,
                member_name=member_name,
                entity_kind=member_name,
            ),
            headers={"Cache-Control": "no-store"},
        )


entity_detail_gaps_routes = [
    Route(_PATH, endpoint=_entity_gaps, methods=["GET"]),
]
