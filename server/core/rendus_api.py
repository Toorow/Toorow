"""toorow -- Galerie des rendus REST API (Story 13.5 volets a et b).

Routes console (auth Bearer, scope AD-5) :
  GET    /api/rendus/snapshots                       -- liste paginee (galerie)
  GET    /api/rendus/snapshots/{snapshot_id}         -- envelope complete (rouvrir widget)
  DELETE /api/rendus/snapshots/{snapshot_id}         -- suppression (optionnelle)
  POST   /api/rendus/snapshots/{snapshot_id}/share   -- creer un partage (volet b, O1)
  GET    /api/rendus/snapshots/{snapshot_id}/shares  -- lister les partages
  DELETE /api/rendus/shares/{share_id}               -- revoquer un partage

Route publique (sans auth, rate-limitee) :

Auth  : meme _check_auth que core.admin_api (Bearer token via core.api_auth).
AD-5  : project_id scope enforced ; 404 non-disclosant pour tout acces invalide.
AD-8  : la console communique exclusivement via cette couche REST.
AD-9  : l'envelope est renvoyee telle quelle (fraicheur/provenance intactes).
O1    : le endpoint public ne touche QUE render_snapshot_shares + render_snapshots.
        Aucun re-run, aucun acces aux marts/projet (AD-20 ratifie).

ASCII-only stdout (L-3). Copie FR accentuee dans les messages d'erreur.
"""

from __future__ import annotations

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import declare_action

# Story 50.7: the `410 Gone` endpoint for the retired share-creation mount. It
# lives in the module that owns the replacement, so the retirement names its
# successor in exactly one place.
from core.render_shares_api import create_snapshot_share_gone

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
# `shared` n a plus d ecrivain depuis que la story 50.7 a retire le chemin de
# partage ; sa declaration reste ICI, a cote de son frere, plutot que dans le
# carrefour -- une absence se lit a cote de ce qui l explique.
ACTION_SNAPSHOT_SHARED = declare_action("render_snapshot.shared")
ACTION_SNAPSHOT_SHARE_REVOKED = declare_action("render_snapshot.share_revoked")


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate-limit pour le endpoint public partage (replique du pattern Story 6.6).
# 60 req/min/(IP, token_prefix) + plafond global 120/min/token.
# TODO(Phase-B) : deplacer vers Redis pour multi-replica.
# ---------------------------------------------------------------------------

# Story 50.7: the in-memory rate-limit buckets are REMOVED with the public
# endpoint that was their only consumer. They were keyed on `token[:8]`.


def _json_for_script(obj) -> str:
    """Serialiser un objet en JSON sur pour injection dans un bloc <script>.

    json.dumps seul ne protege pas contre le breakout </script> ni contre les
    separateurs de ligne Unicode U+2028/U+2029 qui terminent un litteral JS.
    Cette fonction echappe les trois sequences dangereuses apres serialisation :
      - "</"  ->  "<\\/"   (casse le tag </script> sans changer la valeur JS)
      - U+2028 ->  "\\u2028"
      - U+2029 ->  "\\u2029"
    Le resultat reste du JSON/JS valide ; <\\/ est reconnu par tous les parseurs JS.
    A appliquer sur TOUS les champs injectes dans le bloc <script> de l'endpoint
    public (O1), qu'ils proviennent de donnees tiers (enveloppes, noms de campagnes)
    ou de metadonnees internes.
    """
    return (
        json.dumps(obj)
        .replace("</", "<\\/")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


# `_check_rendus_rate_limit` is REMOVED by Story 50.7. It keyed its buckets on
# `token[:8]` -- eight characters of a live plaintext bearer, held in process
# memory and therefore in every heap dump. Its only caller was the public
# `/api/rendus/shared/{token}` endpoint, which is removed with it. The replacement
# keys on the bearer's HMAC prefix: `core.render_shares.check_rate_limit`.


# ---------------------------------------------------------------------------
# Auth helper (delegue a core.admin_api)
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Deleguer a la couche d'auth partagee."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _authorize_snapshot_project(
    identity: str,
    project_id: str,
    conn,
    *,
    minimum_capability: str,
) -> bool:
    """Resolve project access through the strict organization-rooted seam.

    Auth-disabled self-host mode has one explicit compatibility subject:
    ``anonymous``, representing the single local operator. A named bearer never
    inherits that bypass. Authenticated modes require active organization
    membership and an explicit resource grant, even when legacy project
    membership rows are absent.
    """
    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if auth_mode == "disabled":
        return identity == "anonymous"

    try:
        # The connection arrives armed from `request_connection`; arming it
        # again here would be the double this story removes.
        from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

        decision = resolve_strict_resource_access(
            identity,
            conn,
            project_id=project_id,
            minimum_capability=minimum_capability,
            auth_mode=auth_mode,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "snapshot_project_access_resolution_failed project_id=%s error=%s",
            project_id,
            exc,
        )
        return False
    return bool(decision.allowed)


