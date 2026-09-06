"""toorow -- the two doors of match discovery (story 66.2).

Exports DATASTREAM_MATCH_ROUTES: list[Route] spliced into `admin_api.py`.

  GET /api/projects/{project_id}/analyze/matches
  GET /api/projects/{project_id}/analyze/matches/profile   (story 66.3)
  GET /api/projects/{project_id}/datastreams/{datastream_id}/matches

TWO ADDRESSES, ONE PRODUCER. Analyze asks "what can I cross in this Project" and
a Datastream asks "what can I be crossed with" -- the second is the first,
filtered. Two producers would drift the day one of them learned about a new
candidate kind, and the Datastream door would quietly stop showing it.

THE ANSWERS THIS DOOR KEEPS APART, which are the same five as every scoped read
of this repository: 401 without identity; 404 for foreign/denied/absent, never
403; 200 with `empty_reason` when there is genuinely nothing to cross; 503 with
NO list when the catalog could not be read. There is no 422 here: this is a read,
and a read refuses nothing.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_CATALOG = "/api/projects/{project_id}/analyze/matches"
_PER_DATASTREAM = "/api/projects/{project_id}/datastreams/{datastream_id}/matches"
_PROFILE = _CATALOG + "/profile"


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
    )


def _not_found() -> Response:
    return JSONResponse({"code": "not_found", "message": "Not found."}, status_code=404)


def _unavailable() -> Response:
    return JSONResponse(
        {
            "code": "matches_unavailable",
            "message": (
                "The catalog of sources could not be read, so which of them can be "
                "crossed is unknown. This is not a count of zero."
            ),
        },
        status_code=503,
    )


def _guard(project_id: str, identity: str) -> tuple[str | None, Response | None]:
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity, conn, project_id=project_id, minimum_capability="view"
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("datastream_matches_api: guard failed project=%s: %s", project_id, exc)
        return None, JSONResponse(
            {"code": "server_error", "message": "Server error."}, status_code=500
        )

    if not decision.allowed or not decision.org_id:
        return None, _not_found()
    return str(decision.org_id), None


async def _catalog(request: Request) -> Response:
    """GET {catalog} -- every governed cross and candidate of the Project."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    from core.datastream_matches import MatchesUnavailable, discover_matches  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            answer = discover_matches(conn, project_id=project_id)
    except MatchesUnavailable:
        return _unavailable()
    except Exception as exc:  # noqa: BLE001
        logger.error("datastream_matches_api: catalog failed project=%s: %s", project_id, exc)
        return _unavailable()

    return JSONResponse({"project_id": project_id, "organization_id": org_id, **answer})


async def _for_datastream(request: Request) -> Response:
    """GET {per_datastream} -- the same answer, filtered to one source."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    from core.datastream_matches import (  # noqa: PLC0415
        DatastreamNotFound,
        MatchesUnavailable,
        matches_for_datastream,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            answer = matches_for_datastream(
                conn, project_id=project_id, datastream_id=datastream_id
            )
    except DatastreamNotFound:
        return _not_found()
    except MatchesUnavailable:
        return _unavailable()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "datastream_matches_api: per-datastream failed project=%s ds=%s: %s",
            project_id, datastream_id, exc,
        )
        return _unavailable()

    return JSONResponse(
        {
            "project_id": project_id,
            "organization_id": org_id,
            "datastream_id": datastream_id,
            **answer,
        }
    )


async def _profile(request: Request) -> Response:
    """GET {catalog}/profile -- what this cross would do to the rows.

    A 422 here is not a malformed request: it is the product refusing to guess.
    `key_component_unmapped` and `no_published_output` each name the gesture that
    would make the profile possible, and a screen renders them beside the source
    that caused them. Answering 200 with an empty profile would read as "these
    sources share nothing" -- a sentence about the business, not about the gap.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    _org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    left = request.query_params.get("left") or ""
    right = request.query_params.get("right") or ""
    key_version = request.query_params.get("common_key_version_id") or ""
    relationship_name = request.query_params.get("relationship_name") or ""
    view_version_id = request.query_params.get("view_version_id") or ""
    if not (left and right and key_version and relationship_name and view_version_id):
        return JSONResponse(
            {
                "code": "profile_request_incomplete",
                "message": (
                    "A match profile needs both sources, the exact common key version and "
                    "the approved relationship version. Open it from a match rather than "
                    "by hand."
                ),
            },
            status_code=422,
        )

    from core.match_profile import ProfileRefused, profile_match  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            answer = profile_match(
                conn,
                project_id=project_id,
                left_datastream_id=left,
                right_datastream_id=right,
                common_key_version_id=key_version,
                relationship_name=relationship_name,
                view_version_id=view_version_id,
            )
    except ProfileRefused as exc:
        return JSONResponse(
            {"code": exc.code, "message": exc.message, "missing_link": exc.missing_link},
            status_code=422,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("datastream_matches_api: profile failed project=%s: %s", project_id, exc)
        return _unavailable()

    return JSONResponse({"project_id": project_id, "profile": answer})


DATASTREAM_MATCH_ROUTES: list[Route] = [
    # `/profile` before the parameterised catalog entries: Starlette resolves in
    # order, and a later `/matches/{id}` would otherwise swallow this segment.
    Route(_PROFILE, _profile, methods=["GET"]),
    Route(_CATALOG, _catalog, methods=["GET"]),
    Route(_PER_DATASTREAM, _for_datastream, methods=["GET"]),
]

__all__ = ["DATASTREAM_MATCH_ROUTES"]
