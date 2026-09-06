"""The door of the MMM extract (story 62.1).

Exports MMM_EXPORT_ROUTES: list[Route] -- the flat list `admin_api.py` splices
into its router at startup, the pattern of `metric_dimensions_api.py`. Never
imported by `admin_api` at module level.

  POST /api/projects/{project_id}/exports/mmm[?format=csv]

WHY POST FOR A READ, AND WHY IT ASKS FOR `view` AND NOT `edit`. The request names
a Semantic View version, a list of metrics, a list of dimensions and a window --
a body, not a path. Nothing about it writes: no table row, no dataset, no file in
a bucket, no audit row of its own beyond the cross-scope denial the access guard
already records. So the capability floor is the reading floor, and there is NO
`Idempotency-Key`: a key exists to make a repeated WRITE land once, and repeating
this call twice produces two identical files and changes nothing. Requiring one
would tell the next reader that something here is written.

TWO REPRESENTATIONS OF ONE READ, AND THEY COME FROM ONE CALL.

  default        JSON: `columns`, `rows`, and the `provenance` that is the
                 companion the ratified section names -- View version, Concept
                 versions, request hash, Datastream, Output version, relation,
                 pull, the measurement grain version per metric, the declared
                 currency per monetary metric, the window, the date gaps and the
                 row count.
  `?format=csv`  the table alone, as a plain CSV a modeling tool reads with no
                 options. The provenance does not travel in `#` comment lines --
                 it travels in the JSON representation of the same request, and
                 the response says so in a header rather than implying it.

EVERY REFUSAL IS 422 WITH ITS CODE, ITS SENTENCE AND ITS GESTURE. A flat 400
would send a person back to guess which of five things was wrong; `mmm_export`
already names each one and the gesture that repairs it, and this module carries
them out whole.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/exports/mmm"


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
    )


def _not_found() -> Response:
    return JSONResponse({"code": "not_found", "message": "Not found."}, status_code=404)


def _guard(project_id: str, identity: str) -> Response | None:
    """A project the caller's org does not own answers 404, never 403.

    The same posture `metric_dimensions_api._guard` takes, and for its reason: a
    caller of another organization must not learn that an id exists.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity, conn, project_id=project_id, minimum_capability="view"
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("mmm_export_api: guard failed project=%s: %s", project_id, exc)
        return JSONResponse({"code": "server_error", "message": "Server error."}, 500)
    if not decision.allowed or not decision.org_id:
        return _not_found()
    return None


async def _read_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _names(value) -> list[str]:
    """A list of member names, and nothing that is not one.

    A caller sending `"spend"` instead of `["spend"]` is answered by the
    `member_not_in_semantic_view` refusal naming what the View does publish --
    never by iterating the characters of the string.
    """
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int)) and str(item)]


async def _export_mmm(request: Request) -> Response:
    """POST {base} -- the long-format extract of one published Semantic View version."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    body = await _read_body(request)
    from core.mmm_export import MmmExportRefused, build_extract, extract_csv  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            payload = build_extract(
                conn,
                project_id=project_id,
                semantic_view_version_id=str(body.get("semantic_view_version_id") or ""),
                metrics=_names(body.get("metrics")),
                dimensions=_names(body.get("dimensions")),
                start=body.get("start"),
                end=body.get("end"),
            )
    except MmmExportRefused as exc:
        return JSONResponse(exc.payload(), status_code=422)
    except Exception as exc:  # noqa: BLE001
        logger.error("mmm_export_api: extract failed project=%s: %s", project_id, exc)
        return JSONResponse(
            {
                "code": "mmm_extract_unavailable",
                "message": (
                    "This extract could not be produced, so no file was written. "
                    "This is not an empty extract."
                ),
                "gesture": "Try again; if it keeps failing, open the Datastream's "
                "Runs tab, which carries the run behind this Semantic View.",
            },
            status_code=503,
        )

    if request.query_params.get("format") != "csv":
        return JSONResponse({"project_id": project_id, **payload})

    provenance = payload.get("provenance") or {}
    window = provenance.get("window") or {}
    filename = (
        f"mmm_{window.get('start', 'start')}_{window.get('end', 'end')}.csv"
    )
    return Response(
        content=extract_csv(payload),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # The companion is not implied: it is named, with the request that
            # produces it. A saved CSV whose provenance nobody can find again is
            # the file this whole story exists to avoid.
            "X-Toorow-Export-Companion": "POST the same body without ?format=csv",
            "X-Toorow-Export-Rows": str(len(payload.get("rows") or [])),
            "X-Toorow-Export-View-Version": str(
                provenance.get("semantic_view_version_id") or ""
            ),
            "X-Toorow-Export-Request-Hash": str(provenance.get("request_hash") or ""),
            "X-Toorow-Export-Date-Gaps": str(
                (provenance.get("date_gaps") or {}).get("total", 0)
            ),
        },
    )


MMM_EXPORT_ROUTES: list[Route] = [Route(_BASE, _export_mmm, methods=["POST"])]

__all__ = ["MMM_EXPORT_ROUTES"]