# ---------------------------------------------------------------------------
# GET /api/rendus/snapshots
# ---------------------------------------------------------------------------


async def _list_snapshots(request: Request) -> Response:
    """GET /api/rendus/snapshots -- liste la galerie des rendus pour un projet.

    Query params :
        project_id  (required) -- scope AD-5
        limit       (optional, defaut 50, max 200)
        offset      (optional, defaut 0)
        tool_name   (optional) -- filtre 'get_card' ou 'get_report'

    Reponse 200 :
        {
          "snapshots": [
            {
              "id": "rsn_...",
              "project_id": "...",
              "tool_name": "get_card",
              "tool_args": {...},
              "widget_uri": "...",
              "summary_snippet": "...",
              "question": "...",
              "identity": "...",
              "trace_id": "...",
              "created_at": "2026-07-21T...",
              "meta": {...}    -- meta de l'envelope (freshness, provenance, alerts)
            },
            ...
          ],
          "total": N
        }
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
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
    tool_name = request.query_params.get("tool_name") or None

    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.snapshots import list_render_snapshots  # noqa: PLC0415

        with request_connection(identity) as conn:
            # AD-5 : verifier l'acces au projet (404 non-disclosant).
            if not _authorize_snapshot_project(
                identity,
                project_id,
                conn,
                minimum_capability="view",
            ):
                return JSONResponse(
                    {"code": "not_found", "message": "Project not found"},
                    status_code=404,
                )

            snapshots = list_render_snapshots(
                project_id,
                conn,
                limit=limit,
                offset=offset,
                tool_name=tool_name,
            )
    except Exception as exc:
        logger.error("rendus_api: list_snapshots_error project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    return JSONResponse({"snapshots": snapshots, "total": len(snapshots)})


# ---------------------------------------------------------------------------
# GET /api/rendus/snapshots/{snapshot_id}
# ---------------------------------------------------------------------------


async def _get_snapshot(request: Request) -> Response:
    """GET /api/rendus/snapshots/{snapshot_id} -- envelope complete pour rouvrir le widget.

    Query params :
        project_id  (required) -- scope AD-5

    Reponse 200 :
        {
          "id": "rsn_...",
          "project_id": "...",
          "tool_name": "...",
          "tool_args": {...},
          "envelope": {...},     -- envelope AD-1 complete (gelee au moment du rendu)
          "widget_uri": "...",
          "summary_snippet": "...",
          "question": "...",
          "identity": "...",
          "trace_id": "...",
          "created_at": "..."
        }

    La reouverture du widget cote console injecte l'envelope dans
    window.__MCP_STRUCTURED_CONTENT__ et charge le shell (mcpApp.readInjectedEnvelope).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    snapshot_id = request.path_params.get("snapshot_id", "").strip()
    project_id = (request.query_params.get("project_id") or "").strip()

    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.snapshots import get_render_snapshot  # noqa: PLC0415

        with request_connection(identity) as conn:
            # AD-5 : verifier l'acces au projet (404 non-disclosant).
            if not _authorize_snapshot_project(
                identity,
                project_id,
                conn,
                minimum_capability="view",
            ):
                return JSONResponse(
                    {"code": "not_found", "message": "Snapshot non trouve"},
                    status_code=404,
                )

            snapshot = get_render_snapshot(snapshot_id, project_id, conn)

    except Exception as exc:
        logger.error("rendus_api get_snapshot err snap=%s p=%s: %s", snapshot_id, project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    if snapshot is None:
        return JSONResponse(
            {"code": "not_found", "message": "Snapshot non trouve"},
            status_code=404,
        )

    return JSONResponse(snapshot)


# ---------------------------------------------------------------------------
# DELETE /api/rendus/snapshots/{snapshot_id}
# ---------------------------------------------------------------------------


async def _delete_snapshot(request: Request) -> Response:
    """DELETE /api/rendus/snapshots/{snapshot_id} -- supprimer un snapshot.

    Query params :
        project_id  (required) -- scope AD-5

    Reponse 204 : suppression reussie.
    Reponse 404 : snapshot introuvable ou acces refuse.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    snapshot_id = request.path_params.get("snapshot_id", "").strip()
    project_id = (request.query_params.get("project_id") or "").strip()

    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.snapshots import delete_render_snapshot  # noqa: PLC0415

        with request_connection(identity) as conn:
            # AD-5 : verifier l'acces au projet (404 non-disclosant).
            if not _authorize_snapshot_project(
                identity,
                project_id,
                conn,
                minimum_capability="manage",
            ):
                return JSONResponse(
                    {"code": "not_found", "message": "Snapshot non trouve"},
                    status_code=404,
                )

            deleted = delete_render_snapshot(snapshot_id, project_id, conn)

    except Exception as exc:
        logger.error("rendus_api delete err snap=%s p=%s: %s", snapshot_id, project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    if not deleted:
        return JSONResponse(
            {"code": "not_found", "message": "Snapshot non trouve"},
            status_code=404,
        )

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# POST /api/rendus/snapshots/{snapshot_id}/share  (volet b, O1)
# ---------------------------------------------------------------------------


# `_create_share` is REMOVED by Story 50.7. It minted a PLAINTEXT 192-bit token,
# stored it in the clear, returned it in the body AND in a `share_url`, and wrote
# its audit row best-effort AFTER the grant had already committed -- so a share
# that was created but not audited was indistinguishable from one that was never
# created. Its mount stays at the same path and method and now answers `410 Gone`
# through `core.render_shares_api.create_snapshot_share_gone`.
#
# The replacement is `core.render_shares.create_share`: a 256-bit bearer stored
# only as a peppered HMAC, a mandatory expiry, and creation routed through
# `core.operations.execute_operation` so the grant, the audit row and the outbox
# commit together or not at all.


# ---------------------------------------------------------------------------
# GET /api/rendus/snapshots/{snapshot_id}/shares
# ---------------------------------------------------------------------------


async def _list_shares(request: Request) -> Response:
    """GET /api/rendus/snapshots/{snapshot_id}/shares -- liste les partages du snapshot.

    Query params :
        project_id   (required) -- scope AD-5
        active_only  (optional) -- 'true' pour ne lister que les partages actifs

    Reponse 200 :
        {
          "shares": [
            {
              "id": "rss_...",
              "snapshot_id": "rsn_...",
              "shared_at": "...",
              "shared_by": "...",
              "revoked_at": null | "..."
            },
            ...
          ]
        }
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    snapshot_id = request.path_params.get("snapshot_id", "").strip()
    project_id = (request.query_params.get("project_id") or "").strip()
    active_only = request.query_params.get("active_only", "").lower() == "true"

    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.snapshot_shares import list_shares  # noqa: PLC0415

        with request_connection(identity) as conn:
            if not _authorize_snapshot_project(
                identity,
                project_id,
                conn,
                minimum_capability="view",
            ):
                return JSONResponse(
                    {"code": "not_found", "message": "Snapshot non trouve"},
                    status_code=404,
                )

            shares = list_shares(snapshot_id, project_id, conn, active_only=active_only)

    except Exception as exc:
        logger.error("rendus_api: list_shares err snap=%s p=%s: %s", snapshot_id, project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    # Story 50.7 AC10: this listing survives as the LEGACY revocation-and-history
    # seam, and it returns NO token and NO URL. `core.snapshot_shares.list_shares`
    # no longer selects `share_token` at all, and the `share_url` enrichment that
    # used to run here is deleted: a URL built from a stored plaintext token is a
    # live public grant, and putting one in a console response puts it in every
    # screenshot and every browser cache.
    return JSONResponse({"shares": shares, "legacy": True})


# ---------------------------------------------------------------------------
# DELETE /api/rendus/shares/{share_id}
# ---------------------------------------------------------------------------


async def _revoke_share(request: Request) -> Response:
    """DELETE /api/rendus/shares/{share_id} -- revoquer un partage.

    Query params :
        project_id  (required) -- scope AD-5

    Reponse 204 : revocation reussie.
    Reponse 404 : introuvable ou deja revoque (non-disclosant).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis"},
            status_code=401,
        )

    share_id = request.path_params.get("share_id", "").strip()
    project_id = (request.query_params.get("project_id") or "").strip()

    if not project_id:
        return JSONResponse(
            {"code": "invalid_params", "message": "project_id requis"},
            status_code=422,
        )

    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.snapshot_shares import revoke_share  # noqa: PLC0415

        with request_connection(identity) as conn:
            if not _authorize_snapshot_project(
                identity,
                project_id,
                conn,
                minimum_capability="manage",
            ):
                return JSONResponse(
                    {"code": "not_found", "message": "Partage non trouve"},
                    status_code=404,
                )

            revoked = revoke_share(share_id, project_id, conn)

    except Exception as exc:
        logger.error("rendus_api: revoke_share err share=%s p=%s: %s", share_id, project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    if not revoked:
        return JSONResponse(
            {"code": "not_found", "message": "Partage non trouve"},
            status_code=404,
        )

    # Audit (best-effort).
    try:
        from core.audit import write_audit_row  # noqa: PLC0415

        write_audit_row(
            identity=identity,
            action=ACTION_SNAPSHOT_SHARE_REVOKED,
            provider_account="rendus",
            connection_ref="",
            metadata={"share_id": share_id, "project_id": project_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("rendus_api: audit revoke err: %s", exc)

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# GET /api/rendus/shared/{token}  -- endpoint PUBLIC rate-limite (O1)
# ---------------------------------------------------------------------------

# Template HTML single-file : injecte l'envelope gelee dans
# window.__MCP_STRUCTURED_CONTENT__ pour que le shell widget (mcpApp.ts
# readInjectedEnvelope) puisse la lire sans hote MCP.
# AD-9 : "Partage le <shared_at>" honnete + stale_since = freshness figee.
# O1 strict : aucun re-run, aucun acces live.

# ---------------------------------------------------------------------------
# REMOVED BY STORY 50.7: the public share page and its endpoint.
#
# `_SHARED_HTML_TEMPLATE`, `_fmt_date_fr` and `_shared_snapshot_endpoint` are gone.
# What they did, recorded here because the absence is the inventory:
#
#   * The page was `lang="fr"` with French copy ("Rendu partage", "Partage le",
#     "Fraicheur figee"). `analyze-and-test.md` fixes the English Level 2 name
#     "Renders"; the French route segment and label are not reintroduced.
#   * It injected the envelope into `window.__MCP_STRUCTURED_CONTENT__` and MOUNTED
#     NOTHING: the `<div id="widget-mount"></div>` stayed empty because the page
#     loaded no script bundle at all -- only the inline injection. That is the gap
#     `visualization-and-rendering.md:351` names. Verified again on 2026-07-31
#     before deleting it.
#   * Its headers were `X-Frame-Options: SAMEORIGIN` and a CSP carrying
#     `script-src 'unsafe-inline'`, with no `Cache-Control: no-store`, no
#     `Referrer-Policy: no-referrer` and no `frame-ancestors`.
#   * It logged `token[:8]` at `logger.error` and persisted `"token_prefix": token[:8]`
#     into the audit spine -- eight characters of a live bearer, in two durable places.
#   * And the token was in the URL PATH, so every access log held a live bearer
#     before any application code ran. No handler-level redaction can reach that,
#     which is why the route is removed rather than hardened.
#
# Replaced by `GET /share` + `POST /api/render-shares/exchange` + `GET /share/view`
# in `core.render_shares_api`, which mounts the Story 50.5 runtime over one frozen
# Render. AC10 requires NO ROUTE TO MATCH `/api/rendus/shared/{token}`, so its
# `Route(...)` entry is deleted below rather than repointed -- a `410` there would
# still be a route, and the requirement is absence.
# ---------------------------------------------------------------------------


RENDUS_ROUTES: list[Route] = [
    # Routes statiques AVANT les routes parametrisees (Starlette matching order).
    # Story 50.7 AC10: the raw-token public read is REMOVED, not repointed. AC10
    # requires NO route to match "/api/rendus/shared/{token}"; a 410 handler here
    # would still be a route. Absence is the statement.
    # Console : suppression d'un partage (share_id dans path).
    Route("/api/rendus/shares/{share_id}", endpoint=_revoke_share, methods=["DELETE"]),
    # Console : galerie + CRUD snapshots.
    Route("/api/rendus/snapshots", endpoint=_list_snapshots, methods=["GET"]),
    # Story 50.7 AC10: this mount STAYS at this exact path and method, and only its
    # endpoint changes. Deleting the entry would answer 405 (or a fall-through 404),
    # which is a different statement and is indistinguishable to a client from a
    # routing regression. 410 says "this existed and was retired".
    Route(
        "/api/rendus/snapshots/{snapshot_id}/share",
        endpoint=create_snapshot_share_gone,
        methods=["POST"],
    ),
    Route("/api/rendus/snapshots/{snapshot_id}/shares", endpoint=_list_shares, methods=["GET"]),
    Route("/api/rendus/snapshots/{snapshot_id}", endpoint=_get_snapshot, methods=["GET"]),
    Route("/api/rendus/snapshots/{snapshot_id}", endpoint=_delete_snapshot, methods=["DELETE"]),
]
