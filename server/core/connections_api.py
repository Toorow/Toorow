"""Une connexion a une source : la creer, la lire, la sonder, en tirer.

AD-43, 2026-08-13. Huit routes, UN sujet -- contrairement a `/api/projects` et
`/api/organizations`, ou le prefixe en cachait quatre. La regle amendee ne dit pas
que tout prefixe ment ; elle dit de LIRE avant de decouper.

La connexion, ses comptes, ses connecteurs, le rafraichissement de sa sante
(avec son propre etranglement), le tirage manuel et la reprise. Ce qui n est PAS
ici : la revocation, partie avec `project_connections_api` parce qu elle
s adresse par le projet qui la porte.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core import nango_client
from core.audit import (
    ACTION_CONNECTION_CREATED,
    ACTION_CROSS_SCOPE_ATTEMPT,
    declare_action,
    write_audit_row,
)
from core.connection_revocation import (
    _refresh_health_last,
)

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _credential_authorship(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.me_api import _credential_authorship as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _enforce_credential_org_read(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _enforce_credential_org_read as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

# AD-42 : une action se declare chez celui qui l ECRIT, et ce module est
# desormais son seul ecrivain.
ACTION_CONNECTION_ACCOUNT_SELECTED = declare_action("connection.account_selected")
ACTION_ACCOUNT_SELECTED = ACTION_CONNECTION_ACCOUNT_SELECTED

# ISO-8601 date pattern (YYYY-MM-DD) — used by _trigger_pull to validate body dates
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_REFRESH_HEALTH_RATE_LIMIT_SECONDS = 30

async def _list_connections(request: Request) -> Response:
    """List provider accounts usable by the requested project organization."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {
                "code": "unauthorized",
                "message": (
                    "This request carries no signed-in identity. "
                    "Sign in again, then re-run it."
                ),
            },
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {
                "code": "missing_project",
                "message": (
                    "This request does not say which Project to list connections "
                    "for. Add `project_id` to the request and re-run it."
                ),
            },
            status_code=400,
        )

    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

        with request_connection(identity) as conn:
            # Strict, always: Story 46.4 removed the runtime flag, and the 404 is
            # deliberate -- a caller without access must not learn the project
            # exists. `identity_can_access_project_in_org` is no longer reachable
            # from here; a fixture that patches it patches nothing.
            allowed = resolve_strict_resource_access(
                identity, conn, project_id=project_id, minimum_capability="view"
            ).allowed
            if not allowed:
                return JSONResponse(
                    {
                        "code": "not_found",
                        "message": (
                            "This Project is not readable under your sign-in. "
                            "Choose a Project you have access to, or ask an "
                            "administrator of this organisation to grant it."
                        ),
                    },
                    status_code=404,
                )

            with conn.cursor() as cur:
                cur.execute(
                    """
                    WITH viewer AS (
                        SELECT org_id
                        FROM app.projects
                        WHERE id = %s AND status = 'active'
                    )
                    SELECT
                        r.id,
                        r.provider,
                        r.nango_connection_id,
                        r.project_id,
                        r.created_at,
                        r.status,
                        r.auth_path,
                        r.owner_org_id,
                        r.owner_identity,
                        r.token_expiry,
                        o.name AS owner_org_name,
                        up.display_name AS owner_display_name,
                        up.email AS owner_email,
                        v.org_id AS viewer_org_id,
                        s.account_label,
                        s.state AS account_state,
                        (
                            SELECT count(*) FROM app.connection_account_scope sc
                            WHERE sc.connection_ref_id = r.id AND sc.account_id IS NOT NULL
                        ) AS selected_account_count,
                        h.status AS health_status,
                        h.last_checked_at,
                        h.last_fetched_at,
                        COUNT(ds.id) FILTER (WHERE ds.enabled = TRUE)
                            AS active_datastream_count,
                        -- ANY granted account of this credential, not "the"
                        -- selected one. Correlating the grant with a single
                        -- scope row meant a credential shared on account B while
                        -- account A happened to be selected read as not shared.
                        EXISTS (
                            SELECT 1
                            FROM app.credential_account_grants g
                            WHERE g.credential_id = r.id
                              AND g.status = 'active'
                              AND g.grantee_org_id = v.org_id
                        ) AS provided_to_viewer,
                        EXISTS (
                            SELECT 1
                            FROM app.credential_account_grants g
                            WHERE g.credential_id = r.id
                              AND g.status = 'active'
                        ) AS has_outgoing_grant,
                        EXISTS (
                            SELECT 1
                            FROM app.org_members m
                            WHERE m.org_id = r.owner_org_id
                              AND m.identity = %s
                              AND m.status = 'active'
                              AND m.role IN ('owner', 'admin')
                        ) AS caller_manages_owner,
                        r.enabled AS connection_enabled
                    FROM app.connection_ref r
                    CROSS JOIN viewer v
                    LEFT JOIN app.connection_health h ON h.connection_ref_id = r.id
                    -- One row per authorization, whatever the number of accounts
                    -- selected under it (migration 211). See _AUTHORIZATION_QUERY.
                    LEFT JOIN LATERAL (
                        SELECT sc.account_label, sc.state
                        FROM app.connection_account_scope sc
                        WHERE sc.connection_ref_id = r.id
                        ORDER BY (sc.state = 'ready') DESC, sc.verified_at DESC NULLS LAST
                        LIMIT 1
                    ) s ON TRUE
                    LEFT JOIN app.datastreams ds
                      ON ds.connection_ref_id = r.id AND ds.org_id = v.org_id AND ds.project_id = %s
                    LEFT JOIN app.organizations o ON o.id = r.owner_org_id
                    -- The person who plugged the credential in (mig 101). LEFT, and
                    -- deliberately not an FK: the profile row only appears once the
                    -- user fills it in, while owner_identity exists from the first token.
                    LEFT JOIN app.user_profiles up ON up.identity = r.owner_identity
                    WHERE r.owner_org_id = v.org_id
                       OR EXISTS (
                            SELECT 1
                            FROM app.credential_account_grants g
                            WHERE g.credential_id = r.id
                              AND g.status = 'active'
                              AND g.grantee_org_id = v.org_id
                       )
                    GROUP BY r.id, r.provider, r.nango_connection_id, r.project_id,
                             r.created_at, r.status, r.auth_path, r.owner_org_id,
                             r.owner_identity, r.token_expiry, o.name,
                             up.display_name, up.email, v.org_id,
                             s.account_label, s.state, h.status, h.last_checked_at,
                             h.last_fetched_at
                    ORDER BY r.created_at DESC
                    """,
                    (project_id, identity or "anonymous", project_id),
                )
                cols = [desc[0] for desc in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.error("admin_api: list_connections db_error: %s", exc)
        return JSONResponse(
            {
                "code": "db_error",
                "message": (
                    "The connections of this Project could not be read just now. "
                    "Re-run the request in a moment, and if it keeps failing ask "
                    "an administrator to read the server log."
                ),
            },
            status_code=503,
        )

    timestamp_columns = {
        "created_at", "last_checked_at", "last_fetched_at", "token_expiry"
    }
    connections: list[dict] = []
    for ref in rows:
        for column in timestamp_columns:
            value = ref.get(column)
            if value is not None and hasattr(value, "isoformat"):
                ref[column] = value.isoformat()
        owner_org_id = ref.get("owner_org_id")
        viewer_org_id = ref.get("viewer_org_id")
        provided = bool(ref.get("provided_to_viewer"))
        exposure = (
            "provided_by_org"
            if provided and owner_org_id != viewer_org_id
            else "shared_with_org"
            if ref.get("has_outgoing_grant")
            else "owned"
        )
        health_status = ref.get("health_status")
        if ref.get("status") == "revoked":
            health_status = "revoked"
        health = None
        if health_status is not None:
            health = {
                "status": health_status,
                "last_checked_at": ref.get("last_checked_at"),
                "last_fetched_at": ref.get("last_fetched_at"),
            }
        connections.append(
            {
                "id": ref["id"],
                "nango_connection_id": ref["nango_connection_id"],
                "provider": ref["provider"],
                "project_id": project_id,
                "created_at": ref["created_at"],
                "status": ref.get("status"),
                "enabled": ref.get("connection_enabled") is not False,
                "auth_path": ref.get("auth_path"),
                "health": health,
                "active_datastream_count": int(ref.get("active_datastream_count") or 0),
                "owner_org_id": owner_org_id,
                "owner_org_name": ref.get("owner_org_name"),
                "token_expiry": ref.get("token_expiry"),
                "account_label": ref.get("account_label"),
                "account_state": ref.get("account_state"),
                "selected_account_count": ref.get("selected_account_count") or 0,
                "exposure": exposure,
                "can_manage": bool(ref.get("caller_manages_owner")) and not provided,
                **_credential_authorship(ref, identity, provided),
            }
        )

    return JSONResponse({"connections": connections})

async def _create_connection(request: Request) -> Response:
    """POST /api/connections -- register a new connection_ref row.
    Request body (JSON):
      {"nango_connection_id": str, "provider": str, "project_id": str}

    Response (201):
      {"id", "nango_connection_id", "provider", "project_id", "created_at"}

    Writes an audit row (ACTION_CONNECTION_CREATED) after successful DB insert.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {
                "code": "unauthorized",
                "message": (
                    "This request carries no signed-in identity. "
                    "Sign in again, then re-run it."
                ),
            },
            status_code=401,
        )

    # Parse request body
    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes)
    except Exception as exc:
        # The parser's own words used to travel to the caller inside the refusal.
        # They belong in the log: the reader needs the gesture, the operator
        # needs the detail, and the two are not the same sentence.
        logger.warning("admin_api: create_connection invalid_body: %s", type(exc).__name__)
        return JSONResponse(
            {
                "code": "invalid_body",
                "message": (
                    "This request body could not be read as JSON. "
                    "Write it as a JSON object and re-run the request."
                ),
            },
            status_code=400,
        )

    nango_connection_id = (body.get("nango_connection_id") or "").strip()
    provider = (body.get("provider") or "").strip()
    # Project scope is explicit; empty and legacy placeholder ids fail closed.
    project_id = (body.get("project_id") or "").strip()

    if not nango_connection_id:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": (
                    "This request does not name the authorization to record. "
                    "Add `nango_connection_id` to the request and re-run it."
                ),
            },
            status_code=400,
        )
    if not provider:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": (
                    "This request does not say which source the authorization is "
                    "for. Add `provider` to the request and re-run it."
                ),
            },
            status_code=400,
        )
    if not project_id:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": (
                    "This request does not say which Project the connection "
                    "belongs to. Add `project_id` to the request and re-run it."
                ),
            },
            status_code=400,
        )
    if project_id == "default":
        return JSONResponse(
            {
                "code": "invalid_project",
                "message": (
                    "`default` is a placeholder, not a Project. Choose the "
                    "Project this connection belongs to, then re-run the request."
                ),
            },
            status_code=400,
        )

    # review-2-4 F-03: bound and shape-check inputs. provider must be a valid
    # module/integration key (kebab-case, same charset as manifest names);
    # ids are capped to keep the table clean.
    if len(nango_connection_id) > 256 or len(project_id) > 256:
        return JSONResponse(
            {
                "code": "invalid_field",
                "message": (
                    "The authorization identifier or the Project identifier is "
                    "longer than 256 characters. Enter identifiers of 256 "
                    "characters or fewer, then re-run the request."
                ),
            },
            status_code=400,
        )
    if len(provider) > 64 or not re.fullmatch(r"[a-z0-9-]+", provider):
        return JSONResponse(
            {
                "code": "invalid_field",
                "message": (
                    "A source name uses lower-case letters, digits and hyphens "
                    "only, and at most 64 characters. Enter the source name in "
                    "that form and re-run the request."
                ),
            },
            status_code=400,
        )
    try:
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity or "anonymous") as conn:
            owner_org_id, strict_gate = _resolve_connection_create_scope(
                conn, project_id, identity or "anonymous"
            )
    except Exception as exc:
        logger.error("admin_api: connection_scope_error: %s", exc)
        return JSONResponse(
            {
                "code": "db_error",
                "message": (
                    "Your access to this Project could not be checked just now. "
                    "Re-run the request in a moment, and if it keeps failing ask "
                    "an administrator to read the server log."
                ),
            },
            status_code=503,
        )
    if owner_org_id is None:
        return JSONResponse(
            {
                "code": "not_found",
                "message": (
                    "This Project is not readable under your sign-in. Choose a "
                    "Project you have access to, or ask an administrator of this "
                    "organisation to grant it."
                ),
            },
            status_code=404 if strict_gate else 403,
        )

    # review-2-4 F-01: the popup-closed signal does NOT mean OAuth succeeded.
    # Refuse to record a connection Nango does not know about (prevents
    # orphan connection_ref rows when the user aborts the flow).
    try:
        nango_conns = await nango_client._list_connections_async()
        known_ids = {c.get("connection_id") for c in nango_conns}
        if nango_connection_id not in known_ids:
            return JSONResponse(
                {
                    "code": "unknown_nango_connection",
                    "message": (
                        "The authorization was never completed with the source, "
                        "so there is nothing to record. Finish the connection "
                        "flow in the source's window, then re-run the request."
                    ),
                },
                status_code=409,
            )
    except Exception as exc:
        logger.error("admin_api: nango_verify_error: %s", exc)
        return JSONResponse(
            {
                "code": "nango_unreachable",
                "message": (
                    "The authorization could not be verified with the source "
                    "just now, so nothing was recorded. Re-run the request in a "
                    "moment, and if it keeps failing ask an administrator to "
                    "read the server log."
                ),
            },
            status_code=503,
        )

    # Mint ULID
    conn_id = _mint_conn_id()

    # Insert only after re-checking the same manage scope in the write transaction.
    try:
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity or "anonymous") as conn:
            owner_org_id, strict_gate = _resolve_connection_create_scope(
                conn, project_id, identity or "anonymous"
            )
            if owner_org_id is None:
                return JSONResponse(
                    {
                        "code": "not_found",
                        "message": (
                            "This Project is not readable under your sign-in. "
                            "Choose a Project you have access to, or ask an "
                            "administrator of this organisation to grant it."
                        ),
                    },
                    status_code=404 if strict_gate else 403,
                )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.connection_ref
                        (id, provider, nango_connection_id, project_id,
                         owner_org_id, owner_identity)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, provider, nango_connection_id, project_id, created_at
                    """,
                    (
                        conn_id,
                        provider,
                        nango_connection_id,
                        project_id,
                        owner_org_id,
                        identity or "anonymous",
                    ),
                )
                row = cur.fetchone()
                if row is None:  # pragma: no cover
                    raise RuntimeError("INSERT RETURNING returned no row")
                cols = [desc[0] for desc in cur.description]
                created_record: dict = {}
                for col, val in zip(cols, row):
                    if col == "created_at" and val is not None:
                        created_record[col] = val.isoformat()
                    else:
                        created_record[col] = val
            conn.commit()

    except Exception as exc:
        logger.error("admin_api: db_insert_error: %s", exc)
        return JSONResponse(
            {
                "code": "db_error",
                "message": (
                    "This connection could not be recorded just now. Nothing was "
                    "created; re-run the request in a moment, and if it keeps "
                    "failing ask an administrator to read the server log."
                ),
            },
            status_code=500,
        )

    # Write audit row (never raises -- AC6 in audit.py)
    write_audit_row(
        identity=identity,
        action=ACTION_CONNECTION_CREATED,
        provider_account=provider,
        connection_ref=conn_id,
    )

    return JSONResponse(created_record, status_code=201)

