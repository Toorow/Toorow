"""toorow -- the door of the MDM measurement grain (story 71.1).

Exports MDM_METRIC_DIMENSION_ROUTES: list[Route] -- a flat list `admin_api.py`
splices into its router at startup, the pattern of `mdm_common_keys_api.py` and
`mdm_canonical_fields_api.py`. Never imported by `admin_api` at module level.

  GET    /api/projects/{project_id}/mdm/measurement-grains
  POST   /api/projects/{project_id}/mdm/measurement-grains
  GET    /api/projects/{project_id}/mdm/measurement-grains/{grain_id}
  POST   /api/projects/{project_id}/mdm/measurement-grains/{grain_id}/versions
  DELETE /api/projects/{project_id}/mdm/measurement-grains/{grain_id}

THE ORDER OF DECLARATION IS PART OF THE CONTRACT. Starlette resolves in order, so
`/measurement-grains` is declared before `/measurement-grains/{grain_id}` and the
`/versions` segment before nothing else could swallow it.

THREE ANSWERS THAT ARE NOT THE SAME, and this module keeps them apart:

  200 + `empty_reason`   nobody has declared a grain yet. A fact about the
                         vocabulary, not about the address -- a 404 here would
                         tell a person the page does not exist.
  404                    foreign, denied or nonexistent, indistinguishably. A
                         caller of another org must not learn an id exists.
  503                    the store could not be read. "I could not look" is not
                         "there is nothing", and only one of the two invites
                         somebody to start declaring.

REFUSALS ARE 422 WITH THEIR CODE. `head_is_dimension` and `member_is_metric` are
sentences a screen can render beside the field that caused them; a flat 400 would
send a person back to guess which of several fields was wrong.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/mdm/measurement-grains"
_ONE = _BASE + "/{grain_id}"
_VERSIONS = _ONE + "/versions"

#: The empty answer's reason, in the vocabulary of the person reading it: it names
#: the gesture, not a table and not a deployment state.
_EMPTY_REASON = {
    "code": "no_measurement_grain_declared",
    "message": (
        "No measurement grain has been declared yet. A measurement grain names "
        "the dimensions a measure is reported against -- spend by day, campaign, "
        "publisher -- and it is declared from the canonical fields this Project "
        "already uses."
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
    return JSONResponse({"code": "not_found", "message": "Not found."}, status_code=404)


def _refused(exc) -> Response:
    return JSONResponse({"code": exc.code, "message": exc.message}, status_code=422)


def _unavailable(what: str) -> Response:
    return JSONResponse(
        {
            "code": "measurement_grains_unavailable",
            "message": (
                f"The {what} could not be read, so what this Project declares is "
                "unknown. This is not a count of zero."
            ),
        },
        status_code=503,
    )


def _guard(
    project_id: str, identity: str, minimum_capability: str = "view"
) -> tuple[str | None, Response | None]:
    """Resolve the org of *project_id* and verify the caller may read it.

    A project the guarded org does not own answers 404 and never 403 -- the same
    posture `mdm_common_keys_api._guard` applies, for the same reason.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                minimum_capability=minimum_capability,
                hold_access=minimum_capability == "edit",
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("metric_dimensions_api: guard failed project=%s: %s", project_id, exc)
        return None, JSONResponse(
            {"code": "server_error", "message": "Server error."}, status_code=500
        )

    if not decision.allowed or not decision.org_id:
        return None, _not_found()
    return str(decision.org_id), None


async def _read_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _members_of(body: dict) -> list:
    members = body.get("members")
    if members is None:
        members = body.get("member_field_ids")
    return members if isinstance(members, list) else []


