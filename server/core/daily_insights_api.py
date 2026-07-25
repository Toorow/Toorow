"""toorow -- Daily-insights inbox + team share REST API (Epic 35, Story 35.4).

Console surface for the agentic daily insights of Epic 35. Mirrors
``server/core/rendus_api.py`` (route style, Bearer auth, AD-5 non-disclosing 404) and
REUSES the existing building blocks -- it re-implements none of them:

  - store          : ``core.daily_insights`` (35.3 -- list_runs / get_run / get_insight)
  - journal/recipe : ``core.daily_insights_recipe`` (35.5 -- run_journal / build_task_recipe
                     / render_recipe_text)
  - share          : ``core.snapshot_shares`` on ``app.render_snapshot_shares`` (migration
                     054), via each insight's ``render_snapshot_id`` lineage. NO new share
                     table, NO new migration, NO new public route: the existing public
                     ``/api/rendus/shared/{token}`` serves the frozen card.

Routes (console, auth Bearer, scope AD-5) :
  GET    /api/daily-insights/runs                              -- run history (journal)
  GET    /api/daily-insights/runs/{insight_date}              -- one run + its insights
  GET    /api/daily-insights/insights/{insight_id}           -- one insight (full payload)
  POST   /api/daily-insights/insights/{insight_id}/share     -- share via snapshot lineage
  GET    /api/daily-insights/insights/{insight_id}/shares    -- list shares of an insight
  DELETE /api/daily-insights/shares/{share_id}               -- revoke a share
  GET    /api/daily-insights/recipe                           -- 35.5 task recipe + text

Auth  : same ``_check_auth`` as ``core.rendus_api`` (delegates to ``core.admin_api``).
AD-5  : ``project_id`` scope enforced ; non-disclosing 404 for any invalid access.
Frozen open : this layer NEVER queries the warehouse. It reads the persisted daily-insight
        rows and the frozen render_snapshot lineage only. The recipe is a pure function.

ASCII-only stdout (L-3).
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth helper (delegates to core.admin_api, same as core.rendus_api)
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Deleguer a la couche d'auth partagee."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _require_project_id(request: Request) -> str:
    """Extraire project_id de la query (chaine vide si absent)."""
    return (request.query_params.get("project_id") or "").strip()


# ---------------------------------------------------------------------------
# GET /api/daily-insights/runs
# ---------------------------------------------------------------------------


async def _list_runs(request: Request) -> Response:
    """GET /api/daily-insights/runs -- historique des runs (journal 35.5).

    Query params :
        project_id  (required) -- scope AD-5
        limit       (optional, defaut 50, borne 1..200)
        offset      (optional, defaut 0)

    Reponse 200 : {"runs": [run_journal(r), ...], "total": N}
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    project_id = _require_project_id(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    try:
        limit = int(request.query_params.get("limit", "50"))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(request.query_params.get("offset", "0"))
    except (ValueError, TypeError):
        offset = 0

    try:
        from core.daily_insights import list_runs  # noqa: PLC0415
        from core.daily_insights_recipe import run_journal  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_project_access  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_project_access(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Projet non trouve"},
                    status_code=404,
                )

            runs = list_runs(project_id, conn, limit=limit, offset=offset)
    except Exception as exc:
        logger.error("daily_insights_api: list_runs_error project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    journals = [run_journal(r) for r in runs]
    return JSONResponse({"runs": journals, "total": len(journals)})


# ---------------------------------------------------------------------------
# GET /api/daily-insights/runs/{insight_date}
# ---------------------------------------------------------------------------


async def _get_run(request: Request) -> Response:
    """GET /api/daily-insights/runs/{insight_date} -- un run + ses insights.

    Query params :
        project_id  (required) -- scope AD-5

    Reponse 200 : {"run": run_journal(run), "insights": run["insights"]}
    Reponse 404 : run absent pour (project_id, insight_date) ou acces refuse.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    insight_date = request.path_params.get("insight_date", "").strip()
    project_id = _require_project_id(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    try:
        from core.daily_insights import get_run  # noqa: PLC0415
        from core.daily_insights_recipe import run_journal  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_project_access  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_project_access(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Run non trouve"},
                    status_code=404,
                )

            run = get_run(project_id, insight_date, conn)
    except Exception as exc:
        logger.error(
            "daily_insights_api: get_run err date=%s p=%s: %s", insight_date, project_id, exc
        )
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    if run is None:
        return JSONResponse(
            {"code": "not_found", "message": "Run non trouve"},
            status_code=404,
        )

    return JSONResponse({"run": run_journal(run), "insights": run.get("insights") or []})


# ---------------------------------------------------------------------------
# GET /api/daily-insights/insights/{insight_id}
# ---------------------------------------------------------------------------


async def _get_insight(request: Request) -> Response:
    """GET /api/daily-insights/insights/{insight_id} -- un insight (payload complet).

    Query params :
        project_id  (required) -- scope AD-5

    Reponse 200 : l'insight complet (dict get_insight).
    Reponse 404 : insight introuvable ou acces refuse (non-disclosant).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    insight_id = request.path_params.get("insight_id", "").strip()
    project_id = _require_project_id(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    try:
        from core.daily_insights import get_insight  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_project_access  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_project_access(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Insight non trouve"},
                    status_code=404,
                )

            insight = get_insight(insight_id, project_id, conn)
    except Exception as exc:
        logger.error(
            "daily_insights_api: get_insight err insight=%s p=%s: %s",
            insight_id,
            project_id,
            exc,
        )
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    if insight is None:
        return JSONResponse(
            {"code": "not_found", "message": "Insight non trouve"},
            status_code=404,
        )

    return JSONResponse(insight)


# ---------------------------------------------------------------------------
# POST /api/daily-insights/insights/{insight_id}/share
# ---------------------------------------------------------------------------


async def _create_insight_share(request: Request) -> Response:
    """POST /api/daily-insights/insights/{insight_id}/share -- partager via le snapshot.

    Resout l'insight (AD-5), puis reutilise ``snapshot_shares.create_share`` sur son
    ``render_snapshot_id``. Un insight sans lignee de snapshot n'est pas partageable :
    409 actionnable (le follow-up 35.2 persiste le snapshot du rendu fige).

    Query params :
        project_id  (required) -- scope AD-5

    Reponse 201 : {"share_id", "token", "url": "/api/rendus/shared/{token}"}
    Reponse 409 : {"code": "no_snapshot_lineage", ...} -- pas de render_snapshot_id.
    Reponse 404 : insight introuvable ou acces refuse (non-disclosant).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    insight_id = request.path_params.get("insight_id", "").strip()
    project_id = _require_project_id(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    try:
        from core.daily_insights import get_insight  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_project_access  # noqa: PLC0415
        from core.snapshot_shares import create_share  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_project_access(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Insight non trouve"},
                    status_code=404,
                )

            insight = get_insight(insight_id, project_id, conn)
            if insight is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Insight non trouve"},
                    status_code=404,
                )

            snapshot_id = insight.get("render_snapshot_id")
            if not snapshot_id:
                return JSONResponse(
                    {
                        "code": "no_snapshot_lineage",
                        "message": (
                            "Cet insight ne porte pas encore de rendu partageable "
                            "(render_snapshot_id absent) ; la publication doit d'abord "
                            "figer un snapshot du rendu."
                        ),
                    },
                    status_code=409,
                )

            result = create_share(snapshot_id, project_id, identity, conn)
    except Exception as exc:
        logger.error(
            "daily_insights_api: create_share err insight=%s p=%s: %s",
            insight_id,
            project_id,
            exc,
        )
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    if result is None:
        # Le snapshot n'existe pas ou n'appartient pas au projet (non-disclosant).
        return JSONResponse(
            {"code": "not_found", "message": "Insight non trouve"},
            status_code=404,
        )

    share_id, token = result
    return JSONResponse(
        {
            "share_id": share_id,
            "token": token,
            "url": f"/api/rendus/shared/{token}",
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# GET /api/daily-insights/insights/{insight_id}/shares
# ---------------------------------------------------------------------------


async def _list_insight_shares(request: Request) -> Response:
    """GET /api/daily-insights/insights/{insight_id}/shares -- partages de l'insight.

    Resout l'insight (AD-5) puis liste les partages de son ``render_snapshot_id``.
    Un insight sans lignee de snapshot renvoie une liste vide (aucun partage possible).

    Query params :
        project_id   (required) -- scope AD-5
        active_only  (optional) -- 'true' pour ne lister que les partages actifs

    Reponse 200 : {"shares": [...]}
    Reponse 404 : insight introuvable ou acces refuse (non-disclosant).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    insight_id = request.path_params.get("insight_id", "").strip()
    project_id = _require_project_id(request)
    active_only = request.query_params.get("active_only", "").lower() == "true"
    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    try:
        from core.daily_insights import get_insight  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_project_access  # noqa: PLC0415
        from core.snapshot_shares import list_shares  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_project_access(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Insight non trouve"},
                    status_code=404,
                )

            insight = get_insight(insight_id, project_id, conn)
            if insight is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Insight non trouve"},
                    status_code=404,
                )

            snapshot_id = insight.get("render_snapshot_id")
            if not snapshot_id:
                shares: list[dict] = []
            else:
                shares = list_shares(snapshot_id, project_id, conn, active_only=active_only)
    except Exception as exc:
        logger.error(
            "daily_insights_api: list_shares err insight=%s p=%s: %s",
            insight_id,
            project_id,
            exc,
        )
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    return JSONResponse({"shares": shares})


# ---------------------------------------------------------------------------
# DELETE /api/daily-insights/shares/{share_id}
# ---------------------------------------------------------------------------


async def _revoke_insight_share(request: Request) -> Response:
    """DELETE /api/daily-insights/shares/{share_id} -- revoquer un partage.

    Reutilise ``snapshot_shares.revoke_share`` (scope AD-5, historique conserve).

    Query params :
        project_id  (required) -- scope AD-5

    Reponse 200 : {"revoked": bool}
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    share_id = request.path_params.get("share_id", "").strip()
    project_id = _require_project_id(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_project_access  # noqa: PLC0415
        from core.snapshot_shares import revoke_share  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_project_access(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Partage non trouve"},
                    status_code=404,
                )

            revoked = revoke_share(share_id, project_id, conn)
    except Exception as exc:
        logger.error(
            "daily_insights_api: revoke_share err share=%s p=%s: %s",
            share_id,
            project_id,
            exc,
        )
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    return JSONResponse({"revoked": bool(revoked)})


# ---------------------------------------------------------------------------
# GET /api/daily-insights/recipe  (35.5 exposure)
# ---------------------------------------------------------------------------


async def _get_recipe(request: Request) -> Response:
    """GET /api/daily-insights/recipe -- recette de tache 35.5 + texte copiable.

    Pure : ne touche NI la base NI l'entrepot. AD-5 reste applique (l'appelant doit
    avoir acces au projet) meme si la recette est deterministe.

    Query params :
        project_id  (required) -- scope AD-5
        timezone    (optional, defaut "Europe/Paris")
        hour        (optional, defaut 7, borne 0..23)

    Reponse 200 : {"recipe": {...}, "text": "..."}
    Reponse 400 : hour hors bornes ou non-entier.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    project_id = _require_project_id(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    timezone = (request.query_params.get("timezone") or "Europe/Paris").strip() or "Europe/Paris"
    hour_raw = request.query_params.get("hour", "7")
    try:
        hour = int(hour_raw)
    except (ValueError, TypeError):
        return JSONResponse(
            {"code": "invalid_params", "message": "hour doit etre un entier 0..23"},
            status_code=400,
        )
    if not (0 <= hour <= 23):
        return JSONResponse(
            {"code": "invalid_params", "message": "hour doit etre dans [0, 23]"},
            status_code=400,
        )

    try:
        from core.daily_insights_recipe import (  # noqa: PLC0415
            build_task_recipe,
            render_recipe_text,
        )
        from core.daily_insights_schema import SCHEMA_VERSION  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_project_access  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_project_access(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Projet non trouve"},
                    status_code=404,
                )

        recipe = build_task_recipe(
            project_id=project_id,
            timezone=timezone,
            hour_local=hour,
            contract_version=SCHEMA_VERSION,
        )
        text = render_recipe_text(recipe)
    except ValueError as exc:
        return JSONResponse(
            {"code": "invalid_params", "message": str(exc)},
            status_code=400,
        )
    except Exception as exc:
        logger.error("daily_insights_api: recipe err project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    return JSONResponse({"recipe": recipe, "text": text})


# ---------------------------------------------------------------------------
# Route list (spliced into admin_api.router)
# ---------------------------------------------------------------------------

DAILY_INSIGHTS_ROUTES: list[Route] = [
    # Routes statiques AVANT les routes parametrisees (Starlette matching order).
    Route("/api/daily-insights/recipe", endpoint=_get_recipe, methods=["GET"]),
    Route(
        "/api/daily-insights/shares/{share_id}",
        endpoint=_revoke_insight_share,
        methods=["DELETE"],
    ),
    Route("/api/daily-insights/runs", endpoint=_list_runs, methods=["GET"]),
    Route(
        "/api/daily-insights/insights/{insight_id}/share",
        endpoint=_create_insight_share,
        methods=["POST"],
    ),
    Route(
        "/api/daily-insights/insights/{insight_id}/shares",
        endpoint=_list_insight_shares,
        methods=["GET"],
    ),
    Route("/api/daily-insights/insights/{insight_id}", endpoint=_get_insight, methods=["GET"]),
    Route("/api/daily-insights/runs/{insight_date}", endpoint=_get_run, methods=["GET"]),
]
