"""Scoped REST surface for the Story 45.1 Context Hub taxonomy.

Routes (the four IDENTITY writes refuse since 2026-08-25 -- story 49.3, the
acceptance schedule ratified in `docs/product-architecture/governance.md`)::

  GET    /api/context/business-taxonomy              read
  GET    /api/context/business-links                 read
  POST   /api/context/business-paths/preview         read (a preview writes nothing)
  POST   /api/context/business-domains               REFUSED, 409 legacy_store_is_read_only
  PATCH  /api/context/business-domains/{id}          REFUSED, 409 legacy_store_is_read_only
  POST   /api/context/business-classifications       REFUSED, 409 legacy_store_is_read_only
  PATCH  /api/context/business-classifications/{id}  REFUSED, 409 legacy_store_is_read_only
  POST   /api/context/business-links                 write -- a link is not an identity
  DELETE /api/context/business-links/{id}            write -- the governed retirement

THEY REFUSE RATHER THAN DISAPPEAR, the shape the 67.23 Notebooks cutover set and
story 49.3 applied to eight more doors on the same day as this one. Unmounting a
write answers 404, which tells a caller its object does not exist and sends it
looking for it; a refusal names the gesture that works instead.

THE CONSOLE STILL CALLS THEM, and that is known rather than missed.
`ui/admin/src/connaissances/ContextHubLayout.tsx` calls all four and
`ui/admin/src/governance/NewBusinessDomainDialog.tsx` the two creations, so
their buttons surface the refusal -- each printing this sentence rather than
folding it into the stale-version 409 the Hub already answered. The ratified
schedule puts the Master Data authoring screen in the tasks that follow --
*"The screen arrives with tasks 6/7"* -- and
between the two, one refusal that names the convergence is strictly better than
two authorities writing for the same organization.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core import business_taxonomy as taxonomy
from core.row_json import RowJSON

logger = logging.getLogger(__name__)


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as admin_check_auth  # noqa: PLC0415

    return await admin_check_auth(request)


def _not_found() -> JSONResponse:
    return JSONResponse({"code": "not_found", "message": "Project not found"}, status_code=404)



#: Toute reponse qui porte une ligne de base passe par la. La classe et les trois
#: pannes qui l'ont rendue necessaire sont dans `core/row_json.py` -- elle n'est
#: pas locale a ce fichier, elle s'est presentee dans trois.
_TaxonomyJSON = RowJSON


def _project_id(request: Request, body: dict[str, Any] | None = None) -> str | None:
    query_param = (request.query_params.get("project_id") or "").strip()
    if query_param:
        return query_param
    if body and isinstance(body, dict):
        body_param = str(body.get("project_id") or "").strip()
        if body_param:
            return body_param
    return None


def _authorize_scope(
    conn,
    *,
    identity: str,
    project_id: str,
    capability: str,
    require_org_manage: bool = False,
) -> str | None:
    from core.admin_api import _strict_project_capability_allowed  # noqa: PLC0415
    from core.project_access import identity_can_manage_org  # noqa: PLC0415

    if not _strict_project_capability_allowed(
        conn,
        identity=identity,
        project_id=project_id,
        minimum_capability=capability,
        hold_access=True,
    ):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.projects WHERE id = %s AND status = 'active'",
            (project_id,),
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        return None
    org_id = str(row[0])
    if require_org_manage and not identity_can_manage_org(org_id, identity, conn):
        return None
    return org_id


async def _json_body(request: Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _refuse_legacy_taxonomy_write() -> JSONResponse:
    """One code, one sentence, for the four identity doors.

    The sentence lives in `core.business_taxonomy` beside the writers it replaced,
    so the REST refusal and the exception a direct caller gets cannot drift into
    saying two different things about the same cutover.
    """
    return JSONResponse(
        {
            "code": taxonomy.LEGACY_TAXONOMY_WRITE_REFUSED_CODE,
            "message": taxonomy.LEGACY_TAXONOMY_WRITE_REFUSED_MESSAGE,
        },
        status_code=409,
    )


def _error_response(exc: Exception) -> JSONResponse:
    # Second line of defence, and it must never be reachable from this module:
    # the four handlers refuse at the door. It exists because `_with_scope` wraps
    # every operation, and a refusal that fell through to the generic branch
    # would answer 500 -- "the Context Hub is temporarily unavailable" -- to a
    # caller whose request will never be accepted again.
    if isinstance(exc, taxonomy.LegacyTaxonomyWriteRefused):
        return _refuse_legacy_taxonomy_write()
    if isinstance(exc, taxonomy.StaleTaxonomyVersionError):
        return JSONResponse({"code": "conflict", "message": str(exc)}, status_code=409)
    if isinstance(exc, taxonomy.DuplicateTaxonomySlugError):
        return JSONResponse({"code": "conflict", "message": str(exc)}, status_code=409)
    if isinstance(exc, taxonomy.TaxonomyNotFoundError):
        return JSONResponse({"code": "not_found", "message": "Resource not found"}, status_code=404)
    if isinstance(exc, taxonomy.BusinessTaxonomyError):
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)
    logger.warning("business_taxonomy_api: request failed: %s", type(exc).__name__)
    return JSONResponse(
        {"code": "db_error", "message": "Context Hub is temporarily unavailable"},
        status_code=500,
    )


async def _with_scope(
    request: Request,
    *,
    capability: str,
    require_org_manage: bool,
    operation: Callable[[Any, str, str, str], Response],
    body: dict[str, Any] | None = None,
) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"},
            status_code=401,
        )
    project_id = _project_id(request, body)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"},
            status_code=422,
        )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            org_id = _authorize_scope(
                conn,
                identity=identity,
                project_id=project_id,
                capability=capability,
                require_org_manage=require_org_manage,
            )
            if org_id is None:
                return _not_found()
            return operation(conn, org_id, project_id, identity)
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc)


async def _list_taxonomy(request: Request) -> Response:
    status_filter = (request.query_params.get("status") or "active").strip() or "active"
    projection = (request.query_params.get("projection") or "full").strip() or "full"
    if projection not in ("full", "analysis-picker"):
        return JSONResponse(
            {"code": "invalid_param", "message": "unknown taxonomy projection"},
            status_code=422,
        )
    if status_filter not in ("active", "archived", "all"):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "status must be one of: active, archived, all",
            },
            status_code=422,
        )

    def operation(conn, org_id: str, _project_id_value: str, _identity: str) -> Response:
        if projection == "analysis-picker":
            domains = taxonomy.list_domain_picker(
                conn, org_id=org_id, status=status_filter, limit=201
            )
            answer = {
                "org_id": org_id,
                "domains": [
                    {
                        "id": domain["id"],
                        "name": str(domain.get("name") or "")[:160],
                        "version_number": domain.get("version_number"),
                    }
                    for domain in domains[:200]
                ],
                "classifications": [],
                "truncated": len(domains) > 200,
            }
        else:
            answer = taxonomy.list_taxonomy(conn, org_id=org_id, status=status_filter)
        return _TaxonomyJSON(answer)

    return await _with_scope(
        request, capability="view", require_org_manage=False, operation=operation
    )


async def _create_domain(request: Request) -> Response:
    """POST /api/context/business-domains -- REFUSED since 2026-08-25 (49.3).

    A Business Domain is an organization identity, and the authority that owns it
    is Master Data. This door wrote the superseded store directly; an organization
    converges first -- the identity keeps the id it already has -- and the domain
    is created on the Master Data screen afterwards.
    """
    return _refuse_legacy_taxonomy_write()


async def _update_domain(request: Request) -> Response:
    """PATCH /api/context/business-domains/{id} -- REFUSED since 2026-08-25 (49.3).

    Renaming, archiving and restoring all came through here. All three are acts
    on the identity, so all three belong to the authority that holds it: a rename
    publishes a new version, an archive is the guarded node command.
    """
    return _refuse_legacy_taxonomy_write()


async def _create_classification(request: Request) -> Response:
    """POST /api/context/business-classifications -- REFUSED since 2026-08-25 (49.3)."""
    return _refuse_legacy_taxonomy_write()


async def _update_classification(request: Request) -> Response:
    """PATCH /api/context/business-classifications/{id} -- REFUSED since 2026-08-25 (49.3)."""
    return _refuse_legacy_taxonomy_write()


async def _list_links(request: Request) -> Response:
    taxonomy_id = (request.query_params.get("taxonomy_id") or "").strip() or None

    def operation(conn, org_id: str, project_id: str, _identity: str) -> Response:
        return _TaxonomyJSON(
            {
                "links": taxonomy.list_links(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    taxonomy_id=taxonomy_id,
                )
            }
        )

    return await _with_scope(
        request, capability="view", require_org_manage=False, operation=operation
    )


async def _create_link(request: Request) -> Response:
    body = await _json_body(request)
    if body is None:
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"},
            status_code=422,
        )
    try:
        taxonomy._validate_link_kinds(body.get("taxonomy_type"), body.get("target_type"))
    except Exception as exc:  # noqa: BLE001
        return _error_response(exc)

    def operation(conn, org_id: str, project_id: str, identity: str) -> Response:
        from core.main import get_loaded_modules  # noqa: PLC0415

        row = taxonomy.create_link(
            conn,
            org_id=org_id,
            project_id=project_id,
            taxonomy_type=body.get("taxonomy_type"),
            taxonomy_id=body.get("taxonomy_id"),
            target_type=body.get("target_type"),
            target_id=body.get("target_id"),
            relation_type=body.get("relation_type"),
            # `derived` marque un lien qu'une machine a propose. Le defaut reste
            # `direct` : une omission ne doit jamais faire passer une deduction
            # pour une decision.
            link_origin=body.get("link_origin") or "direct",
            actor=identity,
            reason=body.get("reason"),
            trace_id=body.get("trace_id"),
            loaded_modules=get_loaded_modules(),
        )
        conn.commit()
        return _TaxonomyJSON(row, status_code=201)

    return await _with_scope(
        request, capability="edit", require_org_manage=False, operation=operation
    )


async def _retire_link(request: Request) -> Response:
    # The verb stays DELETE on the wire and the act underneath is a RETIREMENT
    # (migration 306): the row stays, stops being served, and names who withdrew
    # it and why. The HTTP method describes what the caller asks of the
    # collection -- "this link is no longer part of it" -- and that is exactly
    # what happens; what changed is that the history survives it.
    #
    # An absent body is the same request as an empty one -- live finding F3
    # repaired the 422 "JSON object body required" this route used to answer
    # when the caller sent nothing. That stays.
    #
    # WHAT CHANGED, AND WHY IT IS NOT A REGRESSION OF F3. `reason` was described
    # here as optional audit metadata, and the writer has ALWAYS refused a
    # missing one (`_audit` calls `_required(reason, ...)`). So a body-less
    # DELETE reached the writer and raised, and this route's promise was false
    # from the outside: the request was accepted and then failed with a message
    # about a field the route said was optional. Now that the act is a
    # RETIREMENT the reason is load-bearing -- *"without a reason the next
    # reader cannot tell a wrong link from an inconvenient one"* -- so it is
    # refused AT THE DOOR, with a sentence naming what to send. F3 was about a
    # value the route did not use; this is a value it cannot do without.
    body = await _json_body(request) or {}
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return JSONResponse(
            {
                "code": "reason_required",
                "message": (
                    "Send a reason: withdrawing a governed business link is recorded, "
                    "and the next reader has to be able to tell a wrong link from an "
                    "inconvenient one."
                ),
            },
            status_code=422,
        )

    def operation(conn, org_id: str, project_id: str, identity: str) -> Response:
        taxonomy.retire_link(
            conn,
            org_id=org_id,
            project_id=project_id,
            link_id=request.path_params["id"],
            actor=identity,
            reason=reason,
            trace_id=body.get("trace_id"),
        )
        conn.commit()
        return Response(status_code=204)

    return await _with_scope(
        request, capability="edit", require_org_manage=False, operation=operation
    )


async def _preview_path(request: Request) -> Response:
    body = await _json_body(request)
    if body is None:
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"},
            status_code=422,
        )

    def operation(conn, org_id: str, project_id: str, _identity: str) -> Response:
        resolved = taxonomy.preview_business_path(
            conn,
            org_id=org_id,
            project_id=project_id,
            target_type=body.get("target_type"),
            target_id=body.get("target_id"),
            taxonomy_id=body.get("taxonomy_id"),
        )
        if resolved is None:
            return JSONResponse(
                {"code": "not_found", "message": "Governed business path not found"},
                status_code=404,
            )
        return _TaxonomyJSON(resolved)

    return await _with_scope(
        request, capability="view", require_org_manage=False, operation=operation
    )


BUSINESS_TAXONOMY_ROUTES: list[Route] = [
    Route("/api/context/business-taxonomy", endpoint=_list_taxonomy, methods=["GET"]),
    Route("/api/context/business-domains", endpoint=_create_domain, methods=["POST"]),
    Route(
        "/api/context/business-domains/{id}",
        endpoint=_update_domain,
        methods=["PATCH"],
    ),
    Route(
        "/api/context/business-classifications",
        endpoint=_create_classification,
        methods=["POST"],
    ),
    Route(
        "/api/context/business-classifications/{id}",
        endpoint=_update_classification,
        methods=["PATCH"],
    ),
    Route("/api/context/business-links", endpoint=_list_links, methods=["GET"]),
    Route("/api/context/business-paths/preview", endpoint=_preview_path, methods=["POST"]),
    Route("/api/context/business-links", endpoint=_create_link, methods=["POST"]),
    Route("/api/context/business-links/{id}", endpoint=_retire_link, methods=["DELETE"]),
]