async def _refresh_health(request: Request) -> Response:
    """POST /api/connections/<id>/refresh-health -- on-demand health poll (AC4).

    Reads the connection_ref row for <id>, calls nango_client.poll_connection_health()
    immediately (NOT the cached value), upserts connection_health, and returns the
    updated health state.

    Response (200):
      {"id": ..., "health": {"status": ..., "last_checked_at": ..., "last_fetched_at": ...}}
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {
                "code": "unauthorized",
                "message": (
                    "This request carries no signed-in identity. "
                    "Sign in again, then re-run it."
                ),
            },
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    if not conn_ref_id:
        return JSONResponse(
            {
                "code": "missing_id",
                "message": (
                    "This request does not say which connection to act on. "
                    "Add the connection identifier to the address and re-run it."
                ),
            },
            status_code=400,
        )

    # AI-18: per-connection rate limit (Story 3.3, AC10).
    # Reject if the same connection was refreshed within the last 30 seconds.
    now_mono = time.monotonic()
    last = _refresh_health_last.get(conn_ref_id, 0.0)
    elapsed = now_mono - last
    if elapsed < _REFRESH_HEALTH_RATE_LIMIT_SECONDS:
        retry_after = int(_REFRESH_HEALTH_RATE_LIMIT_SECONDS - elapsed)
        return JSONResponse(
            {
                "code": "rate_limited",
                "retry_after": retry_after,
                # A refusal with NO sentence at all is the silent end of the
                # class this module was rewritten for: the reader learns the
                # act was refused and never what to do about it.
                "message": (
                    f"This connection was checked a moment ago. Wait for "
                    f"{retry_after} more seconds, then run the check again."
                ),
            },
            status_code=429,
        )

    # Fetch connection_ref to get nango_connection_id + provider
    try:
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity) as conn:
            _scope_ref, scope_error = _resolve_conn_project_scoped(conn_ref_id, identity, conn)
            if scope_error is not None:
                return scope_error
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, nango_connection_id, provider
                    FROM app.connection_ref
                    WHERE id = %s
                    """,
                    (conn_ref_id,),
                )
                row = cur.fetchone()
    except Exception as exc:
        logger.error("admin_api: refresh_health db_error: %s", exc)
        return JSONResponse(
            {
                "code": "db_error",
                "message": (
                    "This connection could not be read just now. Re-run the "
                    "check in a moment, and if it keeps failing ask an "
                    "administrator to read the server log."
                ),
            },
            status_code=500,
        )

    if row is None:
        return JSONResponse(
            {
                "code": "not_found",
                "message": (
                    "No connection with this identifier exists in this Project. "
                    "Choose a connection from the Project's connection list, or "
                    "check the identifier in the address."
                ),
            },
            status_code=404,
        )

    nango_connection_id, provider = row[1], row[2]

    # PROVIDER-AWARE, LIKE EVERY OTHER READ OF HEALTH. This called
    # `_poll_connection_health_async` directly -- the Nango implementation --
    # instead of `poll_connection_health`, which is the router that sends a
    # `google_direct` row to its LOCAL health. Nango knows nothing of a
    # google_direct connection (it has no `nango_connection_id` at all: the
    # column is NULL since migration 128), so it answered `revoked` for every
    # Google authorization, and the console renders revoked as "reconnect".
    #
    # Measured 2026-08-11 on a live consent with a 676-byte blob and fifteen
    # granted scopes, minutes after a preview pulled real data through it: the
    # explicit refresh -- the button a person presses BECAUSE they doubt -- was
    # the one call that reported it dead.
    from core import token_service  # noqa: PLC0415

    resolved = token_service.resolve_connection_by_nango_id(nango_connection_id or conn_ref_id)
    google_direct = (
        resolved is not None and resolved.auth_path == token_service.AUTH_PATH_GOOGLE_DIRECT
    )

    if google_direct:
        health = token_service.google_direct_health(resolved)
    else:
        # Poll Nango (on-demand -- the explicit refresh, not the background poller)
        try:
            health = await nango_client._poll_connection_health_async(
                nango_connection_id, provider=provider
            )
        except Exception as exc:
            logger.error("admin_api: refresh_health nango_error: %s", exc)
            return JSONResponse(
                {
                    "code": "nango_error",
                    "message": (
                        "The source did not answer, so the health of this "
                        "connection is unknown. Re-run the check in a moment, "
                        "and if it keeps failing ask an administrator to read "
                        "the server log."
                    ),
                },
                status_code=502,
            )

    now = datetime.now(tz=timezone.utc)

    # Upsert into connection_health
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.connection_health
                        (connection_ref_id, status, last_checked_at, last_fetched_at)
                    VALUES (%(id)s, %(status)s, %(last_checked_at)s, %(last_fetched_at)s)
                    ON CONFLICT (connection_ref_id) DO UPDATE
                        SET status          = EXCLUDED.status,
                            last_checked_at = EXCLUDED.last_checked_at,
                            last_fetched_at = EXCLUDED.last_fetched_at,
                            -- AI-302 / migration 276: this hand refresh writes an
                            -- auth status (`ok|stale|revoked`), never
                            -- `populate_failed`. The three columns that name the
                            -- pull behind a data red therefore describe a state
                            -- the row no longer holds, and are dropped with it --
                            -- a label pointing at a collection that is not the
                            -- problem is worse than no label.
                            populate_failed_pull_id = NULL,
                            populate_failed_verdict = NULL,
                            populate_failed_at      = NULL
                    """,
                    {
                        "id": conn_ref_id,
                        "status": health.status,
                        "last_checked_at": now,
                        "last_fetched_at": health.last_fetched_at,
                    },
                )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: refresh_health upsert_error: %s", exc)
        return JSONResponse(
            {
                "code": "db_error",
                "message": (
                    "The health of this connection was read but could not be "
                    "recorded. Re-run the check in a moment, and if it keeps "
                    "failing ask an administrator to read the server log."
                ),
            },
            status_code=500,
        )

    # AI-18: record successful refresh timestamp for per-connection rate limiting.
    _refresh_health_last[conn_ref_id] = time.monotonic()
    # review-3-2 F-3: bound the in-memory rate-limit map -- drop entries older
    # than 10 minutes whenever it grows past 1000 keys.
    if len(_refresh_health_last) > 1000:
        _cutoff = time.monotonic() - 600
        for _k in [k for k, v in _refresh_health_last.items() if v < _cutoff]:
            _refresh_health_last.pop(_k, None)

    health_payload = {
        "status": health.status,
        "last_checked_at": now.isoformat(),
        "last_fetched_at": health.last_fetched_at.isoformat() if health.last_fetched_at else None,
    }

    return JSONResponse({"id": conn_ref_id, "health": health_payload})

async def _trigger_pull(request: Request) -> Response:
    """POST /api/connections/{id}/pull -- enqueue a pull job (Story 3.2, AC4).

    Rewired from Story 2.7 (synchronous pull) to Story 3.2 (queue dispatch).
    The pull_id is now minted inside enqueue_pull() (AD-7). The audit row
    (ACTION_PULL_TRIGGERED) is written inside enqueue_pull() -- not here.

    Request body (JSON, optional):
        {"date_from": "YYYY-MM-DD", "date_to": "YYYY-MM-DD"}
    Defaults to a 7-day window ending yesterday if omitted.

    Response (202 Accepted):
        {"job_id", "pull_id", "state": "queued"}

    Error responses:
        401 -- unauthorized
        404 -- connection_ref not found
        422 -- invalid date format in body
        500 -- DB or enqueue error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {
                "code": "unauthorized",
                "message": (
                    "This request carries no signed-in identity. "
                    "Sign in again, then re-run it."
                ),
            },
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    if not conn_ref_id:
        return JSONResponse(
            {
                "code": "missing_id",
                "message": (
                    "This request does not say which connection to act on. "
                    "Add the connection identifier to the address and re-run it."
                ),
            },
            status_code=400,
        )

    # Parse optional JSON body for date_from / date_to
    date_from: str
    date_to: str
    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception:
        body = {}

    default_date_to = (date.today() - timedelta(days=1)).isoformat()
    default_date_from = (date.today() - timedelta(days=7)).isoformat()

    date_to = (body.get("date_to") or default_date_to).strip()
    date_from = (body.get("date_from") or default_date_from).strip()

    if not _ISO_DATE_RE.match(date_from):
        return JSONResponse(
            {
                "code": "invalid_date",
                "message": (
                    "The start of the collection window is not a date. Enter "
                    "`date_from` as YYYY-MM-DD and re-run the request."
                ),
            },
            status_code=422,
        )
    if not _ISO_DATE_RE.match(date_to):
        return JSONResponse(
            {
                "code": "invalid_date",
                "message": (
                    "The end of the collection window is not a date. Enter "
                    "`date_to` as YYYY-MM-DD and re-run the request."
                ),
            },
            status_code=422,
        )

    # Verify the credential exists AND that the caller may use it.
    #
    # L'autorisation se resout sur l'ORGANISATION proprietaire, jamais sur la
    # personne qui a branche l'acces : c'est precisement ce qui permet a un
    # collegue de rafraichir un report sans posseder lui-meme l'acces a la
    # source. Un controle `appelant == owner_identity` bloquerait la sync des
    # que le proprietaire n'est pas la.
    #
    # Avant ce garde-fou, ce handler ne verifiait QUE l'existence : n'importe
    # quelle identite authentifiee pouvait declencher une sync sur n'importe
    # quel credential, y compris celui d'une autre organisation.
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_credential_org_read(conn_ref_id, identity, conn)
            if denied is not None:
                write_audit_row(
                    identity=identity or "anonymous",
                    action=ACTION_CROSS_SCOPE_ATTEMPT,
                    provider_account="",
                    connection_ref=conn_ref_id,
                    metadata={
                        "reason": "credential_not_in_caller_org",
                        "operation": "trigger_pull",
                    },
                )
                return denied
    except Exception as exc:
        logger.error("admin_api: trigger_pull db_error: %s", exc)
        return JSONResponse(
            {
                "code": "db_error",
                "message": (
                    "Your access to this connection could not be checked just "
                    "now, so nothing was collected. Re-run the request in a "
                    "moment, and if it keeps failing ask an administrator to "
                    "read the server log."
                ),
            },
            status_code=500,
        )

    # Enqueue the pull job (AD-7: pull_id minted inside enqueue_pull)
    try:
        from core.queue import enqueue_pull  # noqa: PLC0415

        result = enqueue_pull(
            conn_ref_id,
            date_from,
            date_to,
            requested_by=identity,
        )
    except Exception as exc:
        logger.error("admin_api: trigger_pull enqueue_error: %s", exc)
        return JSONResponse(
            {
                "code": "enqueue_error",
                "message": (
                    "This collection could not be queued just now. Nothing is "
                    "running; re-run the request in a moment, and if it keeps "
                    "failing ask an administrator to read the server log."
                ),
            },
            status_code=500,
        )

    # Story 25.5 review F-1: a topology-declaring provider without a selected
    # + verified reporting account returns a refusal dict (no job_id/pull_id).
    # Surface it as an actionable 409 instead of KeyError-ing into a 500.
    if result.get("state") == "refused":
        return JSONResponse(
            {
                "code": result.get("code", "account_not_selected"),
                "message": result.get(
                    "message",
                    "Choose the reporting account this connection collects "
                    "from, verify it, then run the collection again.",
                ),
            },
            status_code=409,
        )

    return JSONResponse(
        {
            "job_id": result["job_id"],
            "pull_id": result["pull_id"],
            "state": result["state"],
        },
        status_code=202,
    )

