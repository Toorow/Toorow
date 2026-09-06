"""toorow -- Daily-insights inbox + team share REST API (Epic 35, Story 35.4).

Console surface for the agentic daily insights of Epic 35. Mirrors
``server/core/rendus_api.py`` (route style, Bearer auth, AD-5 non-disclosing 404) and
REUSES the existing building blocks -- it re-implements none of them:

  - store          : ``core.daily_insights`` (35.3 -- list_runs / get_run / get_insight)
  - journal/recipe : ``core.daily_insights_recipe`` (35.5 -- run_journal / build_task_recipe
                     / render_recipe_text)
  - share          : RETIRED by Story 50.7. This module was the THIRD caller of the
                     plaintext-token `snapshot_shares.create_share`, and it returned
                     `/api/rendus/shared/{token}` -- a live bearer in a URL path.
                     `POST .../insights/{insight_id}/share` now answers `410 Gone`
                     from its unchanged mount. Listing and revocation survive as the
                     legacy seam and return no token.

                     THIS LINE USED TO SAY "an insight is shared by sharing the
                     Render its publication froze", and it was false -- measured
                     2026-08-16. Publication freezes an `app.render_snapshots` row
                     (`snapshots.persist_render_envelope`); a Share opens an
                     `app.renders` row with its frozen payload
                     (`render_shares.load_frozen_render`). They are different
                     objects and nothing bridges them, which the comment at
                     `_create_insight_share`'s grave already stated correctly: a
                     Share needs one immutable `app.renders` row and there is no
                     way to create one that does not name a real Result. So a
                     published insight is NOT shareable today. Epic 35's DoD line
                     "partage fige/revocable" is OPEN.

                     THE DESIGN QUESTION IS ANSWERED (2026-08-17,
                     `proactive-assertions.md`): yes, publication must name a
                     Result -- because the alternative, a second Share path over
                     `app.render_snapshots`, is the parallel-store clause of
                     `governance.md` arriving at the Share layer. What that costs
                     is measured and is NOT wiring: `app.query_results` requires
                     an `attempt_id` and a `query_spec_version_id`, and a card is
                     built from the card catalogue rather than from a Query Spec.

                     THE QUERY SPEC BEHIND THE CARD EXISTS (same day):
                     `core.daily_insight_result` derives a governed Query Spec
                     from the insight's card contract, versions it through the
                     existing spec store and executes it through the existing
                     governed executor, so a published insight now carries
                     `query_spec_version_id` + `result_id` (migration 281) -- or
                     `result_unavailable_reason`, naming the missing link, when
                     the card cannot stand behind a spec. Sharing goes through
                     the ONE mechanism, unchanged: a Render over that Result
                     (`analyze_artifacts.create_render`), a Share over the
                     Render (`render_shares.create_share`). This module still
                     mounts NO insight-specific share creation, and must not.

Routes (console, auth Bearer, scope AD-5) :
  GET    /api/daily-insights/runs                              -- run history (journal)
  GET    /api/daily-insights/runs/{insight_date}              -- one run + its insights
  GET    /api/daily-insights/insights/{insight_id}           -- one insight (full payload)
  POST   /api/daily-insights/insights/{insight_id}/retract   -- withdraw a published claim
  POST   /api/daily-insights/insights/{insight_id}/share     -- share via snapshot lineage
  GET    /api/daily-insights/insights/{insight_id}/shares    -- list shares of an insight
  DELETE /api/daily-insights/shares/{share_id}               -- revoke a share
  GET    /api/daily-insights/recipe                           -- 35.5 task recipe + text

RETRACTION (migration 321, `proactive-assertions.md` decision 4). The one console
door that withdraws a published claim. It is an audited state transition, never a
DELETE -- the row stays and is served as withdrawn, because it is the evidence that
the claim was made. It demands `edit`, like publication: withdrawing an assertion in
someone else's project is not a read. `GET .../runs/{date}` answers `canRetract` so
the screen never draws a control the caller cannot use.

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

# Story 50.7: the `410 Gone` endpoint for the retired share-creation mount.
from core.render_shares_api import create_insight_share_gone

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


def _with_authorship(insight: dict) -> dict:
    """Garantir que l'insight rendu porte sa provenance (story 53.4, AC5/AC6).

    Le payload publie la porte depuis `daily_insights_tools.publish`. Les lignes
    ECRITES AVANT cette story ne l'ont pas, et la surface qui dessine
    `confidence` devrait alors deviner lequel des deux cas elle regarde -- c'est
    exactement la clause qu'interdit `proactive-assertions.md:162` (<< la prose
    du modele est visiblement distinguable de la donnee citee >>).

    Elle est DERIVEE, jamais fabriquee : `authorship_block` ne lit que le payload
    lui-meme et rend le meme bloc que celui persiste. Rien n'est invente, et rien
    n'est reecrit -- un payload qui la porte deja est rendu tel quel.
    """
    from core.daily_insights_schema import authorship_of  # noqa: PLC0415

    payload = insight.get("payload")
    if not isinstance(payload, dict):
        return insight
    return {**insight, "payload": {**payload, "authorship": authorship_of(payload)}}


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
        from core.project_access import identity_can_read_project  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_can_read_project(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Project not found"},
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
        from core.project_access import (  # noqa: PLC0415
            identity_can_read_project,
            identity_has_project_role,
        )

        with get_connection() as conn:
            if not identity_can_read_project(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Run non trouve"},
                    status_code=404,
                )

            run = get_run(project_id, insight_date, conn)
            can_retract = identity_has_project_role(project_id, identity, "member", conn)
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

    insights = [_with_authorship(i) for i in (run.get("insights") or [])]
    # The screen asks ONE question and gets it answered here: may this caller
    # withdraw a claim of this day? Deriving it in the console from a role it does
    # not hold would either hide the gesture from someone entitled to it or offer a
    # control that answers 404.
    return JSONResponse({"run": run_journal(run), "insights": insights, "canRetract": can_retract})


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
        from core.project_access import identity_can_read_project  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_can_read_project(project_id, identity, conn):
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

    return JSONResponse(_with_authorship(insight))


# ---------------------------------------------------------------------------
# POST /api/daily-insights/insights/{insight_id}/retract
# ---------------------------------------------------------------------------


async def _retract_insight(request: Request) -> Response:
    """POST /api/daily-insights/insights/{insight_id}/retract -- retirer une assertion.

    Transition d'etat AUDITEE, jamais un DELETE (`proactive-assertions.md`,
    decision 4 ; migration 321). La ligne reste : elle est la preuve que la
    revendication a ete faite, et la detruire perdrait la difference entre
    << jamais dit >> et << dit, puis retire >>.

    Body JSON : {"reason": "..."} -- REQUIS. Une retractation sans raison est celle
    que personne ne peut juger plus tard.

    Query params :
        project_id  (required) -- scope AD-5

    Reponse 200 : l'insight retracte (avec retracted_at / retracted_by / retracted_reason).
    Reponse 404 : insight introuvable, projet introuvable ou acces refuse (non-disclosant).
    Reponse 409 : deja retracte -- une retractation ne se defait pas.
    Reponse 422 : raison manquante.
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
        body = await request.json()
    except Exception:  # noqa: BLE001 -- a malformed body is a missing reason
        body = {}
    reason = str((body or {}).get("reason") or "")

    try:
        from core.daily_insights import (  # noqa: PLC0415
            DailyInsightRefusal,
            retract_insight,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_project_role  # noqa: PLC0415

        with get_connection() as conn:
            # `member` (= capability `edit`), the same floor publication demands.
            # Withdrawing an assertion is a WRITE on a durable artifact, and a
            # read-only holder may not make one in the neighbour's project. The
            # refusal is the non-disclosing 404, like every other door here.
            if not identity_has_project_role(project_id, identity, "member", conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Insight non trouve"},
                    status_code=404,
                )

            retracted = retract_insight(
                project_id=project_id,
                insight_id=insight_id,
                retracted_by=identity,
                reason=reason,
                conn=conn,
            )
    except DailyInsightRefusal as refusal:
        return JSONResponse(
            {"code": refusal.code, "message": refusal.message},
            status_code=refusal.status,
        )
    except Exception as exc:
        logger.error(
            "daily_insights_api: retract err insight=%s p=%s: %s",
            insight_id,
            project_id,
            exc,
        )
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    return JSONResponse(_with_authorship(retracted))


# ---------------------------------------------------------------------------
# POST /api/daily-insights/insights/{insight_id}/share
# ---------------------------------------------------------------------------


# `_create_insight_share` is DELETED by Story 50.7. Its `409 no_snapshot_lineage`
# branch was honest and is worth keeping in spirit -- an insight with no frozen
# Render is not shareable, and saying so beats fabricating one. The replacement says
# the same thing at the Render level: a Share needs one immutable `app.renders` row,
# and there is no way to create one that does not name a real Result.


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
        from core.project_access import identity_can_read_project  # noqa: PLC0415
        from core.snapshot_shares import list_shares  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_can_read_project(project_id, identity, conn):
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
        from core.project_access import identity_can_read_project  # noqa: PLC0415
        from core.snapshot_shares import revoke_share  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_can_read_project(project_id, identity, conn):
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
            {"code": "invalid_params", "message": "hour must be an integer 0..23"},
            status_code=400,
        )
    if not (0 <= hour <= 23):
        return JSONResponse(
            {"code": "invalid_params", "message": "hour must be within [0, 23]"},
            status_code=400,
        )

    try:
        from core.daily_insights_recipe import (  # noqa: PLC0415
            build_task_recipe,
            render_recipe_text,
        )
        from core.daily_insights_schema import SCHEMA_VERSION  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_can_read_project  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_can_read_project(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Project not found"},
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
    # Story 50.7 AC10: this mount STAYS at this exact path and method; only its
    # endpoint changes. Nothing is removed from this list -- it mounts no raw-token
    # public route, so there is nothing here whose absence AC10 requires.
    Route(
        "/api/daily-insights/insights/{insight_id}/share",
        endpoint=create_insight_share_gone,
        methods=["POST"],
    ),
    # Migration 321: the ONE door that withdraws a published claim. It is a POST
    # and not a DELETE, and that is the decision rather than a REST habit -- the
    # verb a caller reaches for is the verb they get, and there is no path here
    # that removes the row.
    Route(
        "/api/daily-insights/insights/{insight_id}/retract",
        endpoint=_retract_insight,
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
