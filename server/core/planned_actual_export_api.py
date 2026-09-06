"""The door of the planned-versus-actual extract (story 62.2).

Exports PLANNED_ACTUAL_EXPORT_ROUTES: list[Route] -- the flat list `admin_api.py`
splices into its router at startup, the pattern `mmm_export_api.py` set for the
first reader of this seam. Never imported by `admin_api` at module level.

  POST /api/projects/{project_id}/exports/planned-vs-actual[?format=csv]

ONE PATTERN, NOT A SECOND ONE. Everything about this door is the door of the MMM
extract, deliberately: POST for a read because the request is a body (a plan and
a window) and not a path; the `view` capability floor because nothing here
writes; NO `Idempotency-Key`, because a key exists to make a repeated WRITE land
once and requiring one would tell the next reader that something here is written;
404 and never 403 on a Project the caller's org does not own, so a caller of
another organization cannot learn that an id exists; 422 with a code, a sentence
and a gesture for every refusal, because a flat 400 sends a person back to guess
which of six things was wrong.

TWO REPRESENTATIONS OF ONE READ, FROM ONE CALL.

  default        JSON: `columns`, `rows`, and the `provenance` -- the media plan,
                 its published version, the placement-mapping fingerprint and how
                 that set was obtained, the one declared currency, the mart the
                 comparison came from, the window, and the four coverage counts
                 that keep an under-delivery apart from a hole.
  `?format=csv`  the table alone, written by the seam's own writer, with no
                 comment lines a modeling or audit tool has to be told about.

THE HEADERS NAME THE VARIANCE, NOT ONLY THE ROW COUNT. A saved CSV whose
under-delivery count nobody can find again is the file this story exists to
avoid, so `X-Toorow-Export-Variance-Days` travels beside the companion pointer --
and `X-Toorow-Export-Date-Gaps` carries the HOLES alone, the same number the MMM
extract puts under that name.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/exports/planned-vs-actual"


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
    """A project the caller's org does not own answers 404, never 403."""
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity, conn, project_id=project_id, minimum_capability="view"
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "planned_actual_export_api: guard failed project=%s: %s", project_id, exc
        )
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


async def _export_planned_vs_actual(request: Request) -> Response:
    """POST {base} -- the long-format variance of one published media plan."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    body = await _read_body(request)
    from core.mmm_export import ExportRefused, extract_csv  # noqa: PLC0415
    from core.planned_actual_export import build_extract  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            payload = build_extract(
                conn,
                project_id=project_id,
                plan_id=str(body.get("plan_id") or ""),
                start=body.get("start"),
                end=body.get("end"),
            )
    except ExportRefused as exc:
        return JSONResponse(exc.payload(), status_code=422)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "planned_actual_export_api: extract failed project=%s: %s", project_id, exc
        )
        return JSONResponse(
            {
                "code": "planned_vs_actual_extract_unavailable",
                "message": (
                    "This extract could not be produced, so no file was written. "
                    "This is not an empty extract."
                ),
                "gesture": "Try again; if it keeps failing, this Project's "
                "warehouse has not been rebuilt since the plan was published.",
            },
            status_code=503,
        )

    if request.query_params.get("format") != "csv":
        return JSONResponse({"project_id": project_id, **payload})

    provenance = payload.get("provenance") or {}
    window = provenance.get("window") or {}
    coverage = provenance.get("date_coverage") or {}
    filename = (
        f"planned_vs_actual_{window.get('start', 'start')}_"
        f"{window.get('end', 'end')}.csv"
    )
    return Response(
        content=extract_csv(payload),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Toorow-Export-Companion": "POST the same body without ?format=csv",
            "X-Toorow-Export-Rows": str(len(payload.get("rows") or [])),
            "X-Toorow-Export-Plan-Version": str(
                provenance.get("media_plan_version_id") or ""
            ),
            "X-Toorow-Export-Mapping-Fingerprint": str(
                (provenance.get("placement_mapping") or {}).get("fingerprint") or ""
            ),
            "X-Toorow-Export-Currency": str(
                (provenance.get("currency") or {}).get("currency") or ""
            ),
            # THE HOLES ALONE, under the name the first reader of this seam uses
            # for them. An under-delivery is not a gap and does not travel here.
            "X-Toorow-Export-Date-Gaps": str(coverage.get("total_holes", 0)),
            "X-Toorow-Export-Variance-Days": str(
                (coverage.get("variance_days") or {}).get("count", 0)
            ),
        },
    )


PLANNED_ACTUAL_EXPORT_ROUTES: list[Route] = [
    Route(_BASE, _export_planned_vs_actual, methods=["POST"])
]

__all__ = ["PLANNED_ACTUAL_EXPORT_ROUTES"]