async def _list_connection_connectors(request: Request) -> Response:
    """GET /api/connections/{id}/connectors -- what this authorization opens, and what it does not.

    The wizard's second question. One Google consent screen grants scopes for
    Search Console, Analytics, Ads, Sheets and three more, so "the provider" is
    not the answer to "what can I build from this?" -- the granted scopes are.
    Every Nango connection returns exactly one Connector, which is why no caller
    of the single-Connector paths ever had to ask.

    `unlocked_by_reconsent` est la seconde moitie (AI-112) : les Connecteurs que
    ce deploiement installe, que le projet active, et dont le scope est DEMANDE
    sans etre accorde. Ajouter un scope au produit ne met a jour aucune
    autorisation deja emise -- trois Connecteurs sont restes invisibles des
    semaines pour ce motif, sans que rien ne le dise a personne.

    Ce champ ne dit PAS que la personne a refuse : `connection_ref` ne garde pas
    les scopes demandes au moment du consentement, donc << decoche >> et
    << n'existait pas encore >> sont indistinguables ici. Le libelle doit rester
    une offre, jamais un reproche.

    404 unknown/foreign/inactive connection (fail-closed, indistinguishable);
    503 when Connector enablement or project scope cannot be read.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {
                "code": "unauthorized",
                "message": (
                    "This request carries no signed-in identity. "
                    "Sign in again, then re-run it."
                ),
            },
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    project_id = request.query_params.get("project_id", "").strip()
    if not conn_ref_id or not project_id:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": (
                    "This request does not name both the connection and the "
                    "Project. Add the connection identifier to the address and "
                    "`project_id` to the request, then re-run it."
                ),
            },
            status_code=400,
        )

    from core.connection_tools import (  # noqa: PLC0415
        ConnectionConnectorsNotFound,
        ConnectionConnectorsUnavailable,
        list_connection_connectors,
        list_unopened_connectors,
    )
    from core.db import get_connection  # noqa: PLC0415
    from core.main import get_loaded_modules  # noqa: PLC0415

    try:
        with get_connection() as conn:
            modules = get_loaded_modules()
            connectors = list_connection_connectors(
                project_id=project_id,
                connection_ref_id=conn_ref_id,
                identity=identity or "anonymous",
                loaded_modules=modules,
                conn=conn,
            )
            # AI-112 : la meme question, sa seconde moitie. Ajoute ICI plutot que
            # sur une route voisine parce que << ce que cette autorisation ouvre >>
            # et << ce qu'un re-consentement ajouterait >> ne se lisent QUE
            # ensemble : la premiere seule est ce qui a laisse trois Connecteurs
            # installes invisibles pendant des semaines.
            unopened = list_unopened_connectors(
                project_id=project_id,
                connection_ref_id=conn_ref_id,
                identity=identity or "anonymous",
                loaded_modules=modules,
                conn=conn,
            )
    except ConnectionConnectorsNotFound:
        return JSONResponse(
            {
                "code": "not_found",
                "message": (
                    "No connection with this identifier exists in this Project. "
                    "Choose a connection from the Project's connection list, or "
                    "check the identifier in the address."
                ),
            },
            status_code=404,
        )
    except ConnectionConnectorsUnavailable:
        return JSONResponse(
            {
                "code": "connectors_unavailable",
                "message": (
                    "What this authorization opens could not be read just now. "
                    "Re-run the request in a moment, and if it keeps failing ask "
                    "an administrator to read the server log."
                ),
            },
            status_code=503,
        )
    except Exception as exc:  # noqa: BLE001 -- stable public failure contract
        logger.warning(
            "admin_api: list_connection_connectors failed: %s", type(exc).__name__
        )
        return JSONResponse(
            {
                "code": "connectors_unavailable",
                "message": (
                    "What this authorization opens could not be read just now. "
                    "Re-run the request in a moment, and if it keeps failing ask "
                    "an administrator to read the server log."
                ),
            },
            status_code=503,
        )

    return JSONResponse(
        {
            "connection_ref_id": conn_ref_id,
            "connectors": [connector.as_dict() for connector in connectors],
            # Vide quand rien ne manque, et vide pour toute connexion Nango. Un
            # tableau vide est la reponse, pas une absence : la console doit
            # pouvoir faire disparaitre l'invite au lieu de rassurer.
            "unlocked_by_reconsent": [connector.as_dict() for connector in unopened],
        }
    )

async def _list_connection_accounts(request: Request) -> Response:
    """GET /api/connections/{id}/accounts -- discover the reachable accounts (AC3).

    Runs the module's discovery callable (token via the existing connection path)
    and returns the account>property hierarchy. 409 when the module declares no
    account topology; 404 unknown connection; 403 cross-project (AD-5).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {
                "code": "unauthorized",
                "message": (
                    "This request carries no signed-in identity. "
                    "Sign in again, then re-run it."
                ),
            },
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    if not conn_ref_id:
        return JSONResponse(
            {
                "code": "missing_id",
                "message": (
                    "This request does not say which connection to act on. "
                    "Add the connection identifier to the address and re-run it."
                ),
            },
            status_code=400,
        )

    # WHICH Connector of the authorization to list properties for. One Google
    # consent opens Search Console, Analytics, Ads and Sheets, and their property
    # lists are different lists -- the wizard asks after the Connector is chosen.
    requested_module = request.query_params.get("connector", "").strip() or None

    try:
        from core import account_topology  # noqa: PLC0415
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity) as conn:
            scope_ref, err = _resolve_conn_project_scoped(conn_ref_id, identity, conn)
            if err is not None:
                return err
            module_name: str | None = None
            if requested_module:
                from core.connection_tools import (  # noqa: PLC0415
                    ConnectionConnectorsNotFound,
                    resolve_connection_connector,
                )
                from core.main import get_loaded_modules  # noqa: PLC0415

                try:
                    module_name = resolve_connection_connector(
                        project_id=str(scope_ref.get("project_id") or ""),
                        connection_ref_id=conn_ref_id,
                        identity=identity or "anonymous",
                        loaded_modules=get_loaded_modules(),
                        conn=conn,
                        requested_connector=requested_module,
                    )
                except ConnectionConnectorsNotFound:
                    return JSONResponse(
                        {
                            "code": "not_found",
                            "message": (
                                "This authorization does not open that "
                                "connector. Choose one of the connectors this "
                                "authorization covers, then re-run the request."
                            ),
                        },
                        status_code=404,
                    )
            discovered = account_topology.discover_accounts(conn_ref_id, module=module_name)
    except account_topology.NoTopologyError:
        return JSONResponse(
            {
                "code": "no_account_topology",
                "message": (
                    "This connector has no account to choose: it collects "
                    "everything the authorization already reaches. Run the "
                    "collection on this connection directly."
                ),
            },
            status_code=409,
        )
    except account_topology.ConnectionNotFound:
        return JSONResponse(
            {
                "code": "not_found",
                "message": (
                    "No connection with this identifier exists in this Project. "
                    "Choose a connection from the Project's connection list, or "
                    "check the identifier in the address."
                ),
            },
            status_code=404,
        )
    except Exception as exc:  # noqa: BLE001
        # Typed connector errors (auth_expired, etc.) and transport failures land
        # here; expose the class so the shell can render a reconnect affordance.
        error_class = getattr(exc, "error_class", None)
        logger.error(
            "admin_api: list_accounts discovery_error conn=%s class=%s: %s",
            conn_ref_id,
            error_class,
            type(exc).__name__,
        )
        return JSONResponse(
            {
                "code": error_class or "discovery_error",
                "message": (
                    "The accounts this authorization reaches could not be "
                    "listed. Re-run the listing in a moment; if it keeps "
                    "failing, authorize the source again from this connection."
                ),
            },
            status_code=502,
        )

    return JSONResponse(
        {
            "connection_ref_id": conn_ref_id,
            "topology": discovered.get("topology"),
            "accounts": discovered.get("accounts"),
        }
    )

