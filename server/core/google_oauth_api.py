"""Le consentement Google : autoriser, revenir, lire l etat, revoquer.

AD-43, 2026-08-13. Quatre routes qui portent la totalite du parcours OAuth
direct -- celui que la cible ratifie pour TOUT produit Google, BigQuery comprise.
Le rappel (`/callback`) est la plus longue des quatre (218 lignes) parce qu elle
est le seul endroit ou un jeton entre.

AD-21 est l exception ratifiee et bornee a AD-2/AD-3 : cet ecran de consentement
serveur unique porte volontairement les scopes de toute la pile Google.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from core.audit import (
    ACTION_CONNECTION_CREATED,
    ACTION_CROSS_SCOPE_ATTEMPT,
    write_audit_row,
)
from core.connection_revocation import (
    _refresh_connection_health_now,
)

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

# Human-readable French labels for the known Google stack scopes (UX-DR10).
#
# The line above used to say "Extend when new scopes are added to
# GOOGLE_STACK_SCOPES". It was an instruction, not a guard, and nobody followed
# it: measured 2026-08-01, FOUR labels for ELEVEN requested scopes -- cm360,
# dv360 and sa360 had been showing a raw URI to the person deciding what to
# grant since they were added. The guard now exists and is the reason this list
# is complete: tests/conformance/test_google_consent.py.
_GOOGLE_SCOPE_LABELS: dict[str, str] = {
    "https://www.googleapis.com/auth/analytics.readonly": "Google Analytics 4 (lecture)",
    "https://www.googleapis.com/auth/webmasters.readonly": "Google Search Console (lecture)",
    "https://www.googleapis.com/auth/adwords": "Google Ads",
    "https://www.googleapis.com/auth/spreadsheets.readonly": "Google Sheets (lecture)",
    "https://www.googleapis.com/auth/dfareporting": "Campaign Manager 360 (lecture)",
    "https://www.googleapis.com/auth/display-video": "Display & Video 360 (lecture)",
    "https://www.googleapis.com/auth/doubleclicksearch": "Search Ads 360 (lecture)",
    "https://www.googleapis.com/auth/bigquery.readonly": "Google BigQuery (lecture)",
    "https://www.googleapis.com/auth/dfp": "Google Ad Manager (lecture)",
    "https://www.googleapis.com/auth/business.manage": "Google Business Profile",
    "https://www.googleapis.com/auth/yt-analytics.readonly": "YouTube Analytics (lecture)",
    "https://www.googleapis.com/auth/youtube.readonly": "YouTube -- mises en ligne (lecture)",
}

async def _google_oauth_authorize(request: Request) -> Response:
    """GET /api/google/oauth/authorize?project_id=&connection_ref_id=

    Returns {"authorize_url": ...} for the console to redirect the admin to the
    single multi-scope Google consent screen. Enforces auth (AD-14) AND
    identity_can_read_project (AD-5) BEFORE generating the URL.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    connection_ref_id = (request.query_params.get("connection_ref_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "Le parametre 'project_id' est requis."},
            status_code=400,
        )
    if not connection_ref_id:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "Le parametre 'connection_ref_id' est requis.",
            },
            status_code=400,
        )

    # Consent can mutate only a Google credential owned by the requested project org.
    from core.project_access import (  # noqa: PLC0415
        identity_can_manage_org,
        identity_can_read_project,
    )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = identity_can_read_project(project_id, identity or "anonymous", conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT r.owner_org_id, p.org_id, r.provider
                    FROM app.connection_ref r
                    JOIN app.projects p ON p.id = %s AND p.status = 'active'
                    WHERE r.id = %s
                    """,
                    (project_id, connection_ref_id),
                )
                connection_row = cur.fetchone()
            if connection_row is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Connexion introuvable."},
                    status_code=404,
                )
            owner_org_id, project_org_id, provider = connection_row
            owner_managed = (
                owner_org_id == project_org_id
                and str(provider or "").lower().startswith("google")
                and identity_can_manage_org(owner_org_id, identity or "anonymous", conn)
            )
    except Exception as exc:
        logger.error("admin_api: google_oauth_authorize db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees."},
            status_code=500,
        )
    if not allowed or not owner_managed:
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_CROSS_SCOPE_ATTEMPT,
            provider_account="google_direct",
            connection_ref=connection_ref_id,
            metadata={
                "project_id": project_id,
                "operation": "google_oauth_authorize",
                "reason": "not_owner_manager",
            },
        )
        return JSONResponse(
            {
                "code": "forbidden",
                "message": "Seule l'organisation proprietaire peut reconnecter cet acces.",
            },
            status_code=403,
        )
    from core.google_oauth import GoogleOAuthConfigError, build_authorize_url  # noqa: PLC0415

    try:
        authorize_url = build_authorize_url(
            project_id=project_id,
            connection_ref_id=connection_ref_id,
            identity=identity or "anonymous",
        )
    except GoogleOAuthConfigError:
        # Client config missing (Phase B / AI-08). Honest French message, no leak.
        return JSONResponse(
            {
                "code": "oauth_not_configured",
                "message": (
                    "Le client OAuth Google n'est pas configure sur le serveur "
                    "(variables GOOGLE_OAUTH_* manquantes)."
                ),
            },
            status_code=503,
        )
    except Exception as exc:
        logger.error("admin_api: google_oauth_authorize error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "oauth_error", "message": "Impossible de construire l'URL d'autorisation."},
            status_code=500,
        )

    return JSONResponse({"authorize_url": authorize_url})

async def _google_oauth_callback(request: Request) -> Response:
    """GET /api/google/oauth/callback?code=&state=

    Google redirects here. Validates the anti-CSRF state (expired/unknown/forged
    -> generic 4xx, no oracle), exchanges the code, stores the encrypted token
    (Story 18.1), writes the emission audit row (AD-14), and redirects to the
    console with a coarse status flag. NO token/code in logs, errors, or the
    redirect URL.
    """
    from core.google_oauth import (  # noqa: PLC0415
        GoogleOAuthError,
        GoogleOAuthStateError,
        exchange_code,
        verify_state,
    )

    # Google may redirect with ?error=access_denied when the user declines.
    google_error = (request.query_params.get("error") or "").strip()
    state_param = request.query_params.get("state") or ""
    code = request.query_params.get("code") or ""

    # 1. Verify the anti-CSRF state FIRST -- before touching the code. A bad state
    #    is a generic 4xx with NO distinguishing detail (anti-oracle).
    try:
        state = verify_state(state_param)
    except GoogleOAuthStateError:
        logger.warning("admin_api: google_oauth_callback rejected_state")
        return JSONResponse(
            {
                "code": "invalid_state",
                "message": "Requete OAuth invalide ou expiree. Relancez la connexion Google.",
            },
            status_code=400,
        )

    if google_error:
        # User declined or Google refused. Honest French message; audit the attempt.
        logger.info("admin_api: google_oauth_callback user_declined")
        write_audit_row(
            identity=state.identity or "anonymous",
            action=ACTION_CROSS_SCOPE_ATTEMPT,
            provider_account="google_direct",
            connection_ref=state.connection_ref_id,
            metadata={
                "project_id": state.project_id,
                "operation": "google_oauth_callback",
                "reason": "user_declined",
            },
        )
        return JSONResponse(
            {
                "code": "consent_declined",
                "message": "Google consent denied or cancelled. No connection created.",
            },
            status_code=400,
        )

    if not code:
        return JSONResponse(
            {
                "code": "missing_code",
                "message": "Requete OAuth invalide (code absent). Relancez la connexion Google.",
            },
            status_code=400,
        )

    # 1b. Defense en profondeur -- re-verifier AD-5 AVANT l'echange du code.
    #     L'acces a pu etre revoque entre authorize et callback (fenetre <= 600 s).
    #     Si l'acces est refuse ici, on N'echange PAS le code (F-3).
    from core.project_access import identity_can_read_project  # noqa: PLC0415

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as _conn:
            _still_allowed = identity_can_read_project(
                state.project_id, state.identity or "anonymous", _conn
            )
    except Exception as _exc:
        logger.error(
            "admin_api: google_oauth_callback ad5_recheck_db_error: %s", type(_exc).__name__
        )
        return JSONResponse(
            {
                "code": "db_error",
                "message": "Erreur base de donnees lors de la verification d'acces.",
            },
            status_code=500,
        )
    if not _still_allowed:
        write_audit_row(
            identity=state.identity or "anonymous",
            action=ACTION_CROSS_SCOPE_ATTEMPT,
            provider_account="google_direct",
            connection_ref=state.connection_ref_id,
            metadata={
                "project_id": state.project_id,
                "operation": "google_oauth_callback_ad5_recheck",
                "reason": "access_revoked_between_authorize_and_callback",
            },
        )
        logger.warning(
            "admin_api: google_oauth_callback ad5_recheck_denied identity=%s project=%s "
            "(acces revoque entre authorize et callback)",
            state.identity,
            state.project_id,
        )
        return JSONResponse(
            {
                "code": "forbidden",
                "message": (
                    "Acces refuse : vous n'etes plus membre de ce projet. "
                    "La connexion Google n'a pas ete creee."
                ),
            },
            status_code=403,
        )

    # 2. Exchange the code for tokens (redacted errors -- never the code/tokens).
    try:
        token = await exchange_code(code)
    except GoogleOAuthError as exc:
        # exc message is redacted (status + short google error code only). Surface
        # a generic French message -- do NOT echo the redacted detail to the UI.
        logger.warning("admin_api: google_oauth_callback exchange_failed: %s", exc)
        return JSONResponse(
            {
                "code": "exchange_failed",
                "message": (
                    "Echec de la connexion Google (echange du code). Relancez la "
                    "connection; a new consent may be required."
                ),
            },
            status_code=502,
        )
    except Exception as exc:
        logger.error("admin_api: google_oauth_callback unexpected: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "oauth_error", "message": "Erreur inattendue lors de la connexion Google."},
            status_code=500,
        )

    # 3. Persist the encrypted token via the Story 18.1 store (single writer).
    #    expected_project_id defends against cross-project id confusion (18.1 F-1).
    from core.google_token_store import (  # noqa: PLC0415
        GoogleTokenStoreError,
        store_google_token,
    )

    try:
        store_google_token(
            state.connection_ref_id,
            {
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
                "metadata": {"token_type": token.token_type},
            },
            token.token_expiry,
            token.granted_scopes,
            expected_project_id=state.project_id,
        )
    except GoogleTokenStoreError as exc:
        # exc is already redacted. Generic French message to the UI.
        logger.warning("admin_api: google_oauth_callback store_failed: %s", exc)
        return JSONResponse(
            {
                "code": "store_failed",
                "message": "Impossible d'enregistrer la connexion Google. Reessayez.",
            },
            status_code=500,
        )

    # 3b. Story 56.6 (AD-36): re-poll the health HERE, at the moment it changed.
    #     Measured 2026-07-31: app.connection_health said 'revoked' with
    #     last_checked_at=11:10 while this very callback had refreshed the
    #     connection at 19:47 the same day -- eight and a half hours, because the
    #     poller is a 300 s thread in a container that gets no CPU between
    #     requests. The console therefore showed a dead connection to someone who
    #     had just reconnected it. A consent is an EVENT; waiting for a clock to
    #     notice it is what produced that lie.
    _refresh_connection_health_now(state.connection_ref_id)

    # 4. Emission audit row (AD-14 On-Behalf-Of). BLOCKED-18.2 F-6: emission audit
    #    belongs to this flow. Records the REAL identity from the verified state,
    #    the granted scopes -- NEVER the token.
    write_audit_row(
        identity=state.identity or "anonymous",
        action=ACTION_CONNECTION_CREATED,
        provider_account="google_direct",
        connection_ref=state.connection_ref_id,
        metadata={
            "project_id": state.project_id,
            "auth_path": "google_direct",
            "event": "google_token_emitted",
            "granted_scopes": token.granted_scopes,
        },
    )
    logger.info(
        "admin_api: google_oauth_callback success connection=%s scopes=%d",
        state.connection_ref_id,
        len(token.granted_scopes),
    )

    # 5. Redirect the browser back to the console with a coarse success flag only.
    org_id = ""
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (state.project_id,))
            row = cur.fetchone()
            org_id = str(row[0]) if row else ""
    except Exception:  # noqa: BLE001 -- a failed lookup costs the address, not the consent
        logger.warning("admin_api: google_oauth_callback could not resolve org for return address")
    return RedirectResponse(
        url=_oauth_console_redirect("success", state.connection_ref_id, state.project_id, org_id),
        status_code=302,
    )

async def _google_status(request: Request) -> Response:
    """GET /api/google/oauth/status/{connection_ref_id}

    Retourne l'etat Google direct d'une connexion :
      {
        "connection_ref_id": str,
        "auth_path":        "google_direct" | "nango",
        "health":           "ok" | "stale" | "not_connected" | "unknown",
        "token_expiry":     str (ISO-8601) | null,
        "granted_scopes":   [{"scope": str, "label": str}, ...],
        "project_id":       str
      }

    Jamais le blob chiffre (NFR3). AD-5 verifie l'acces projet avant reponse.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis."},
            status_code=401,
        )

    connection_ref_id = request.path_params.get("connection_ref_id", "")
    if not connection_ref_id:
        return JSONResponse(
            {"code": "missing_param", "message": "connection_ref_id est requis."},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_can_read_project  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT project_id, auth_path, token_expiry, granted_scopes
                      FROM app.connection_ref
                     WHERE id = %s
                    """,
                    (connection_ref_id,),
                )
                row = cur.fetchone()

            if row is None:
                return JSONResponse(
                    {
                        "code": "not_found",
                        "message": (f"Connexion '{connection_ref_id}' introuvable."),
                    },
                    status_code=404,
                )

            project_id, auth_path, token_expiry, granted_scopes = row

            # AD-5: verifier l'acces au projet.
            if not identity_can_read_project(project_id, identity or "anonymous", conn):
                write_audit_row(
                    identity=identity or "anonymous",
                    action=ACTION_CROSS_SCOPE_ATTEMPT,
                    provider_account="google_direct",
                    connection_ref=connection_ref_id,
                    metadata={
                        "project_id": project_id,
                        "operation": "google_status",
                        "reason": "not_a_member",
                    },
                )
                return JSONResponse(
                    {
                        "code": "forbidden",
                        "message": "Acces refuse : vous n'appartenez pas a ce projet.",
                    },
                    status_code=403,
                )
    except Exception as exc:
        logger.error("admin_api: google_status db_error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees."},
            status_code=500,
        )

    health = _derive_google_health(token_expiry, auth_path or "nango")
    scopes_list = list(granted_scopes or [])
    scope_objects = [{"scope": s, "label": _scope_label(s)} for s in scopes_list]

    return JSONResponse(
        {
            "connection_ref_id": connection_ref_id,
            "auth_path": auth_path or "nango",
            "health": health,
            "token_expiry": token_expiry.isoformat() if token_expiry else None,
            "granted_scopes": scope_objects,
            "project_id": project_id,
        }
    )

async def _google_revoke(request: Request) -> Response:
    """POST /api/google/oauth/revoke/{connection_ref_id}

    Revoque le token Google direct :
      1. Verifie auth + acces projet (AD-5).
      2. Appelle le revoke endpoint Google (best-effort : une erreur cote Google
         ne bloque PAS la purge locale -- pattern delete_connection).
      3. Purge le blob chiffre local via clear_google_token(performed_by=identite
         REELLE -- jamais 'system' sur un chemin humain, review 18.1 F-6).
      4. L'audit On-Behalf-Of est ecrit par clear_google_token (AD-14).

    Idempotent : une connexion deja en auth_path='nango' sans blob repond 200.
    Cross-projet : AD-5 -> 403 (audit ACTION_CROSS_SCOPE_ATTEMPT).
    Connexion absente : 404.

    Response (200):
      {"revoked": true, "connection_ref_id": str,
       "google_revoke": "ok" | "best_effort_failed"
                      | "skipped_already_clear" | "skipped_decrypt_failed"}
    Note: "skipped_decrypt_failed" means the blob is present but unreadable --
    the token may still be ACTIVE at Google; manual revocation may be needed.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis."},
            status_code=401,
        )

    connection_ref_id = request.path_params.get("connection_ref_id", "")
    if not connection_ref_id:
        return JSONResponse(
            {"code": "missing_param", "message": "connection_ref_id est requis."},
            status_code=400,
        )

    # 1. Verifier l'existence + le projet + l'acces.
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import (  # noqa: PLC0415
            identity_can_manage_org,
            identity_can_read_project,
        )

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT project_id, auth_path, token_expiry, granted_scopes, owner_org_id
                      FROM app.connection_ref
                     WHERE id = %s
                    """,
                    (connection_ref_id,),
                )
                row = cur.fetchone()

            if row is None:
                return JSONResponse(
                    {
                        "code": "not_found",
                        "message": (f"Connexion '{connection_ref_id}' introuvable."),
                    },
                    status_code=404,
                )

            project_id, auth_path, _expiry, _scopes, owner_org_id = row

            # AD-5: verifier l'acces au projet AVANT toute operation.
            if not identity_can_read_project(project_id, identity or "anonymous", conn):
                write_audit_row(
                    identity=identity or "anonymous",
                    action=ACTION_CROSS_SCOPE_ATTEMPT,
                    provider_account="google_direct",
                    connection_ref=connection_ref_id,
                    metadata={
                        "project_id": project_id,
                        "operation": "google_revoke",
                        "reason": "not_a_member",
                    },
                )
                return JSONResponse(
                    {
                        "code": "forbidden",
                        "message": "Acces refuse : vous n'appartenez pas a ce projet.",
                    },
                    status_code=403,
                )
            if not identity_can_manage_org(owner_org_id, identity or "anonymous", conn):
                return JSONResponse(
                    {
                        "code": "forbidden",
                        "message": "Seule l'organisation proprietaire peut revoquer cet acces.",
                    },
                    status_code=403,
                )
    except Exception as exc:
        logger.error("admin_api: google_revoke db_error: %s", type(exc).__name__)

        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees."},
            status_code=500,
        )

    # 2. Appel best-effort au revoke endpoint Google (NFR3 : aucun token loggue).
    #    Si auth_path n'est pas 'google_direct', il n'y a pas de token a revoquer
    #    cote Google : on saute l'appel et on purge quand meme (idempotence).
    google_revoke_status = "skipped"
    if auth_path == "google_direct":
        try:
            # On charge le token pour obtenir l'access_token a revoquer.
            # clear_google_token efface le blob; il faut charger AVANT.
            # review-18-5: deux cas de "skip" distincts (AD-9 honnetete) :
            #   - "skipped_already_clear" : blob absent ou connexion sans blob ->
            #     le token n'existe pas chez Google (idempotent, safe).
            #   - "skipped_decrypt_failed" : blob PRESENT mais indechiffrable ->
            #     le token peut etre encore ACTIF cote Google ; AVERTIR (NFR3).
            from core.google_token_store import (  # noqa: PLC0415
                GoogleTokenStoreError,
                load_google_token,
            )

            try:
                gt = load_google_token(connection_ref_id, expected_project_id=project_id)
                access_token_to_revoke = gt.access_token
            except GoogleTokenStoreError as _load_exc:
                access_token_to_revoke = None
                _err_msg = str(_load_exc)
                # Distinguish: "no blob" / "not found" (safe, token never issued or
                # already cleared) vs "decrypt failed" (blob present but unreadable --
                # token may still be ACTIVE at Google).
                _decrypt_failed = any(
                    kw in _err_msg
                    for kw in (
                        "cannot decrypt",
                        "decryption failed",
                        "tampered",
                        "wrong key",
                        "valid JSON",
                        "unsupported token blob",
                    )
                )
                if _decrypt_failed:
                    google_revoke_status = "skipped_decrypt_failed"
                    logger.warning(
                        "admin_api: google_revoke skipped_decrypt_failed "
                        "connection=%s -- token may still be active at Google; "
                        "manual revocation may be required (review-18-5 AD-9)",
                        connection_ref_id,
                    )
                else:
                    # Blob absent / already purged -> safe to skip (idempotent).
                    google_revoke_status = "skipped_already_clear"

            if access_token_to_revoke:
                import httpx  # noqa: PLC0415

                from core.google_oauth import GOOGLE_REVOKE_ENDPOINT  # noqa: PLC0415

                try:
                    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                        # RFC / Google docs: token must be in the FORM BODY, never in
                        # the query string (which would leak it to logs/proxies).
                        resp = await client.post(
                            GOOGLE_REVOKE_ENDPOINT,
                            data={"token": access_token_to_revoke},
                        )
                    if resp.status_code < 400:
                        google_revoke_status = "ok"
                    else:
                        # Google a refuse mais on continue : best-effort.
                        google_revoke_status = "best_effort_failed"
                        logger.warning(
                            "admin_api: google_revoke google_endpoint_failed "
                            "status=%d (purge locale continue)",
                            resp.status_code,
                        )
                except Exception as exc:
                    google_revoke_status = "best_effort_failed"
                    logger.warning(
                        "admin_api: google_revoke google_endpoint_error: %s "
                        "(purge locale continue)",
                        type(exc).__name__,
                    )
            elif google_revoke_status == "skipped":
                # access_token_to_revoke was empty/falsy but no exception was raised
                # (empty access_token field in a valid blob) -> treat as already clear.
                google_revoke_status = "skipped_already_clear"
        except Exception as exc:
            logger.warning(
                "admin_api: google_revoke load_token_error: %s (purge locale continue)",
                type(exc).__name__,
            )
            google_revoke_status = "best_effort_failed"

    # 3. Purge locale -- jamais bloquee par l'echec du revoke Google.
    #    performed_by = identite REELLE (review-18-1 F-6 : jamais 'system' sur
    #    un chemin humain).
    try:
        from core.google_token_store import (  # noqa: PLC0415
            GoogleTokenStoreError,
            clear_google_token,
        )

        clear_google_token(
            connection_ref_id,
            performed_by=identity or "anonymous",
            expected_project_id=project_id,
        )
    except GoogleTokenStoreError as exc:
        logger.error("admin_api: google_revoke clear_failed: %s", exc)
        return JSONResponse(
            {
                "code": "revoke_failed",
                "message": "Impossible de purger la connexion Google. Reessayez.",
            },
            status_code=500,
        )

    logger.info(
        "admin_api: google_revoke success connection=%s google_status=%s",
        connection_ref_id,
        google_revoke_status,
    )
    return JSONResponse(
        {
            "revoked": True,
            "connection_ref_id": connection_ref_id,
            "google_revoke": google_revoke_status,
        }
    )

def _derive_google_health(token_expiry, auth_path: str) -> str:
    """Derive a health status string from the token expiry and auth_path.

    Rules (source locale -- pas de polling Nango pour google_direct):
      * auth_path != 'google_direct'  -> 'not_connected' (pas de token Google)
      * token_expiry is None          -> 'unknown'  (token sans expiry connue)
      * expiry dans > 5 min           -> 'ok'
      * expiry dans <= 5 min ou passe -> 'stale'  (refresh imminent ou requis)

    These mirror the Nango health statuses (ok/stale) reused from the existing
    surface (Story 2.5) so the UI can render the same badges.
    """
    if auth_path != "google_direct":
        return "not_connected"
    if token_expiry is None:
        return "unknown"
    now = datetime.now(tz=timezone.utc)
    # token_expiry may be a naive datetime from Postgres -- normalise to UTC.
    if token_expiry.tzinfo is None:
        token_expiry = token_expiry.replace(tzinfo=timezone.utc)
    delta = (token_expiry - now).total_seconds()
    return "ok" if delta > 300 else "stale"

def _oauth_console_redirect(
    status: str,
    connection_ref_id: str = "",
    project_id: str = "",
    org_id: str = "",
) -> str:
    """Build the safe console redirect target after a callback.

    Carries ONLY a coarse status flag + the connection id -- never a token, never
    the authorization code, never any state detail. The console reads
    ``?google_oauth=<status>`` to render a French success/error banner.

    F-5: la valeur de ADMIN_CONSOLE_OAUTH_RETURN doit commencer par '/' (chemin
    relatif interne). Toute valeur ne respectant pas ce critere (ex: URL externe
    http://evil.com) est ignoree et le defaut '/console/connections' est utilise
    (defense contre les redirections ouvertes).
    """
    # THE CONSENT CAME BACK TO A 404, and that is what made a SUCCESSFUL consent
    # look like a failed one: `/console/connections` is a route the shell does
    # not have, resolved against the API host, which serves no console at all.
    # Measured 2026-08-11 on a real Google consent: the token was stored, the
    # scopes were granted, and the person was shown "Not Found".
    #
    # The console's own address is `/org/{org}/project/{project}/data/sources` —
    # the Sources collection of that Project, which is where a new authorization
    # becomes visible. The origin is READ FROM THE ENVIRONMENT, never written
    # here: this repository carries no real domain. Without it the behaviour is
    # exactly what it was, a relative path.
    console_origin = (
        os.environ.get("ADMIN_CONSOLE_ORIGIN", "").strip()
        or os.environ.get("TOOROW_INVITATION_ORIGIN", "").strip()
    ).rstrip("/")
    if project_id and org_id:
        path = f"/org/{quote(org_id)}/project/{quote(project_id)}/data/sources"
        params = {"google_oauth": status}
        if connection_ref_id:
            params["connection"] = connection_ref_id
        return f"{console_origin}{path}?{urlencode(params)}"

    _DEFAULT_RETURN = "/console/connections"
    raw = os.environ.get("ADMIN_CONSOLE_OAUTH_RETURN", "").strip()
    if raw and raw.startswith("/"):
        base = raw
    else:
        if raw:
            logger.warning(
                "admin_api: ADMIN_CONSOLE_OAUTH_RETURN=%r ne commence pas par '/' "
                "-- utilisation du defaut %r (defense open-redirect F-5)",
                raw,
                _DEFAULT_RETURN,
            )
        base = _DEFAULT_RETURN
    params = {"google_oauth": status}
    if connection_ref_id:
        params["connection"] = connection_ref_id
    return f"{base}?{urlencode(params)}"

def _scope_label(scope: str) -> str:
    """Return a human-readable French label for a scope URI, or the raw URI."""
    return _GOOGLE_SCOPE_LABELS.get(scope, scope)


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

GOOGLE_OAUTH_ROUTES_1 = [
    # Story 18.2: Google server-side OAuth (authorize + callback). AD-15: the
    # flow lives in the console; the callback is Google's redirect target.
    Route(
        "/api/google/oauth/authorize",
        endpoint=_google_oauth_authorize,
        methods=["GET"],
    ),
    Route(
        "/api/google/oauth/callback",
        endpoint=_google_oauth_callback,
        methods=["GET"],
    ),
    # Story 18.4: Google connection status + revocation.
    # IMPORTANT: /status/{id} and /revoke/{id} must come BEFORE any generic
    # parametrized route that could absorb "status" or "revoke" as path params.
    Route(
        "/api/google/oauth/status/{connection_ref_id}",
        endpoint=_google_status,
        methods=["GET"],
    ),
    Route(
        "/api/google/oauth/revoke/{connection_ref_id}",
        endpoint=_google_revoke,
        methods=["POST"],
    ),
]
