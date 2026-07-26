"""toorow -- Galerie des rendus REST API (Story 13.5 volets a et b).

Routes console (auth Bearer, scope AD-5) :
  GET    /api/rendus/snapshots                       -- liste paginee (galerie)
  GET    /api/rendus/snapshots/{snapshot_id}         -- envelope complete (rouvrir widget)
  DELETE /api/rendus/snapshots/{snapshot_id}         -- suppression (optionnelle)
  POST   /api/rendus/snapshots/{snapshot_id}/share   -- creer un partage (volet b, O1)
  GET    /api/rendus/snapshots/{snapshot_id}/shares  -- lister les partages
  DELETE /api/rendus/shares/{share_id}               -- revoquer un partage

Route publique (sans auth, rate-limitee) :
  GET    /api/rendus/shared/{token}                  -- snapshot fige HTML (O1)

Auth  : meme _check_auth que core.admin_api (Bearer token via core.api_auth).
AD-5  : project_id scope enforced ; 404 non-disclosant pour tout acces invalide.
AD-8  : la console communique exclusivement via cette couche REST.
AD-9  : l'envelope est renvoyee telle quelle (fraicheur/provenance intactes).
O1    : le endpoint public ne touche QUE render_snapshot_shares + render_snapshots.
        Aucun re-run, aucun acces aux marts/projet (AD-20 ratifie).

ASCII-only stdout (L-3). Copie FR accentuee dans les messages d'erreur.
"""

from __future__ import annotations

import html as _html_mod
import json
import logging
import os
import time

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate-limit pour le endpoint public partage (replique du pattern Story 6.6).
# 60 req/min/(IP, token_prefix) + plafond global 120/min/token.
# TODO(Phase-B) : deplacer vers Redis pour multi-replica.
# ---------------------------------------------------------------------------

_shared_rendus_rate: dict[str, tuple[int, float]] = {}
_SHARED_RATE_LIMIT = 60
_SHARED_TOKEN_RATE_LIMIT = 120
_SHARED_RATE_WINDOW = 60.0


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


