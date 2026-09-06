"""La surface de preuve d'epic 14, remplacee ecran par ecran -- et pas encore retiree.

AD-43, 2026-08-13. Trois lectures que plus aucun ecran n'ouvre :

    GET /api/feedback                  remplacee par /api/projects/{}/test/feedback
    GET /api/eval/golden-questions     remplacee par l'atelier Golden Question (51.1)
    GET /api/eval/runs                 remplacee par Regression Runs (51.2 / 51.4)

Chacune des trois remplacantes DIT dans son propre en-tete pourquoi elle ne
retombe jamais sur celle-ci : la ligne d'ici ne fixe ni Resultat, ni version de
Vue Semantique, ni Chemin IA, et la precedente presentait un pourcentage brut que
`analyze-and-test.md:313` interdit de montrer seul.

CE FICHIER NE LES RETIRE PAS, et c'est ecrit dans `known-debt.json` depuis le
2026-07-31 : « deleting it would erase the last read surface on the benchmark
corpus [...] the owner of the epic-14 harness decides ». Elles sont
NON REFERENCEES, pas cassees. Les tenir a part est ce qu'on peut faire sans
decider a la place de leur proprietaire : un lecteur qui ouvre `feedback_review_api`
ou `golden_questions_api` ne tombe plus sur un homonyme de l'ancienne surface.

Les trois noms portent `legacy` pour cette raison exacte -- `_list_feedback` et
`_list_golden_questions` existaient EN DOUBLE dans `core`, et une recherche par
nom seul trouvait l'un pour l'autre.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("core.admin_api")


# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)


def _project_not_found_response(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _project_not_found_response as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)


def _strict_project_capability_allowed(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _strict_project_capability_allowed as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)


# --- les handlers, deplaces TELS QUELS (seul le nom change) ---------------


async def _list_legacy_feedback_rows(request: Request) -> Response:
    """GET /api/feedback?project_id=<id>&module=<name>&limit=50

    List feedback rows for a project, ordered by created_at DESC.

    Query params:
        project_id  (required)
        module      (optional) -- filter by module name
        limit       (optional, default 50, max 200)

    Response (200):
        [{"id", "rating", "comment", "module", "report_ref", "trace_id", "created_at"}]
        Note: created_by is intentionally omitted (privacy).

    Error responses:
        400 -- missing project_id
        401 -- unauthorized
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            )
    except Exception as exc:
        logger.warning(
            "admin_api: feedback access unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return _project_not_found_response()
    if not allowed:
        return _project_not_found_response()

    module_filter = request.query_params.get("module") or None
    try:
        limit = max(1, min(int(request.query_params.get("limit", "50")), 200))
    except (TypeError, ValueError):
        limit = 50

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                params: list = [project_id]
                sql = """
                    SELECT id, rating, comment, module, report_ref, trace_id, created_at
                    FROM app.feedback
                    WHERE project_id = %s
                """
                if module_filter is not None:
                    sql += " AND module = %s"
                    params.append(module_filter)
                sql += " ORDER BY created_at DESC LIMIT %s"
                params.append(limit)
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                rows: list[dict] = []
                for row in cur.fetchall():
                    record: dict = {}
                    for col, val in zip(cols, row):
                        if col == "created_at" and val is not None:
                            record[col] = val.isoformat()
                        else:
                            record[col] = val
                    rows.append(record)
    except Exception as exc:
        logger.error("admin_api: list_feedback_error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Feedback is temporarily unavailable"},
            status_code=500,
        )

    return JSONResponse(rows)


# ---------------------------------------------------------------------------
# Test workspace read surfaces (Migration 096, eval loop — Epic 14)
# GET /api/eval/golden-questions?project_id=<id>
# GET /api/eval/runs?project_id=<id>
# ---------------------------------------------------------------------------
async def _list_legacy_benchmark_questions(request: Request) -> Response:
    """GET /api/eval/golden-questions?project_id=<id> -- authorized benchmark set."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"}, status_code=400
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            )
    except Exception as exc:
        logger.warning(
            "admin_api: golden-questions access unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return _project_not_found_response()
    if not allowed:
        return _project_not_found_response()

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, question, topic, expected_citations, last_result,
                           expected_business_routes
                    FROM app.eval_benchmark_questions
                    WHERE project_id = %s
                    ORDER BY topic ASC, question ASC
                    """,
                    (project_id,),
                )
                cols = [d[0] for d in cur.description]
                questions = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_api: list_golden_questions db_error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Evaluation evidence is temporarily unavailable"},
            status_code=500,
        )
    return JSONResponse({"questions": questions}, status_code=200)


async def _list_legacy_eval_runs(request: Request) -> Response:
    """GET /api/eval/runs?project_id=<id> -- authorized persisted run history."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"}, status_code=400
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            )
    except Exception as exc:
        logger.warning(
            "admin_api: eval-runs access unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return _project_not_found_response()
    if not allowed:
        return _project_not_found_response()

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, run_at, score_passed, score_total, precision_pct,
                           regressions, status, observed_path_keys, observed_trace_ids,
                           path_coverage_pct, missing_path_count, wrong_domain_count,
                           path_version_drift_count
                    FROM app.eval_runs
                    WHERE project_id = %s
                    ORDER BY run_at DESC
                    """,
                    (project_id,),
                )
                cols = [d[0] for d in cur.description]
                runs = [
                    {
                        c: (
                            v.isoformat()
                            if c == "run_at" and v is not None
                            else (
                                float(v)
                                if c in {"precision_pct", "path_coverage_pct"} and v is not None
                                else v
                            )
                        )
                        for c, v in zip(cols, row)
                    }
                    for row in cur.fetchall()
                ]
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_api: list_eval_runs db_error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Evaluation evidence is temporarily unavailable"},
            status_code=500,
        )
    return JSONResponse({"runs": runs}, status_code=200)


# --- LES ROUTES, a la position exacte qu'elles occupaient -----------------
LEGACY_EVIDENCE_ROUTES = [
    Route("/api/feedback", endpoint=_list_legacy_feedback_rows, methods=["GET"]),
    Route("/api/eval/golden-questions", endpoint=_list_legacy_benchmark_questions, methods=["GET"]),
    Route("/api/eval/runs", endpoint=_list_legacy_eval_runs, methods=["GET"]),
]