async def _list_grains(request: Request) -> Response:
    """GET {base} -- every measurement grain of the project, current version included."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    from core.metric_dimensions import list_measurement_grains  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            grains = list_measurement_grains(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("metric_dimensions_api: list failed project=%s: %s", project_id, exc)
        return _unavailable("measurement grains of this Project")

    return JSONResponse(
        {
            "project_id": project_id,
            "organization_id": org_id,
            "measurement_grains": grains,
            "empty_reason": _EMPTY_REASON if not grains else None,
        }
    )


async def _create_grain(request: Request) -> Response:
    """POST {base} -- declare a grain and freeze its version 1."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    _org_id, refusal = _guard(project_id, identity, "edit")
    if refusal is not None:
        return refusal

    body = await _read_body(request)
    from core.metric_dimensions import (  # noqa: PLC0415
        MeasurementGrainNotFound,
        MeasurementGrainRefused,
        create_measurement_grain,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            created = create_measurement_grain(
                conn,
                project_id=project_id,
                name=body.get("name"),
                head_field_id=body.get("head") or body.get("head_field_id"),
                member_field_ids=_members_of(body),
                description=body.get("description"),
                actor=identity,
            )
            conn.commit()
    except MeasurementGrainRefused as exc:
        return _refused(exc)
    except MeasurementGrainNotFound:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        logger.error("metric_dimensions_api: create failed project=%s: %s", project_id, exc)
        return _unavailable("measurement grain store")

    return JSONResponse(
        {"project_id": project_id, "measurement_grain": created}, status_code=201
    )


async def _read_grain(request: Request) -> Response:
    """GET {base}/{id} -- one grain and its versions."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    grain_id = request.path_params["grain_id"]
    _org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    from core.metric_dimensions import (  # noqa: PLC0415
        MeasurementGrainNotFound,
        MetricDimensionsUnavailable,
        read_measurement_grain,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            grain = read_measurement_grain(
                conn, project_id=project_id, measurement_grain_id=grain_id
            )
    except MeasurementGrainNotFound:
        return _not_found()
    except MetricDimensionsUnavailable as exc:
        # The used-by store folded into this read could not be served. "I could
        # not look" is a 503, never an empty dependency list rendered as fact.
        logger.error(
            "metric_dimensions_api: used-by unreadable project=%s grain=%s: %s",
            project_id, grain_id, exc,
        )
        return _unavailable("dependents of this measurement grain")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "metric_dimensions_api: read failed project=%s grain=%s: %s",
            project_id, grain_id, exc,
        )
        return _unavailable("measurement grain")

    return JSONResponse({"project_id": project_id, "measurement_grain": grain})


async def _append_version(request: Request) -> Response:
    """POST {base}/{id}/versions -- append version N+1; never edit version N."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    grain_id = request.path_params["grain_id"]
    _org_id, refusal = _guard(project_id, identity, "edit")
    if refusal is not None:
        return refusal

    body = await _read_body(request)
    from core.metric_dimensions import (  # noqa: PLC0415
        MeasurementGrainNotFound,
        MeasurementGrainRefused,
        append_version,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            appended = append_version(
                conn,
                project_id=project_id,
                measurement_grain_id=grain_id,
                head_field_id=body.get("head") or body.get("head_field_id"),
                member_field_ids=_members_of(body),
                actor=identity,
            )
            conn.commit()
    except MeasurementGrainRefused as exc:
        return _refused(exc)
    except MeasurementGrainNotFound:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "metric_dimensions_api: append failed project=%s grain=%s: %s",
            project_id, grain_id, exc,
        )
        return _unavailable("measurement grain store")

    return JSONResponse(
        {"project_id": project_id, "measurement_grain": appended}, status_code=201
    )


async def _archive_grain(request: Request) -> Response:
    """DELETE {base}/{id} -- archive the grain, freeing its name."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    grain_id = request.path_params["grain_id"]
    _org_id, refusal = _guard(project_id, identity, "edit")
    if refusal is not None:
        return refusal

    from core.metric_dimensions import (  # noqa: PLC0415
        MeasurementGrainNotFound,
        MeasurementGrainRefused,
        MetricDimensionsUnavailable,
        archive_measurement_grain,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            archived = archive_measurement_grain(
                conn, project_id=project_id, measurement_grain_id=grain_id, actor=identity
            )
            conn.commit()
    except MeasurementGrainRefused as exc:
        # `measurement_grain_in_use` is a CONFLICT, not a malformed request: the
        # caller sent something valid and the world says no. 409 is what a screen
        # turns into "retire the reads that slice by this grain first"; the mirror
        # of `mdm_common_keys_api`'s `common_key_in_use`.
        status = 409 if exc.code == "measurement_grain_in_use" else 422
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=status)
    except MetricDimensionsUnavailable as exc:
        # The dependency read that gates the archive did not run. Fail CLOSED: the
        # grain is not retired, and the answer is 503, never a silent archive.
        logger.error(
            "metric_dimensions_api: archive gate unreadable project=%s grain=%s: %s",
            project_id, grain_id, exc,
        )
        return _unavailable("dependents of this measurement grain")
    except MeasurementGrainNotFound:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "metric_dimensions_api: archive failed project=%s grain=%s: %s",
            project_id, grain_id, exc,
        )
        return _unavailable("measurement grain store")

    return JSONResponse({"project_id": project_id, "measurement_grain": archived})


MDM_METRIC_DIMENSION_ROUTES: list[Route] = [
    Route(_BASE, _list_grains, methods=["GET"]),
    Route(_BASE, _create_grain, methods=["POST"]),
    Route(_VERSIONS, _append_version, methods=["POST"]),
    Route(_ONE, _read_grain, methods=["GET"]),
    Route(_ONE, _archive_grain, methods=["DELETE"]),
]

__all__ = ["MDM_METRIC_DIMENSION_ROUTES"]