async def _select_connection_account(request: Request) -> Response:
    """POST /api/connections/{id}/account {account_id} -- select + verify (AC3).

    Verifies access to the requested account (minimal read via the discovery
    path), persists the scope with state='ready' + verified_at, writes an audit
    row, then enqueues a bounded TRIAL pull (last 3 days) via enqueue_pull.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {
                "code": "unauthorized",
                "message": (
                    "This request carries no signed-in identity. "
                    "Sign in again, then re-run it."
                ),
            },
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    if not conn_ref_id:
        return JSONResponse(
            {
                "code": "missing_id",
                "message": (
                    "This request does not say which connection to act on. "
                    "Add the connection identifier to the address and re-run it."
                ),
            },
            status_code=400,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception:
        body = {}
    account_id = (body.get("account_id") or "").strip()
    if not account_id:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": (
                    "This request does not say which account to select. "
                    "Add `account_id` to the request and re-run it."
                ),
            },
            status_code=422,
        )

    # WHICH tool the account belongs to. The listing route already takes it, and
    # verification must take the same one: a Google direct grant is stored with
    # provider='google', which declares no topology, so selecting a property the
    # screen had just listed answered 409 "no account topology". Optional, so a
    # single-Connector authorization keeps working unchanged.
    requested_module = (
        (body.get("connector") or "").strip()
        or (request.query_params.get("connector") or "").strip()
        or None
    )

    try:
        from core import account_topology  # noqa: PLC0415
        from core.db import request_connection  # noqa: PLC0415

        with request_connection(identity) as conn:
            scope_ref, err = _resolve_conn_project_scoped(conn_ref_id, identity, conn)
            if err is not None:
                return err
            module_name: str | None = None
            if requested_module:
                from core.connection_tools import (  # noqa: PLC0415
                    ConnectionConnectorsNotFound,
                    resolve_connection_connector,
                )
                from core.main import get_loaded_modules  # noqa: PLC0415

                try:
                    module_name = resolve_connection_connector(
                        project_id=str(scope_ref.get("project_id") or ""),
                        connection_ref_id=conn_ref_id,
                        identity=identity or "anonymous",
                        loaded_modules=get_loaded_modules(),
                        conn=conn,
                        requested_connector=requested_module,
                    )
                except ConnectionConnectorsNotFound:
                    return JSONResponse(
                        {
                            "code": "not_found",
                            "message": (
                                "This authorization does not open that "
                                "connector. Choose one of the connectors this "
                                "authorization covers, then re-run the request."
                            ),
                        },
                        status_code=404,
                    )
            scope = account_topology.verify_and_select_account(
                conn_ref_id,
                account_id,
                selected_by=identity or "anonymous",
                module=module_name,
            )
    except account_topology.NoTopologyError:
        return JSONResponse(
            {
                "code": "no_account_topology",
                "message": (
                    "This connector has no account to choose: it collects "
                    "everything the authorization already reaches. Run the "
                    "collection on this connection directly."
                ),
            },
            status_code=409,
        )
    except account_topology.ConnectionNotFound:
        return JSONResponse(
            {
                "code": "not_found",
                "message": (
                    "No connection with this identifier exists in this Project. "
                    "Choose a connection from the Project's connection list, or "
                    "check the identifier in the address."
                ),
            },
            status_code=404,
        )
    except account_topology.AccountNotReachable:
        return JSONResponse(
            {
                "code": "account_not_reachable",
                "message": (
                    "This authorization does not reach the account you chose. "
                    "Choose an account it already covers, or authorize the "
                    "source again with access to this one."
                ),
            },
            status_code=422,
        )
    except Exception as exc:  # noqa: BLE001
        error_class = getattr(exc, "error_class", None)
        logger.error(
            "admin_api: select_account error conn=%s class=%s: %s",
            conn_ref_id,
            error_class,
            type(exc).__name__,
        )
        return JSONResponse(
            {
                "code": error_class or "select_account_error",
                "message": (
                    "The account could not be selected just now and nothing was "
                    "changed. Re-run the selection in a moment, and if it keeps "
                    "failing ask an administrator to read the server log."
                ),
            },
            status_code=502,
        )

    # AD-8/AD-14: audit the selection (best-effort; never blocks the response).
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ACCOUNT_SELECTED,
        provider_account=scope_ref.get("provider") if scope_ref else "",
        connection_ref=conn_ref_id,
        metadata={
            "account_id": account_id,
            "account_label": scope.get("account_label"),
            "state": scope.get("state"),
        },
    )

    # Bounded TRIAL pull (last 3 days) via the EXISTING queue path.
    trial: dict = {}
    try:
        from core.account_topology import enqueue_trial_pull  # noqa: PLC0415

        trial = enqueue_trial_pull(conn_ref_id, requested_by=identity or "anonymous")
    except Exception as exc:  # noqa: BLE001
        # Selection succeeded and is persisted; a trial-enqueue hiccup is not fatal.
        logger.warning("admin_api: select_account trial_enqueue_failed: %s", exc)
        trial = {"state": "trial_enqueue_failed"}

    return JSONResponse(
        {
            "connection_ref_id": conn_ref_id,
            "scope": {
                "account_id": scope.get("account_id"),
                "account_label": scope.get("account_label"),
                "state": scope.get("state"),
                "verified_at": scope.get("verified_at"),
            },
            "trial": trial,
        },
        status_code=201,
    )

async def _backfill_connection(request: Request) -> Response:
    """POST /api/connections/{id}/backfill {days} -- windowed backfill (AC3).

    Validates 1..365, splits into <=31-day windows, enqueues one job per window
    (existing dedup applies), and returns the window list. NEVER auto-triggered.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {
                "code": "unauthorized",
                "message": (
                    "This request carries no signed-in identity. "
                    "Sign in again, then re-run it."
                ),
            },
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    if not conn_ref_id:
        return JSONResponse(
            {
                "code": "missing_id",
                "message": (
                    "This request does not say which connection to act on. "
                    "Add the connection identifier to the address and re-run it."
                ),
            },
            status_code=400,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception:
        body = {}

    try:
        from core import account_topology  # noqa: PLC0415
        from core.db import request_connection  # noqa: PLC0415

        # Validate days BEFORE any enqueue (AC3: 1..365).
        days = account_topology.validate_backfill_days(body.get("days"))

        with request_connection(identity) as conn:
            _scope_ref, err = _resolve_conn_project_scoped(conn_ref_id, identity, conn)
        if err is not None:
            return err

        windows = account_topology.enqueue_backfill(
            conn_ref_id, days, requested_by=identity or "anonymous"
        )
    except account_topology.BackfillDaysInvalid:
        return JSONResponse(
            {
                "code": "invalid_days",
                "message": (
                    "A backfill covers a whole number of days, from 1 to 365. "
                    "Enter a length in that range and re-run the request."
                ),
            },
            status_code=422,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_api: backfill error conn=%s: %s", conn_ref_id, type(exc).__name__)
        return JSONResponse(
            {
                "code": "backfill_error",
                "message": (
                    "The backfill could not be queued just now. Nothing is "
                    "running; re-run the request in a moment, and if it keeps "
                    "failing ask an administrator to read the server log."
                ),
            },
            status_code=500,
        )

    return JSONResponse(
        {
            "connection_ref_id": conn_ref_id,
            "days": days,
            "windows": windows,
        },
        status_code=202,
    )

def _mint_conn_id() -> str:
    """Mint a new ULID with 'conn_' prefix."""
    from ulid import ULID  # noqa: PLC0415

    return f"conn_{ULID()}"

def _resolve_conn_project_scoped(conn_ref_id: str, identity: str, conn):
    """Resolve a connection_ref's project_id and enforce AD-5 access on *conn*.

    Returns a tuple ``(scope_ref, error_response)``:
      * scope_ref: {"project_id", "provider"} when the connection exists AND the
        caller has project access; None otherwise.
      * error_response: a JSONResponse (404 unknown / 403 cross-scope) when the
        access fails; None on success.
    A cross-scope refusal writes an ACTION_CROSS_SCOPE_ATTEMPT audit row (AD-8).
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    # Strict, always. The `identity_can_read_project` branch that used to sit here
    # was unreachable after Story 46.4 removed the runtime flag, and
    # `test_account_topology_seams.py` was repaired against THAT symbol -- so it
    # patched dead code while the real gate opened a socket through
    # `write_audit_row`. The branch is gone so the mistake cannot recur.
    #
    # The access context is no longer installed here: every caller acquires
    # through `request_connection`, so the connection arrives armed for
    # `identity` and arming it a second time is the double Story 21.6 removes.

    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, provider FROM app.connection_ref WHERE id = %s",
            (conn_ref_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None, JSONResponse(
            {
                "code": "not_found",
                "message": (
                    "No connection with this identifier exists in this Project. "
                    "Choose a connection from the Project's connection list, or "
                    "check the identifier in the address."
                ),
            },
            status_code=404,
        )
    project_id, provider = row[0], row[1]
    allowed = resolve_strict_resource_access(
        identity, conn, project_id=project_id, minimum_capability="view"
    ).allowed
    if not allowed:
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_CROSS_SCOPE_ATTEMPT,
            provider_account=provider or "",
            connection_ref=conn_ref_id,
            metadata={
                "project_id": project_id,
                "operation": "account_topology",
                "reason": "not_a_member",
            },
        )
        # Non-disclosing: a caller without access does not learn the connection
        # exists. This is 404 and not 403 by decision, so the fixture that still
        # expects 403 is stale, not the handler.
        return None, JSONResponse(
            {
                "code": "not_found",
                "message": (
                    "This connection is not readable under your sign-in. Choose "
                    "a connection from a Project you have access to, or ask an "
                    "administrator of this organisation to grant it."
                ),
            },
            status_code=404,
        )
    return {"project_id": project_id, "provider": provider}, None

def _resolve_connection_create_scope(conn, project_id: str, identity: str):
    """Return ``(owner_org_id, strict)`` only for project managers.

    The second element is kept in the tuple because its callers read it to choose
    404 over 403; it is now always True, since Story 46.4 removed the flag that
    could make it False.
    """
    # The caller acquires through `request_connection`, so this connection is
    # already armed for `identity`; arming it again here is the double that
    # Story 21.6 removes.
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    allowed = resolve_strict_resource_access(
        identity,
        conn,
        project_id=project_id,
        minimum_capability="manage",
    ).allowed

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT org_id
            FROM app.projects
            WHERE id = %s AND status = 'active'
            """,
            (project_id,),
        )
        row = cur.fetchone()
    owner_org_id = row[0] if row else None
    if not owner_org_id:
        return None, True
    return (owner_org_id if allowed else None), True


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

CONNECTIONS_ROUTES_1 = [
    Route("/api/connections", endpoint=_list_connections, methods=["GET"]),
    Route("/api/connections", endpoint=_create_connection, methods=["POST"]),
]

CONNECTIONS_ROUTES_2 = [
    Route(
        "/api/connections/{id}/refresh-health",
        endpoint=_refresh_health,
        methods=["POST"],
    ),
    Route(
        "/api/connections/{id}/pull",
        endpoint=_trigger_pull,
        methods=["POST"],
    ),
    # What one authorization opens. Registered before /accounts so the two
    # sibling paths stay adjacent and obviously ordered: connector, then
    # property.
    Route(
        "/api/connections/{id}/connectors",
        endpoint=_list_connection_connectors,
        methods=["GET"],
    ),
    # Story 25.5: account topology onboarding (discovery / selection / backfill).
    Route(
        "/api/connections/{id}/accounts",
        endpoint=_list_connection_accounts,
        methods=["GET"],
    ),
    Route(
        "/api/connections/{id}/account",
        endpoint=_select_connection_account,
        methods=["POST"],
    ),
    Route(
        "/api/connections/{id}/backfill",
        endpoint=_backfill_connection,
        methods=["POST"],
    ),
]
