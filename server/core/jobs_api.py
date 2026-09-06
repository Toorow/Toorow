"""Un travail lance : sa liste, son etat, sa verification.

AD-43, 2026-08-13. Trois routes de LECTURE. Ce qui declenche un travail vit
ailleurs -- ici on regarde ce qui a ete lance et si le resultat tient.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core import pull_job_states as _pull_job_states

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

#: Every state `app.pull_jobs.state` may hold, from the ONE owner (story 63.6).
#: Exported so the conformance sweep can compare it to the CHECK constraint
#: instead of re-reading a literal buried inside a request handler.
VALID_JOB_STATES: frozenset[str] = frozenset(_pull_job_states.JOB_STATES)

async def _list_jobs(request: Request) -> Response:
    """GET /api/jobs -- list pull jobs with optional state and connection_ref_id filters.

    Story 3.4 (AC6): surfaces dead-letter jobs for visibility.
    Future admin UI panels should use this endpoint for the dead-letter queue view.

    Query params (all optional):
        state              filter by job state (e.g. "dead_letter", "queued", "running")
        connection_ref_id  filter by connection (conn_ ULID)

    Response (200):
        {"jobs": [{id, pull_id, connection_ref_id, date_from, date_to, state,
                   requested_by, error_detail, attempt_count,
                   enqueued_at, started_at, completed_at}, ...]}
        All timestamps as ISO-8601 strings. Maximum 200 rows, newest first.

    Error responses:
        401 -- unauthorized
        500 -- DB error
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    # Validate state param against the REGISTRY (story 63.6). It was a literal
    # set of five here, and `superseded` was missing from it: a state the CHECK
    # constraint accepts and this route answered 400 to, so the rows the dedup
    # index produces could not be listed at all.
    state_filter = request.query_params.get("state") or None
    if state_filter is not None and state_filter not in VALID_JOB_STATES:
        valid_list = ", ".join(sorted(VALID_JOB_STATES))
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": (
                    f"Valeur de 'state' invalide : '{state_filter}'. "
                    f"Valeurs valides : {valid_list}."
                ),
            },
            status_code=400,
        )
    conn_ref_filter = request.query_params.get("connection_ref_id") or None
    if conn_ref_filter is not None and len(conn_ref_filter) > 256:
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": (
                    "Shorten 'connection_ref_id' to 256 characters or fewer."
                ),
            },
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Build parameterised query with optional filters
                params: list = []
                sql = """
                    SELECT id, pull_id, connection_ref_id, date_from, date_to,
                           state, requested_by, error_detail, attempt_count,
                           enqueued_at, started_at, completed_at
                    FROM app.pull_jobs
                    WHERE 1=1
                """
                if state_filter is not None:
                    sql += " AND state = %s"
                    params.append(state_filter)
                if conn_ref_filter is not None:
                    sql += " AND connection_ref_id = %s"
                    params.append(conn_ref_filter)
                sql += " ORDER BY enqueued_at DESC LIMIT 200"
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                _TS_COLS = {"enqueued_at", "started_at", "completed_at"}
                jobs = []
                for row in cur.fetchall():
                    record: dict = {}
                    for col, val in zip(cols, row):
                        if col in _TS_COLS and val is not None:
                            record[col] = val.isoformat()
                        elif col in ("date_from", "date_to") and val is not None:
                            record[col] = str(val)
                        else:
                            record[col] = val
                    jobs.append(record)
    except Exception as exc:
        logger.error("admin_api: list_jobs db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse({"jobs": jobs})

async def _get_job_status(request: Request) -> Response:
    """GET /api/jobs/{id} -- get pull job status (Story 3.2, AC5).

    Response (200):
        {"job_id", "pull_id", "state", "connection_ref_id", "date_from", "date_to",
         "enqueued_at", "started_at", "completed_at", "attempt_count", "error_detail"}
        All timestamps as ISO-8601 strings.

    Error responses:
        401 -- unauthorized
        404 -- job not found
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    job_id = request.path_params.get("id", "")
    if not job_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Job id is required"},
            status_code=400,
        )

    from core.queue import get_job_status  # noqa: PLC0415

    job = get_job_status(job_id)
    if job is None:
        return JSONResponse(
            {"code": "not_found", "message": f"Job '{job_id}' not found"},
            status_code=404,
        )

    # AI-24 (Story 4.1 AC12): derive quota_state from error_detail.
    # quota_state values: null (not quota-blocked), "quota_blocked" (budget exhausted),
    # "circuit_open" (breaker tripped).
    error_detail = job.get("error_detail") or ""
    if error_detail.startswith("quota_blocked: circuit_open"):
        quota_state: str | None = "circuit_open"
    elif error_detail.startswith("quota_blocked:"):
        quota_state = "quota_blocked"
    else:
        quota_state = None

    return JSONResponse(
        {
            "job_id": job["id"],
            "pull_id": job["pull_id"],
            "state": job["state"],
            "connection_ref_id": job["connection_ref_id"],
            "date_from": job["date_from"],
            "date_to": job["date_to"],
            "enqueued_at": job.get("enqueued_at"),
            "started_at": job.get("started_at"),
            "completed_at": job.get("completed_at"),
            "attempt_count": job["attempt_count"],
            "error_detail": job.get("error_detail"),
            "quota_state": quota_state,
        }
    )

async def _get_job_verification(request: Request) -> Response:
    """GET /api/jobs/{id}/verification -- return verification record for a job (Story 3.5, AC7).

    Resolves pull_id from app.pull_jobs, then queries app.pull_verifications.

    Response (200):
        {"pull_id", "expected_rows", "actual_rows", "completeness_ratio",
         "verdict", "verified_at"}
        completeness_ratio as float.  verified_at as ISO-8601 string.

    Error responses:
        401 -- unauthorized
        404 -- job not found, or no verification record exists yet
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    job_id = request.path_params.get("id", "")
    if not job_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Job id is required"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # 1. Resolve pull_id from pull_jobs
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pull_id FROM app.pull_jobs WHERE id = %s",
                    (job_id,),
                )
                job_row = cur.fetchone()

            if job_row is None:
                return JSONResponse(
                    {"code": "not_found", "message": f"Job '{job_id}' not found"},
                    status_code=404,
                )

            pull_id = job_row[0]

            # 2. Query pull_verifications
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT pull_id, expected_rows, actual_rows,
                           completeness_ratio, verdict, verified_at
                    FROM app.pull_verifications
                    WHERE pull_id = %s
                    """,
                    (pull_id,),
                )
                ver_row = cur.fetchone()
    except Exception as exc:
        logger.error("admin_api: get_job_verification db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    if ver_row is None:
        return JSONResponse(
            {
                "code": "not_found",
                "message": "No verification record for this job",
            },
            status_code=404,
        )

    return JSONResponse(
        {
            "pull_id": ver_row[0],
            "expected_rows": ver_row[1],
            "actual_rows": ver_row[2],
            "completeness_ratio": float(ver_row[3]),
            "verdict": ver_row[4],
            "verified_at": ver_row[5].isoformat() if ver_row[5] is not None else None,
        }
    )


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : memes
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

JOBS_ROUTES_1 = [
    # Story 3.4 (AC6): list jobs with optional filters (?state=&connection_ref_id=)
    # IMPORTANT: /api/jobs must come before /api/jobs/{id} so the list route matches first.
    Route("/api/jobs", endpoint=_list_jobs, methods=["GET"]),
    # Story 3.5 (AC7): verification endpoint MUST be before /api/jobs/{id}
    # so Starlette does not absorb "verification" as the job ID parameter.
    Route(
        "/api/jobs/{id}/verification",
        endpoint=_get_job_verification,
        methods=["GET"],
    ),
    Route(
        "/api/jobs/{id}",
        endpoint=_get_job_status,
        methods=["GET"],
    ),
]