def _check_rendus_rate_limit(ip: str, token: str = "") -> tuple[bool, float]:
    """Retourner (dans_limite, retry_after_seconds).

    Cle sur (client_host, token_prefix[:8]) pour eviter le contournement XFF.
    Plafond global par token pour que les IPs rotatives restent limitees.

    M1 : purge des entrees expirees au debut de chaque appel pour eviter la
    croissance illimitee du dict (DoS par IPs ou tokens rotatifs).
    """
    ts = time.monotonic()
    token_prefix = token[:8] if token else ""

    # Purge des entrees dont la fenetre est ecoulee (M1).
    expired_keys = [k for k, (_, w) in _shared_rendus_rate.items() if ts - w >= _SHARED_RATE_WINDOW]
    for k in expired_keys:
        _shared_rendus_rate.pop(k, None)

    ip_key = f"ip::{ip}::{token_prefix}"
    ip_count, ip_window = _shared_rendus_rate.get(ip_key, (0, ts))
    if ts - ip_window >= _SHARED_RATE_WINDOW:
        ip_count, ip_window = 0, ts
    if ip_count >= _SHARED_RATE_LIMIT:
        retry_after = _SHARED_RATE_WINDOW - (ts - ip_window)
        return False, max(retry_after, 1.0)
    _shared_rendus_rate[ip_key] = (ip_count + 1, ip_window)

    if token_prefix:
        tok_key = f"tok::{token_prefix}"
        tok_count, tok_window = _shared_rendus_rate.get(tok_key, (0, ts))
        if ts - tok_window >= _SHARED_RATE_WINDOW:
            tok_count, tok_window = 0, ts
        if tok_count >= _SHARED_TOKEN_RATE_LIMIT:
            retry_after = _SHARED_RATE_WINDOW - (ts - tok_window)
            return False, max(retry_after, 1.0)
        _shared_rendus_rate[tok_key] = (tok_count + 1, tok_window)

    return True, 0.0


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
        from core.db import set_local_access_context  # noqa: PLC0415
        from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

        set_local_access_context(conn, identity, enforce_epic36=True)
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
        from core.db import get_connection  # noqa: PLC0415
        from core.snapshots import list_render_snapshots  # noqa: PLC0415

        with get_connection() as conn:
            # AD-5 : verifier l'acces au projet (404 non-disclosant).
            if not _authorize_snapshot_project(
                identity,
                project_id,
                conn,
                minimum_capability="view",
            ):
                return JSONResponse(
                    {"code": "not_found", "message": "Projet non trouve"},
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
        from core.db import get_connection  # noqa: PLC0415
        from core.snapshots import get_render_snapshot  # noqa: PLC0415

        with get_connection() as conn:
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
        from core.db import get_connection  # noqa: PLC0415
        from core.snapshots import delete_render_snapshot  # noqa: PLC0415

        with get_connection() as conn:
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


async def _create_share(request: Request) -> Response:
    """POST /api/rendus/snapshots/{snapshot_id}/share -- creer un partage tokenise (O1).

    Query params :
        project_id  (required) -- scope AD-5

    Reponse 201 :
        {
          "share_id": "rss_...",
          "share_token": "<token>",
          "share_url": "https://{host}/api/rendus/shared/<token>",
          "shared_at": "2026-07-21T..."
        }

    Reponse 404 : snapshot introuvable ou acces refuse (non-disclosant).
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
        from core.db import get_connection  # noqa: PLC0415
        from core.snapshot_shares import create_share  # noqa: PLC0415

        with get_connection() as conn:
            if not _authorize_snapshot_project(
                identity,
                project_id,
                conn,
                minimum_capability="edit",
            ):
                return JSONResponse(
                    {"code": "not_found", "message": "Snapshot non trouve"},
                    status_code=404,
                )

            result = create_share(snapshot_id, project_id, identity, conn)

    except Exception as exc:
        logger.error("rendus_api: create_share err snap=%s p=%s: %s", snapshot_id, project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees : {exc}"},
            status_code=500,
        )

    if result is None:
        return JSONResponse(
            {"code": "not_found", "message": "Snapshot non trouve"},
            status_code=404,
        )

    share_id, token = result
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    share_url = f"{base_url}/api/rendus/shared/{token}"

    # Audit (best-effort, non bloquant).
    try:
        from core.audit import ACTION_SNAPSHOT_SHARED, write_audit_row  # noqa: PLC0415

        write_audit_row(
            identity=identity,
            action=ACTION_SNAPSHOT_SHARED,
            provider_account="rendus",
            connection_ref="",
            metadata={"share_id": share_id, "snapshot_id": snapshot_id, "project_id": project_id},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("rendus_api: audit share err: %s", exc)

    return JSONResponse(
        {
            "share_id": share_id,
            "share_token": token,
            "share_url": share_url,
        },
        status_code=201,
    )


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
              "share_token": "...",
              "share_url": "https://{host}/api/rendus/shared/<token>",
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
        from core.db import get_connection  # noqa: PLC0415
        from core.snapshot_shares import list_shares  # noqa: PLC0415

        with get_connection() as conn:
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

    # Enrichir avec share_url (construit a partir de l'hote de la requete).
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    for share in shares:
        tok = share.get("share_token") or ""
        share["share_url"] = f"{base_url}/api/rendus/shared/{tok}" if tok else None

    return JSONResponse({"shares": shares})


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
        from core.db import get_connection  # noqa: PLC0415
        from core.snapshot_shares import revoke_share  # noqa: PLC0415

        with get_connection() as conn:
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
        from core.audit import ACTION_SNAPSHOT_SHARE_REVOKED, write_audit_row  # noqa: PLC0415

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

_SHARED_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Rendu partage &mdash; toorow</title>
  <style>
    body {{
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f0f1a;
      color: #e8e8f0;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 32px 16px;
      min-height: 100vh;
    }}
    .banner {{
      font-size: 11px;
      color: #8888aa;
      background: rgba(255,255,255,0.04);
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 6px;
      padding: 8px 14px;
      margin-bottom: 24px;
      text-align: center;
      max-width: 640px;
    }}
    .widget-frame {{
      width: 100%;
      max-width: 900px;
      border: 1px solid rgba(255,255,255,0.08);
      border-radius: 10px;
      overflow: hidden;
      background: rgba(255,255,255,0.03);
      min-height: 400px;
    }}
    #widget-mount {{
      width: 100%;
      min-height: 400px;
    }}
  </style>
</head>
<body>
  <div class="banner">
    Partag&eacute; le {shared_at_fr} &mdash; Fraicheur fig&eacute;e : {stale_since}
    &mdash; Ce lien est en lecture seule. Aucune donn&eacute;e n&rsquo;est
    recharg&eacute;e au moment de l&rsquo;ouverture.
  </div>
  <div class="widget-frame">
    <div id="widget-mount"></div>
  </div>
  <script>
    // O1 strict : l'envelope est INJECTEE ici (gelee au moment du rendu).
    // Aucun appel reseau vers les marts ou le projet n'est effectue.
    window.__MCP_STRUCTURED_CONTENT__ = {envelope_json};
    window.__TOOROW_SHARED_META__ = {{
      share_id: {share_id_json},
      shared_at: {shared_at_json},
      stale_since: {stale_since_json},
      tool_name: {tool_name_json},
      question: {question_json}
    }};
    // Le shell widget lit l'envelope depuis window.__MCP_STRUCTURED_CONTENT__
    // via mcpApp.readInjectedEnvelope() au chargement.
    // (mcpApp.ts, Story 9.10 -- non modifie ici, AI-53)
  </script>
</body>
</html>"""


def _fmt_date_fr(iso: str | None) -> str:
    """Formater une date ISO en date FR lisible (sans librairie externe)."""
    if not iso:
        return "date inconnue"
    try:
        date_part = iso[:10]   # "2026-07-21"
        time_part = iso[11:16] if len(iso) > 16 else ""  # "14:32"
        mois = [
            "", "jan.", "fev.", "mars", "avr.", "mai", "juin",
            "juil.", "aout", "sept.", "oct.", "nov.", "dec.",
        ]
        y, m, d = date_part.split("-")
        m_label = mois[int(m)]
        return f"{int(d)} {m_label} {y}" + (f" a {time_part}" if time_part else "")
    except Exception:
        return iso or ""


async def _shared_snapshot_endpoint(request: Request) -> Response:
    """GET /api/rendus/shared/{token} -- snapshot fige HTML (endpoint public O1).

    Sans auth. Rate-limite : 60 req/min/(IP, token_prefix) + 120/min/token.
    Retourne le HTML single-file avec l'envelope GELEE injectee dans
    window.__MCP_STRUCTURED_CONTENT__ (mcpApp.readInjectedEnvelope, AI-53).

    Reponse 200 : HTML
    Reponse 404 : token inconnu ou revoque (non-disclosant)
    Reponse 429 : rate limit (Retry-After header)

    GARANTIE O1 :
      Aucun re-run. Aucun acces aux marts/projet.
      get_shared_snapshot ne touche QUE render_snapshot_shares + render_snapshots.
    """
    token = request.path_params.get("token", "")

    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = _check_rendus_rate_limit(client_ip, token)
    if not allowed:
        return JSONResponse(
            {"code": "rate_limited", "message": "Trop de requetes"},
            status_code=429,
            headers={"Retry-After": str(int(retry_after))},
        )

    if not token:
        return JSONResponse(
            {"code": "not_found", "message": "Lien non trouve"},
            status_code=404,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.snapshot_shares import get_shared_snapshot  # noqa: PLC0415

        with get_connection() as conn:
            snap = get_shared_snapshot(token, conn)

    except Exception as exc:
        logger.error("rendus_api: shared_snapshot err token_prefix=%s: %s", token[:8], exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur interne"},
            status_code=500,
        )

    if snap is None:
        return JSONResponse(
            {"code": "not_found", "message": "Lien non trouve"},
            status_code=404,
        )

    # Audit acces (best-effort, non bloquant).
    try:
        from core.audit import ACTION_SNAPSHOT_SHARE_ACCESSED, write_audit_row  # noqa: PLC0415

        write_audit_row(
            identity=f"public:{client_ip}",
            action=ACTION_SNAPSHOT_SHARE_ACCESSED,
            provider_account="rendus",
            connection_ref="",
            metadata={
                "share_id": snap.get("share_id"),
                "snapshot_id": snap.get("snapshot_id"),
                "token_prefix": token[:8],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("rendus_api: audit share_accessed err: %s", exc)

    # Construire le HTML avec l'envelope injectee (O1 strict).
    envelope = snap.get("envelope") or {}
    shared_at = snap.get("shared_at") or ""
    stale_since = snap.get("stale_since") or "unknown"
    share_id = snap.get("share_id") or ""
    tool_name = snap.get("tool_name") or ""
    question = snap.get("question") or ""

    html_content = _SHARED_HTML_TEMPLATE.format(
        shared_at_fr=_html_mod.escape(_fmt_date_fr(shared_at)),
        stale_since=_html_mod.escape(str(stale_since)),
        envelope_json=_json_for_script(envelope),
        share_id_json=_json_for_script(share_id),
        shared_at_json=_json_for_script(shared_at),
        stale_since_json=_json_for_script(stale_since),
        tool_name_json=_json_for_script(tool_name),
        question_json=_json_for_script(question),
    )

    # L1/L2 : headers de durcissement pour l'endpoint public.
    # 'unsafe-inline' est requis car le script est inline (injection envelope O1 strict).
    # C1 (fix XSS) est la defense principale ; ces headers sont la defense en profondeur.
    security_headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
        "Content-Security-Policy": (
            "default-src 'none'; "
            "style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; "
            "object-src 'none'; "
            "base-uri 'none'; "
            "form-action 'none'"
        ),
    }
    return HTMLResponse(content=html_content, status_code=200, headers=security_headers)


# ---------------------------------------------------------------------------
# Route list (spliced into admin_api.router)
# ---------------------------------------------------------------------------

RENDUS_ROUTES: list[Route] = [
    # Routes statiques AVANT les routes parametrisees (Starlette matching order).
    # Le endpoint public /shared/{token} n'a pas d'auth -- doit preceder les routes
    # console pour que "shared" ne soit pas absorbe comme snapshot_id.
    Route("/api/rendus/shared/{token}", endpoint=_shared_snapshot_endpoint, methods=["GET"]),
    # Console : suppression d'un partage (share_id dans path).
    Route("/api/rendus/shares/{share_id}", endpoint=_revoke_share, methods=["DELETE"]),
    # Console : galerie + CRUD snapshots.
    Route("/api/rendus/snapshots", endpoint=_list_snapshots, methods=["GET"]),
    Route("/api/rendus/snapshots/{snapshot_id}/share", endpoint=_create_share, methods=["POST"]),
    Route("/api/rendus/snapshots/{snapshot_id}/shares", endpoint=_list_shares, methods=["GET"]),
    Route("/api/rendus/snapshots/{snapshot_id}", endpoint=_get_snapshot, methods=["GET"]),
    Route("/api/rendus/snapshots/{snapshot_id}", endpoint=_delete_snapshot, methods=["DELETE"]),
]
