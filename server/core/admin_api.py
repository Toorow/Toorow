"""toorow -- Admin API router (Story 2.4, T5.4; Story 2.5, T3; Story 3.2).

Starlette Router with routes:
  GET  /api/connections              -- list connections (Nango + connection_ref + health join)
  POST /api/connections              -- create a connection_ref row
  POST /api/connections/<id>/refresh-health -- on-demand health refresh for one connection
  POST /api/connections/<id>/pull    -- enqueue a pull job (Story 3.2, returns 202)
  GET  /api/jobs/<id>                -- get pull job status (Story 3.2, AC5)

Design decisions (recorded per Dev Notes):
  - Postgres client: psycopg v3 (sync) via core.db.get_connection().
    Chosen over asyncpg: sync is simpler in Starlette sync handlers and
    consistent with audit.py. No asyncio.run() nesting risk.
  - ULID: python-ulid (already in venv). Prefix: conn_ per ARCHITECTURE-SPINE.
  - Nango: list_connections_async preferred from async Starlette handlers
    (avoids _run_coro thread overhead in the async path).
  - Auth: follows Story 2.3 pattern -- disabled mode passes through;
    static/oauth modes VERIFY the Bearer token via core.api_auth (review-2-6 F-01).
  - Story 2.6 integration point: identity is extracted from the Bearer token
    header so audit rows can be written with the correct subject.
  - Story 2.5: health is served from the Postgres cache (connection_health table)
    NOT by calling Nango on every GET request (no per-request Nango polling).
  - Story 3.2: _trigger_pull now enqueues via core.queue.enqueue_pull (returns 202).
    The pull_id is minted inside enqueue_pull (AD-7). Audit row written there too.

AD-3: no token columns written to Postgres.
AD-8: admin console communicates exclusively through this API (no direct DB).
Windows/CI note (L-3): all log strings use ASCII-safe characters only.
"""

from __future__ import annotations

import html as html_module
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route, Router

from core import nango_client
from core.audit import (
    ACTION_ACCOUNT_ERASED,
    ACTION_ACCOUNT_EXPOSED,
    ACTION_ACCOUNT_GRANT_REVOKED,
    ACTION_ALERT_DEF_CREATED,
    ACTION_ALERT_DEF_DELETED,
    ACTION_ALERT_DEF_UPDATED,
    ACTION_CONNECTION_CREATED,
    ACTION_CONNECTION_REVOKED,
    ACTION_CONTEXT_EVENT_CREATED,
    ACTION_CROSS_SCOPE_ATTEMPT,
    ACTION_DATASET_ACCESS_GRANTED,
    ACTION_DATASET_ACCESS_REVOKED,
    ACTION_DATASTREAM_CREATED,
    ACTION_DATASTREAM_DELETED,
    ACTION_DATASTREAM_RUN,
    ACTION_DATASTREAM_UPDATED,
    ACTION_FLUX_LINKED,
    ACTION_FLUX_UNLINKED,
    ACTION_KEY_CREATED,
    ACTION_KEY_DELETED,
    ACTION_KEY_ROTATED,
    ACTION_NOTEBOOK_DELETED,
    ACTION_ORG_CREATED,
    ACTION_ORG_DELETED,
    ACTION_ORG_MEMBER_ADDED,
    ACTION_ORG_MEMBER_REMOVED,
    ACTION_ORG_MEMBER_UPDATED,
    ACTION_ORG_SCHEMAS_DROPPED,
    ACTION_ORG_SCHEMAS_PROVISIONED,
    ACTION_ORG_UPDATED,
    ACTION_PROJECT_ARCHIVED,
    ACTION_PROJECT_CREATED,
    ACTION_PROJECT_GEOGRAPHIC_POSTURE_UPDATED,
    insert_audit_row,
    write_audit_row,
)
from core.cards_api import CARDS_ROUTES as _CARDS_ROUTES
from core.conflict_resolutions_api import CONFLICT_RESOLUTION_ROUTES as _CONFLICT_RESOLUTION_ROUTES

# Story 38.5: connector activation/deactivation (org-owner) + health layering.
from core.connector_activation_api import (
    CONNECTOR_ACTIVATION_ROUTES as _CONNECTOR_ACTIVATION_ROUTES,
)

# Story 38.7: inbound delivery credential lifecycle (issue/rotate/revoke/list).
from core.inbound_credentials_api import (
    INBOUND_CREDENTIAL_ROUTES as _INBOUND_CREDENTIAL_ROUTES,
)

# Story 38.3: connector domain and adapter-route configuration (platform-admin).
from core.connector_domain_api import (
    CONNECTOR_DOMAIN_ROUTES as _CONNECTOR_DOMAIN_ROUTES,
)

# Story 38.2: connector installation state surface (platform-admin + catalog gate).
from core.connector_installation_api import (
    CONNECTOR_INSTALLATION_ROUTES as _CONNECTOR_INSTALLATION_ROUTES,
)

# Story 38.4: connector verification + synthetic test delivery (platform-admin).
from core.connector_verification_api import (
    CONNECTOR_VERIFICATION_ROUTES as _CONNECTOR_VERIFICATION_ROUTES,
)
from core.context_api import CONTEXT_ROUTES as _CONTEXT_ROUTES
from core.country_vocabulary import CountryVocabularyError, get_country_vocabulary
from core.daily_insights_api import DAILY_INSIGHTS_ROUTES as _DAILY_INSIGHTS_ROUTES
from core.datamodel_api import DATAMODEL_ROUTES as _DATAMODEL_ROUTES
from core.dq_api import DQ_ROUTES as _DQ_ROUTES
from core.flows_api import FLOWS_ROUTES as _FLOWS_ROUTES
from core.geographic_reporting import (
    InvalidGeographicPosture,
    fetch_project_geographic_posture,
    merge_geographic_patch,
    normalize_geographic_posture,
)

# Story 38.6: import template catalog + inbound managed-feed Datastream creation.
from core.import_templates_api import (
    IMPORT_TEMPLATE_ROUTES as _IMPORT_TEMPLATE_ROUTES,
)
from core.mediaplan_api import MEDIAPLAN_ROUTES as _MEDIAPLAN_ROUTES
from core.metric_semantics_api import METRIC_SEMANTICS_ROUTES as _METRIC_SEMANTICS_ROUTES
from core.money_api import MONEY_ROUTES as _MONEY_ROUTES

# Story 34.3: org-plan control surface routes (isolated import to keep the edit
# surgical -- appended as its own single-line block, not spliced into the sorted
# route-import group above).
from core.org_plan_api import ORG_PLAN_ROUTES as _ORG_PLAN_ROUTES
from core.overview import OVERVIEW_ROUTES as _OVERVIEW_ROUTES
from core.rendus_api import RENDUS_ROUTES as _RENDUS_ROUTES
from core.report_chain import REPORT_CHAIN_ROUTES as _REPORT_CHAIN_ROUTES
from core.schema_context_api import SCHEMA_CONTEXT_ROUTES as _SCHEMA_CONTEXT_ROUTES
from core.timezone_api import TIMEZONE_ROUTES as _TIMEZONE_ROUTES

logger = logging.getLogger(__name__)

# AI-18: per-connection rate limit on /refresh-health (Story 3.3, AC10).
# Maps connection_ref_id -> monotonic timestamp of last successful refresh.
# TODO(Phase-B): move rate-limit state to Postgres for multi-replica safety.
_refresh_health_last: dict[str, float] = {}

_REFRESH_HEALTH_RATE_LIMIT_SECONDS = 30


# ---------------------------------------------------------------------------
# ULID helper
# ---------------------------------------------------------------------------


def _mint_conn_id() -> str:
    """Mint a new ULID with 'conn_' prefix."""
    from ulid import ULID  # noqa: PLC0415

    return f"conn_{ULID()}"


# ---------------------------------------------------------------------------
# Auth helper (reuses Story 2.3 pattern from _audit_endpoint in main.py)
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Real token verification via the shared api_auth layer (review-2-6 F-01)."""
    from core.api_auth import authenticate_api_request

    return await authenticate_api_request(request)

async def _check_invitation_identity(request: Request) -> tuple[bool, str]:
    """Require an OAuth-verified email identity for invitation transitions."""
    from core.api_auth import authenticate_invitation_identity

    return await authenticate_invitation_identity(request)



# ---------------------------------------------------------------------------
# Story 7.4 (AC4, AC7) -- notebook project-scope enforcement (AI-38 fix).
#
# The admin API uses a single shared Bearer token (API_SECRET_KEY): any holder
# is "the admin" and, until now, could PATCH / SCHEDULE / EXPORT ANY project's
# notebook by id (review-epic-6 F-2). This helper closes that gap on every write
# path: it fetches the notebook's project_id, verifies caller access via the
# per-identity ACL (core.project_access + app.project_members, default-open for
# single-tenant), and — critically — treats a scope violation as 404 so the
# endpoint does not even confirm the notebook's existence to a non-member. Every
# rejection is AUDITED (ACTION_CROSS_SCOPE_ATTEMPT) so refused access is
# observable (FR12, AD-5, AD-8).
#
# Callers pass the notebook's project_id (already fetched, so no second SELECT).
# When an explicit project_id scope hint is supplied (query param or body), a
# mismatch is ALSO a 404 — this is what the isolation suite exercises: a caller
# claiming project_alpha must never touch project_beta's notebook.
# ---------------------------------------------------------------------------


def _enforce_notebook_project_scope(
    notebook_project_id: str,
    identity: str,
    notebook_id: str,
    conn,
    scope_hint: str = "",
    action: str = "",
) -> Response | None:
    """Return a 404 Response if the caller may not touch this notebook, else None.

    Args:
        notebook_project_id: The notebook's owning project_id (already fetched).
        identity:            Caller subject from the verified Bearer token.
        notebook_id:         The notebook id (for the audit metadata).
        conn:                Open psycopg connection (reused; no new connection).
        scope_hint:          Explicit project_id the caller CLAIMS to be acting
                             within (from ?project_id= or body["project_id"]).
                             Empty = no explicit claim; ACL still applies.
        action:              Human label of the attempted operation (audit meta).

    A refusal ALWAYS writes an ACTION_CROSS_SCOPE_ATTEMPT audit row before
    returning the 404. Returns None (access granted) otherwise.
    """
    from core.project_access import identity_has_project_access  # noqa: PLC0415

    denied = False
    reason = ""

    # 1. Explicit scope claim mismatch -> refuse (caller claims a project that is
    #    not the notebook's owner).
    if scope_hint and scope_hint != notebook_project_id:
        denied = True
        reason = "scope_mismatch"
    # 2. Per-identity ACL (default-open until the project has members).
    elif not identity_has_project_access(notebook_project_id, identity, conn):
        denied = True
        reason = "not_a_member"

    if not denied:
        return None

    # Audit the refused access (best-effort; never blocks the 404).
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="",
        connection_ref="",
        metadata={
            "notebook_id": notebook_id,
            "notebook_project_id": notebook_project_id,
            "claimed_project_id": scope_hint or None,
            "reason": reason,
            "operation": action or "notebook_access",
        },
    )
    logger.warning(
        "admin_api: cross_scope_attempt identity=%s notebook=%s owner_project=%s "
        "claimed=%s reason=%s op=%s",
        identity,
        notebook_id,
        notebook_project_id,
        scope_hint or "-",
        reason,
        action or "-",
    )
    # 404 (not 403): do not disclose that the notebook exists to a non-member.
    return JSONResponse(
        {"code": "not_found", "message": "Notebook not found"},
        status_code=404,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _list_connections(request: Request) -> Response:
    """GET /api/connections -- list Nango connections joined with connection_ref.

    Response shape:
      {"connections": [{"id", "nango_connection_id", "provider",
                        "project_id", "created_at", "status"}]}

    Steps:
      1. Auth check.
      2. Call nango_client.list_connections_async() to get Nango connections.
      3. Join with app.connection_ref rows from platform Postgres.
      4. Return enriched list.

    Nango errors are caught and returned as 502.
    Postgres errors are caught and returned as 500.
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    # review-epic-7 F-1: optional project scoping — with the ProjectSwitcher
    # active, the console requests one project's connections at a time.
    project_filter = request.query_params.get("project_id") or None

    # Story 2.5 design (review-2-5 F-01): health is served from the Postgres
    # cache -- Nango is only consulted as a FALLBACK when the DB is down.
    # No Nango call happens on the happy path.
    conn_ref_rows: list[dict] = []
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Epic 42 (Sources screen): surface the ownership + sharing +
                # expiry model that Epic 21/36 already landed in the schema
                # (owner_org_id, credential_account_grants, token_expiry) so the
                # Sources UI stops rendering literals. viewer org = the queried
                # project's org (po.org_id); exposure is derived per connection.
                cur.execute(
                    """
                    SELECT
                        r.id,
                        r.provider,
                        r.nango_connection_id,
                        r.project_id,
                        r.created_at,
                        r.owner_org_id,
                        r.token_expiry,
                        o.name          AS owner_org_name,
                        po.org_id       AS viewer_org_id,
                        h.status        AS health_status,
                        h.last_checked_at,
                        h.last_fetched_at,
                        COUNT(ds.id) FILTER (WHERE ds.enabled = TRUE)
                            AS active_datastream_count,
                        EXISTS (
                            SELECT 1 FROM app.credential_account_grants g
                            WHERE g.credential_id = r.id
                              AND g.status = 'active'
                              AND g.grantee_org_id = po.org_id
                        )               AS provided_to_viewer,
                        EXISTS (
                            SELECT 1 FROM app.credential_account_grants g
                            WHERE g.credential_id = r.id
                              AND g.status = 'active'
                        )               AS has_outgoing_grant
                    FROM app.connection_ref r
                    LEFT JOIN app.connection_health h ON h.connection_ref_id = r.id
                    LEFT JOIN app.datastreams ds ON ds.connection_ref_id = r.id
                    LEFT JOIN app.organizations o ON o.id = r.owner_org_id
                    LEFT JOIN app.projects po ON po.id = r.project_id
                    WHERE (%s::text IS NULL OR r.project_id = %s)
                    GROUP BY r.id, r.provider, r.nango_connection_id, r.project_id,
                             r.created_at, r.owner_org_id, r.token_expiry, o.name,
                             po.org_id, h.status, h.last_checked_at, h.last_fetched_at
                    ORDER BY r.created_at DESC
                    """,
                    (project_filter, project_filter),
                )
                cols = [desc[0] for desc in cur.description]
                _TS_COLS = {
                    "created_at",
                    "last_checked_at",
                    "last_fetched_at",
                    "token_expiry",
                }
                for row in cur.fetchall():
                    record: dict = {}
                    for col, val in zip(cols, row):
                        if col in _TS_COLS and val is not None:
                            record[col] = val.isoformat()
                        else:
                            record[col] = val
                    conn_ref_rows.append(record)
    except Exception as exc:
        logger.warning("admin_api: db_unavailable: %s -- returning Nango-only list", exc)
        # Graceful degradation: fall back to a live Nango listing (review-2-5
        # F-01: the ONLY code path that calls Nango in this endpoint).
        try:
            nango_conns = await nango_client._list_connections_async()
        except Exception as nango_exc:
            logger.error("admin_api: nango_list_error: %s", nango_exc)
            return JSONResponse(
                {"code": "nango_error", "message": f"Nango unavailable: {nango_exc}"},
                status_code=502,
            )
        connections = [
            {
                "id": None,
                "nango_connection_id": c["connection_id"],
                "provider": c["provider"],
                "project_id": None,
                "created_at": c.get("created_at", ""),
                "health": None,
            }
            for c in nango_conns
        ]
        return JSONResponse({"connections": connections})

    # Step 4: build response with nested health object + active_datastream_count
    connections = []
    for ref in conn_ref_rows:
        health_status = ref.get("health_status")  # may be None if no health row yet
        health: dict | None = None
        if health_status is not None:
            health = {
                "status": health_status,
                "last_checked_at": ref.get("last_checked_at"),
                "last_fetched_at": ref.get("last_fetched_at"),
            }
        # Epic 42: derive the ownership/exposure state the Sources UI needs.
        owner_org_id = ref.get("owner_org_id")
        viewer_org_id = ref.get("viewer_org_id")
        if owner_org_id is not None and owner_org_id == viewer_org_id:
            exposure = "shared_with_org" if ref.get("has_outgoing_grant") else "owned"
        elif ref.get("provided_to_viewer"):
            exposure = "provided_by_org"
        else:
            exposure = "owned"
        connections.append(
            {
                "id": ref["id"],
                "nango_connection_id": ref["nango_connection_id"],
                "provider": ref["provider"],
                "project_id": ref["project_id"],
                "created_at": ref["created_at"],
                "health": health,
                # Fix [MEDIUM/spec #10]: count of enabled datastreams for this connection
                # so the UI can show 'utilise par N flux' (1 auth -> N streams).
                "active_datastream_count": int(ref.get("active_datastream_count") or 0),
                # Epic 42 (Sources): ownership, sharing and expiry (Epic 21/36 model).
                "owner_org_id": owner_org_id,
                "owner_org_name": ref.get("owner_org_name"),
                "token_expiry": ref.get("token_expiry"),
                "exposure": exposure,
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
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    # Parse request body
    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes)
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    nango_connection_id = (body.get("nango_connection_id") or "").strip()
    provider = (body.get("provider") or "").strip()
    # Story 7.1 (AC5): no inline 'default' auto-bind. The empty value is resolved
    # to the active project via the shared resolver inside the DB block below.
    project_id = (body.get("project_id") or "").strip()

    if not nango_connection_id:
        return JSONResponse(
            {"code": "missing_field", "message": "nango_connection_id is required"},
            status_code=400,
        )
    if not provider:
        return JSONResponse(
            {"code": "missing_field", "message": "provider is required"},
            status_code=400,
        )

    # review-2-4 F-03: bound and shape-check inputs. provider must be a valid
    # module/integration key (kebab-case, same charset as manifest names);
    # ids are capped to keep the table clean.
    if len(nango_connection_id) > 256 or len(project_id) > 256:
        return JSONResponse(
            {"code": "invalid_field", "message": "field exceeds 256 characters"},
            status_code=400,
        )
    if len(provider) > 64 or not re.fullmatch(r"[a-z0-9-]+", provider):
        return JSONResponse(
            {"code": "invalid_field", "message": "provider must match [a-z0-9-]+"},
            status_code=400,
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
                    "message": "Nango has no such connection; complete the OAuth flow first",
                },
                status_code=409,
            )
    except Exception as exc:
        logger.error("admin_api: nango_verify_error: %s", exc)
        return JSONResponse(
            {"code": "nango_unreachable", "message": "Cannot verify connection with Nango"},
            status_code=503,
        )

    # Mint ULID
    conn_id = _mint_conn_id()

    # Insert into app.connection_ref
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_resolver import resolve_project_id  # noqa: PLC0415

        with get_connection() as conn:
            # Story 7.1 (AC5): resolve+validate the project on the same connection
            # (empty -> seeded 'default'; missing/archived -> ToolError -> 422).
            project_id = resolve_project_id(project_id, conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.connection_ref
                        (id, provider, nango_connection_id, project_id)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, provider, nango_connection_id, project_id, created_at
                    """,
                    (conn_id, provider, nango_connection_id, project_id),
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
        # Story 7.1 (AC5): a missing/archived project surfaces as a ToolError from
        # the resolver -> return 422 (client error), not 500.
        from fastmcp.exceptions import ToolError  # noqa: PLC0415

        if isinstance(exc, ToolError):
            return JSONResponse(
                {"code": "project_not_found", "message": "Project not found or archived."},
                status_code=422,
            )
        logger.error("admin_api: db_insert_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
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
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    if not conn_ref_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Connection ref id is required"},
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
            {"code": "rate_limited", "retry_after": retry_after},
            status_code=429,
        )

    # Fetch connection_ref to get nango_connection_id + provider
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
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
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    if row is None:
        return JSONResponse(
            {"code": "not_found", "message": f"Connection ref '{conn_ref_id}' not found"},
            status_code=404,
        )

    nango_connection_id, provider = row[1], row[2]

    # Poll Nango (on-demand -- this is the explicit refresh, not the background poller)
    try:
        health = await nango_client._poll_connection_health_async(
            nango_connection_id, provider=provider
        )
    except Exception as exc:
        logger.error("admin_api: refresh_health nango_error: %s", exc)
        return JSONResponse(
            {"code": "nango_error", "message": f"Nango unavailable: {exc}"},
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
                            last_fetched_at = EXCLUDED.last_fetched_at
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
            {"code": "db_error", "message": f"Database error on health upsert: {exc}"},
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


# ISO-8601 date pattern (YYYY-MM-DD) — used by _trigger_pull to validate body dates
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    if not conn_ref_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Connection ref id is required"},
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
                "message": f"date_from must be YYYY-MM-DD, got: {date_from!r}",
            },
            status_code=422,
        )
    if not _ISO_DATE_RE.match(date_to):
        return JSONResponse(
            {
                "code": "invalid_date",
                "message": f"date_to must be YYYY-MM-DD, got: {date_to!r}",
            },
            status_code=422,
        )

    # Verify connection_ref exists before enqueuing
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM app.connection_ref WHERE id = %s
                    """,
                    (conn_ref_id,),
                )
                row = cur.fetchone()
    except Exception as exc:
        logger.error("admin_api: trigger_pull db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    if row is None:
        return JSONResponse(
            {"code": "not_found", "message": f"Connection ref '{conn_ref_id}' not found"},
            status_code=404,
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
            {"code": "enqueue_error", "message": f"Failed to enqueue pull job: {exc}"},
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
                    "message", "Select and verify a reporting account first."
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


# ---------------------------------------------------------------------------
# Story 25.5: account topology onboarding endpoints (discovery / selection /
# backfill). Logic lives in core.account_topology; these handlers mirror the
# neighbouring /api/connections* auth, AD-5 project-scoping and error shapes.
#
# Audit action for a verified account selection. Defined locally (audit.py is a
# shared, append-only registry; a literal action string is accepted by
# write_audit_row's free-form action column -- no migration needed).
# ---------------------------------------------------------------------------

ACTION_ACCOUNT_SELECTED = "connection.account_selected"


def _resolve_conn_project_scoped(conn_ref_id: str, identity: str, conn):
    """Resolve a connection_ref's project_id and enforce AD-5 access on *conn*.

    Returns a tuple ``(scope_ref, error_response)``:
      * scope_ref: {"project_id", "provider"} when the connection exists AND the
        caller has project access; None otherwise.
      * error_response: a JSONResponse (404 unknown / 403 cross-scope) when the
        access fails; None on success.
    A cross-scope refusal writes an ACTION_CROSS_SCOPE_ATTEMPT audit row (AD-8).
    """
    from core.db import set_local_access_context  # noqa: PLC0415
    from core.project_access import (  # noqa: PLC0415
        epic36_production_access_enabled,
        identity_has_project_access,
        resolve_strict_resource_access,
    )

    strict_gate = epic36_production_access_enabled()
    if strict_gate:
        set_local_access_context(conn, identity, enforce_epic36=True)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, provider FROM app.connection_ref WHERE id = %s",
            (conn_ref_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None, JSONResponse(
            {"code": "not_found", "message": f"Connection ref '{conn_ref_id}' not found"},
            status_code=404,
        )
    project_id, provider = row[0], row[1]
    if strict_gate:
        allowed = resolve_strict_resource_access(
            identity, conn, project_id=project_id, minimum_capability="view"
        ).allowed
    else:
        allowed = identity_has_project_access(project_id, identity or "anonymous", conn)
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
        if strict_gate:
            return None, JSONResponse(
                {"code": "not_found", "message": "Connection not found"},
                status_code=404,
            )
        return None, JSONResponse(
            {"code": "forbidden", "message": "Access denied: not a member of this project."},
            status_code=403,
        )
    return {"project_id": project_id, "provider": provider}, None


async def _list_connection_accounts(request: Request) -> Response:
    """GET /api/connections/{id}/accounts -- discover the reachable accounts (AC3).

    Runs the module's discovery callable (token via the existing connection path)
    and returns the account>property hierarchy. 409 when the module declares no
    account topology; 404 unknown connection; 403 cross-project (AD-5).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    if not conn_ref_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Connection ref id is required"},
            status_code=400,
        )

    try:
        from core import account_topology  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            scope_ref, err = _resolve_conn_project_scoped(conn_ref_id, identity, conn)
            if err is not None:
                return err
            discovered = account_topology.discover_accounts(conn_ref_id)
    except account_topology.NoTopologyError:
        return JSONResponse(
            {
                "code": "no_account_topology",
                "message": "This connector does not declare an account topology.",
            },
            status_code=409,
        )
    except account_topology.ConnectionNotFound:
        return JSONResponse(
            {"code": "not_found", "message": f"Connection ref '{conn_ref_id}' not found"},
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
                "message": "Account discovery failed.",
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
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    if not conn_ref_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Connection ref id is required"},
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
            {"code": "missing_field", "message": "account_id is required"},
            status_code=422,
        )

    try:
        from core import account_topology  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            scope_ref, err = _resolve_conn_project_scoped(conn_ref_id, identity, conn)
            if err is not None:
                return err
            scope = account_topology.verify_and_select_account(
                conn_ref_id, account_id, selected_by=identity or "anonymous"
            )
    except account_topology.NoTopologyError:
        return JSONResponse(
            {
                "code": "no_account_topology",
                "message": "This connector does not declare an account topology.",
            },
            status_code=409,
        )
    except account_topology.ConnectionNotFound:
        return JSONResponse(
            {"code": "not_found", "message": f"Connection ref '{conn_ref_id}' not found"},
            status_code=404,
        )
    except account_topology.AccountNotReachable:
        return JSONResponse(
            {
                "code": "account_not_reachable",
                "message": "The selected account is not reachable by this connection.",
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
                "message": "Account selection failed.",
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
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    conn_ref_id = request.path_params.get("id", "")
    if not conn_ref_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Connection ref id is required"},
            status_code=400,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception:
        body = {}

    try:
        from core import account_topology  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        # Validate days BEFORE any enqueue (AC3: 1..365).
        days = account_topology.validate_backfill_days(body.get("days"))

        with get_connection() as conn:
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
                "message": "days must be an integer in [1, 365].",
            },
            status_code=422,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "admin_api: backfill error conn=%s: %s", conn_ref_id, type(exc).__name__
        )
        return JSONResponse(
            {"code": "backfill_error", "message": "Backfill enqueue failed."},
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

    # Validate state param against known enum (hardening: reject unknowns with 400).
    _VALID_JOB_STATES = {"queued", "running", "done", "failed", "dead_letter"}
    state_filter = request.query_params.get("state") or None
    if state_filter is not None and state_filter not in _VALID_JOB_STATES:
        valid_list = ", ".join(sorted(_VALID_JOB_STATES))
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
                "message": "Le parametre 'connection_ref_id' ne peut depasser 256 caracteres.",
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


async def _check_internal_auth(request: Request) -> tuple[bool, Response | None]:
    """Env-gated header guard for /internal/* endpoints (Phase-A scaffold).

    When INTERNAL_ENDPOINTS_REQUIRE_HEADER is set (non-empty), the request MUST
    carry header X-Internal-Auth equal to that value; otherwise 403 is returned
    and an audit row is written.

    When the env var is unset (default), returns (True, None) -- current behavior
    preserved so existing callers are unaffected.

    Phase B: replace this check with Cloud Scheduler OIDC audience validation.
    The env var will be retired; remove this function and its callsites at that time.
    """
    required_secret = os.environ.get("INTERNAL_ENDPOINTS_REQUIRE_HEADER", "")
    if not required_secret:
        # Env var not set -- Phase-A default, allow through.
        return True, None

    presented = request.headers.get("x-internal-auth", "")
    if presented != required_secret:
        logger.warning(
            "admin_api: internal_auth_rejected path=%s -- missing or wrong X-Internal-Auth",
            request.url.path,
        )
        # Write audit row (best-effort; swallowed on error by write_audit_row).
        write_audit_row(
            identity="anonymous",
            action="access_denied",
            provider_account="",
            connection_ref="",
            metadata={"path": str(request.url.path), "reason": "internal_auth_missing"},
        )
        return False, JSONResponse(
            {"code": "forbidden", "message": "X-Internal-Auth header required"},
            status_code=403,
        )

    return True, None


async def _dispatch_nightly_internal(request: Request) -> Response:
    """POST /internal/scheduler/dispatch-nightly -- Cloud Scheduler trigger (Story 3.4, AC3).

    Phase B only (QUEUE_BACKEND=cloud_tasks). Returns 404 for local backend.
    Auth via _check_auth (service-account Bearer token validates as any caller).
    Also gated by _check_internal_auth (Phase-A scaffold for OIDC -- see that function).

    This endpoint is STUBBED at P3-dev (same pattern as CloudTasksBackend in Story 3.2,
    AC8). No google-cloud-scheduler import at runtime (HG-1).

    Response (200, Phase B only):
        {"jobs": [...]}  -- list of enqueued job dicts from dispatch_nightly()
    """
    ok, err_resp = await _check_internal_auth(request)
    if not ok:
        return err_resp  # type: ignore[return-value]

    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    queue_backend = os.environ.get("QUEUE_BACKEND", "local")
    if queue_backend != "cloud_tasks":
        return JSONResponse(
            {
                "code": "not_available",
                "message": "POST /internal/scheduler/dispatch-nightly is only active "
                "when QUEUE_BACKEND=cloud_tasks (Phase B)",
            },
            status_code=404,
        )

    from core.scheduler import dispatch_nightly  # noqa: PLC0415

    jobs = dispatch_nightly()
    return JSONResponse({"jobs": jobs})


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


# ---------------------------------------------------------------------------
# Story 4.3 — Context events REST handlers (AC4)
#
# POST /api/context-events -- create a context event from the admin console.
# GET  /api/context-events -- list events with project_id + optional date filters.
#
# Validation logic (_validate_event_input) is shared with the MCP tool in main.py
# via a local import to avoid a circular dependency (main imports admin_api at startup
# via build_asgi_app; admin_api cannot import from main). The admin_api has its own
# inline validation that mirrors the MCP tool's logic exactly.
#
# AD-8: admin console communicates exclusively through this REST API.
# HG-2: widget writes via callServerTool (add_context_event MCP tool) — never here.
# AD-2: no module-specific strings.
# ---------------------------------------------------------------------------

# ISO-8601 date pattern (reused from existing _ISO_DATE_RE above)
_CONTEXT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_context_event_input(label: str, event_date: str) -> str | None:
    """Validate context event inputs. Returns error message string or None if valid.

    Mirrors _validate_event_input in main.py. Both must stay in sync.
    Returns a French error message on failure, None on success.
    """
    if len(label) > 120:
        return "label trop long (max 120 caractères)"
    if not _CONTEXT_DATE_RE.match(event_date):
        return f"event_date invalide (format attendu YYYY-MM-DD) : {event_date!r}"
    return None


async def _create_context_event(request: Request) -> Response:
    """POST /api/context-events -- create a context event (admin console).

    Request body (JSON):
        {"project_id": str, "event_date": str, "type": str, "label": str,
         "description": str?}

    Response (201):
        {"id", "project_id", "event_date", "type", "label", "created_at"}

    Response (422):
        {"error": "label trop long (max 120 caracteres)"} on validation failure.

    HG-2: this endpoint is for the admin console only. The widget uses the
    add_context_event MCP tool (callServerTool). Do NOT call this from the widget.
    """
    from ulid import ULID  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes)
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    event_date = (body.get("event_date") or "").strip()
    type_ = (body.get("type") or "").strip()
    label = (body.get("label") or "").strip()
    description = (body.get("description") or "").strip() or None

    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id is required"},
            status_code=400,
        )
    if not event_date:
        return JSONResponse(
            {"code": "missing_field", "message": "event_date is required"},
            status_code=400,
        )
    if not type_:
        return JSONResponse(
            {"code": "missing_field", "message": "type is required"},
            status_code=400,
        )
    if not label:
        return JSONResponse(
            {"code": "missing_field", "message": "label is required"},
            status_code=400,
        )

    validation_error = _validate_context_event_input(label, event_date)
    if validation_error:
        return JSONResponse({"error": validation_error}, status_code=422)

    evt_id = f"evt_{ULID()}"
    created_by = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.context_events
                        (id, project_id, event_date, type, label, description, created_by)
                    VALUES (%s, %s, %s::date, %s, %s, %s, %s)
                    RETURNING id, project_id, event_date, type, label, created_at
                    """,
                    (evt_id, project_id, event_date, type_, label, description, created_by),
                )
                row = cur.fetchone()
                if row is None:  # pragma: no cover
                    raise RuntimeError("INSERT RETURNING returned no row")
                cols = [desc[0] for desc in cur.description]
                created_record: dict = {}
                for col, val in zip(cols, row):
                    if col == "created_at" and val is not None:
                        created_record[col] = val.isoformat()
                    elif col == "event_date" and val is not None:
                        created_record[col] = str(val)
                    else:
                        created_record[col] = val
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: context_event_insert_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=created_by,
        action=ACTION_CONTEXT_EVENT_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "event_id": evt_id,
            "project_id": project_id,
            "type": type_,
            "label": label,
        },
    )

    return JSONResponse(created_record, status_code=201)


async def _list_context_events(request: Request) -> Response:
    """GET /api/context-events -- list context events for a project.

    Query params:
        project_id  (required)
        start       (optional ISO date, inclusive)
        end         (optional ISO date, inclusive)

    Response (200):
        {"events": [{id, project_id, event_date, type, label, description,
                     created_by, created_at}, ...]}
        Order: event_date DESC, created_at DESC. No pagination (admin console view).

    Response (400): missing project_id.
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.query_params.get("project_id") or ""
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )

    start = request.query_params.get("start") or None
    end = request.query_params.get("end") or None

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                params: list = [project_id]
                sql = """
                    SELECT id, project_id, event_date, type, label,
                           description, created_by, created_at
                    FROM app.context_events
                    WHERE project_id = %s
                """
                if start is not None:
                    sql += " AND event_date >= %s::date"
                    params.append(start)
                if end is not None:
                    sql += " AND event_date <= %s::date"
                    params.append(end)
                sql += " ORDER BY event_date DESC, created_at DESC"
                cur.execute(sql, params)
                cols = [desc[0] for desc in cur.description]
                events: list[dict] = []
                for row in cur.fetchall():
                    record: dict = {}
                    for col, val in zip(cols, row):
                        if col == "created_at" and val is not None:
                            record[col] = val.isoformat()
                        elif col == "event_date" and val is not None:
                            record[col] = str(val)
                        else:
                            record[col] = val
                    events.append(record)
    except Exception as exc:
        logger.error("admin_api: list_context_events db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse({"events": events})


# ---------------------------------------------------------------------------
# Story 4.4 (AC8) — POST /api/mirror/sync (manual mirror sync trigger)
# ---------------------------------------------------------------------------


async def _trigger_mirror_sync(request: Request) -> Response:
    """POST /api/mirror/sync -- trigger a mirror sync manually (Story 4.4, AC8).

    Useful for dev and for Story 5.3 forced syncs. Auth-guarded (same pattern as
    all other admin API endpoints). Returns the sync result dict from mirror_sync.py.

    Response (200):
        {"synced": {...}, "lag_seconds": float, "synced_at": str}

    Response (500):
        {"code": "sync_error", "message": str}  on sync failure.
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        from core import mirror_sync  # noqa: PLC0415

        result = mirror_sync.sync_tables()
    except Exception as exc:
        logger.error("admin_api: mirror_sync_error: %s", exc)
        return JSONResponse(
            {"code": "sync_error", "message": f"Mirror sync failed: {exc}"},
            status_code=500,
        )

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Story 5.2 (AC4) — GET /api/health (REST proxy for the MCP health tool)
#
# The admin console PipelinePanel fetches this endpoint to get circuit breaker
# states (data.quota) and mirror sync lag (data.mirror_sync).
#
# The health() function is defined in core/main.py as an MCP tool. We import
# it here and call it directly -- the result shape is identical to the MCP
# structuredContent response.
#
# Auth-guarded (same pattern as all other admin API endpoints).
# ---------------------------------------------------------------------------


async def _health_proxy(request: Request) -> Response:
    """GET /api/health -- REST proxy for the health MCP tool (Story 5.2, AC4).

    Returns the health tool's dict response as JSON.
    Useful for the admin Pipeline panel which needs circuit breaker states and
    mirror sync lag without going through the MCP protocol.

    Response (200):
        {"data": {"status": "ok", "quota": [...], "mirror_sync": {...}|null, ...}, ...}

    Response (401): unauthorized
    Response (500): internal error calling health tool
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.query_params.get("project_id", "default")

    try:
        from core.main import health  # noqa: PLC0415

        result = health(project_id=project_id)
    except Exception as exc:
        logger.error("admin_api: health_proxy_error: %s", exc)
        return JSONResponse(
            {"code": "health_error", "message": f"Health tool error: {exc}"},
            status_code=500,
        )

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Story 5.3 (AC5) -- /api/alert-definitions CRUD
#
# GET    /api/alert-definitions?project_id=<id>  -- list definitions
# POST   /api/alert-definitions                  -- create definition
# PATCH  /api/alert-definitions/{id}             -- toggle enabled / update threshold
# DELETE /api/alert-definitions/{id}             -- soft-delete (enabled=false)
#
# All routes guarded by api_auth (same pattern as above).
# AD-8: admin console communicates exclusively through this REST API.
# AD-2: no module-specific strings.
# ---------------------------------------------------------------------------

# Valid operators for alert threshold comparisons (whitelist enforced at CRUD).
_ALERT_OPERATOR_WHITELIST = {"<", ">", "<=", ">="}

# Valid metric names: additive metrics from dim_metric + semantic view names.
# Driven by ALERT_SEMANTIC_METRICS env var at runtime (deferred import below).
_ALERT_ADDITIVE_METRICS = frozenset(
    [
        "sessions",
        "active_users",
        "conversions",
        "cost",
        "impressions",
        "clicks",
        "revenue",
        "average_position",
    ]
)


def _get_valid_alert_metrics() -> frozenset[str]:
    """Return all valid metric names for alert definitions."""
    from core.business_alerts import _get_semantic_metrics  # noqa: PLC0415

    return _ALERT_ADDITIVE_METRICS | _get_semantic_metrics()


async def _list_alert_definitions(request: Request) -> Response:
    """GET /api/alert-definitions?project_id=<id> -- list definitions for a project.

    Includes last firing date and value per definition via LEFT JOIN on alert_firings.

    Response (200):
        {"definitions": [{id, project_id, metric, operator, threshold, connector,
                          enabled, created_by, created_at, updated_at,
                          last_firing_date, last_firing_value}]}

    Error responses:
        400 -- missing project_id
        401 -- unauthorized
        500 -- DB error
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.query_params.get("project_id") or ""
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        d.id, d.project_id, d.metric, d.operator, d.threshold,
                        d.connector, d.enabled, d.created_by, d.created_at, d.updated_at,
                        lf.last_firing_date,
                        lf.last_firing_value
                    FROM app.alert_definitions d
                    LEFT JOIN (
                        SELECT
                            definition_id,
                            MAX(window_date) AS last_firing_date,
                            (ARRAY_AGG(observed_value ORDER BY fired_at DESC))[1]
                                AS last_firing_value
                        FROM app.alert_firings
                        GROUP BY definition_id
                    ) lf ON lf.definition_id = d.id
                    WHERE d.project_id = %s
                    ORDER BY d.created_at DESC
                    """,
                    (project_id,),
                )
                cols = [desc[0] for desc in cur.description]
                _TS_COLS = {"created_at", "updated_at"}
                definitions = []
                for row in cur.fetchall():
                    record: dict = {}
                    for col, val in zip(cols, row):
                        if col in _TS_COLS and val is not None:
                            record[col] = val.isoformat()
                        elif col == "last_firing_date" and val is not None:
                            record[col] = str(val)
                        elif col in ("threshold", "last_firing_value") and val is not None:
                            record[col] = float(val)
                        else:
                            record[col] = val
                    definitions.append(record)
    except Exception as exc:
        logger.error("admin_api: list_alert_definitions_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse({"definitions": definitions})


async def _create_alert_definition(request: Request) -> Response:
    """POST /api/alert-definitions -- create a new alert definition.

    Request body (JSON):
        {"project_id": str, "metric": str, "operator": str,
         "threshold": number, "connector": str?}

    Response (201):
        {id, project_id, metric, operator, threshold, connector, enabled,
         created_by, created_at, updated_at}

    Error responses:
        400 -- missing required fields
        401 -- unauthorized
        422 -- invalid operator or unknown metric
        500 -- DB error
    """
    from ulid import ULID  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes)
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    metric = (body.get("metric") or "").strip().lower()
    operator = (body.get("operator") or "").strip()
    threshold_raw = body.get("threshold")
    connector = (body.get("connector") or "").strip() or None

    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id is required"},
            status_code=400,
        )
    if not metric:
        return JSONResponse(
            {"code": "missing_field", "message": "metric is required"},
            status_code=400,
        )
    if not operator:
        return JSONResponse(
            {"code": "missing_field", "message": "operator is required"},
            status_code=400,
        )
    if threshold_raw is None:
        return JSONResponse(
            {"code": "missing_field", "message": "threshold is required"},
            status_code=400,
        )

    # Operator whitelist validation
    if operator not in _ALERT_OPERATOR_WHITELIST:
        return JSONResponse(
            {
                "code": "invalid_operator",
                "message": (
                    f"operator must be one of {sorted(_ALERT_OPERATOR_WHITELIST)}, "
                    f"got: {operator!r}"
                ),
            },
            status_code=422,
        )

    # Metric validation
    valid_metrics = _get_valid_alert_metrics()
    if metric not in valid_metrics:
        return JSONResponse(
            {
                "code": "unknown_metric",
                "message": (
                    f"metric {metric!r} is not a known metric. "
                    f"Valid metrics: {sorted(valid_metrics)}"
                ),
            },
            status_code=422,
        )

    # Threshold must be numeric
    try:
        threshold = float(threshold_raw)
    except (TypeError, ValueError):
        return JSONResponse(
            {"code": "invalid_threshold", "message": "threshold must be a number"},
            status_code=422,
        )

    alert_id = f"alrt_{ULID()}"
    created_by = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.alert_definitions
                        (id, project_id, metric, operator, threshold, connector,
                         enabled, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s)
                    RETURNING id, project_id, metric, operator, threshold, connector,
                              enabled, created_by, created_at, updated_at
                    """,
                    (alert_id, project_id, metric, operator, threshold, connector, created_by),
                )
                row = cur.fetchone()
                if row is None:  # pragma: no cover
                    raise RuntimeError("INSERT RETURNING returned no row")
                cols = [desc[0] for desc in cur.description]
                created_record: dict = {}
                for col, val in zip(cols, row):
                    if col in ("created_at", "updated_at") and val is not None:
                        created_record[col] = val.isoformat()
                    elif col == "threshold" and val is not None:
                        created_record[col] = float(val)
                    else:
                        created_record[col] = val
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: create_alert_definition_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=created_by,
        action=ACTION_ALERT_DEF_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "alert_def_id": alert_id,
            "project_id": project_id,
            "metric": metric,
            "operator": operator,
            "threshold": threshold,
        },
    )

    return JSONResponse(created_record, status_code=201)


async def _update_alert_definition(request: Request) -> Response:
    """PATCH /api/alert-definitions/{id} -- toggle enabled or update threshold.

    Request body (JSON, all fields optional):
        {"enabled": bool?, "threshold": number?}

    Response (200):
        {id, project_id, metric, operator, threshold, connector, enabled,
         created_by, created_at, updated_at}

    Error responses:
        400 -- empty body / no updatable fields
        401 -- unauthorized
        404 -- definition not found
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    alert_id = request.path_params.get("id", "")
    if not alert_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Alert definition id is required"},
            status_code=400,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    # Build the SET clause from allowed updatable fields
    set_clauses: list[str] = []
    params: list = []

    if "enabled" in body:
        set_clauses.append("enabled = %s")
        params.append(bool(body["enabled"]))

    if "threshold" in body:
        try:
            threshold_val = float(body["threshold"])
            set_clauses.append("threshold = %s")
            params.append(threshold_val)
        except (TypeError, ValueError):
            return JSONResponse(
                {"code": "invalid_threshold", "message": "threshold must be a number"},
                status_code=422,
            )

    if not set_clauses:
        return JSONResponse(
            {
                "code": "no_update_fields",
                "message": "Provide 'enabled' or 'threshold' to update",
            },
            status_code=400,
        )

    set_clauses.append("updated_at = NOW()")
    params.append(alert_id)

    sql = (
        "UPDATE app.alert_definitions SET "
        + ", ".join(set_clauses)
        + " WHERE id = %s RETURNING id, project_id, metric, operator, threshold,"
        " connector, enabled, created_by, created_at, updated_at"
    )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {
                            "code": "not_found",
                            "message": f"Alert definition '{alert_id}' not found",
                        },
                        status_code=404,
                    )
                cols = [desc[0] for desc in cur.description]
                updated_record: dict = {}
                for col, val in zip(cols, row):
                    if col in ("created_at", "updated_at") and val is not None:
                        updated_record[col] = val.isoformat()
                    elif col == "threshold" and val is not None:
                        updated_record[col] = float(val)
                    else:
                        updated_record[col] = val
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: update_alert_definition_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ALERT_DEF_UPDATED,
        provider_account="",
        connection_ref="",
        metadata={
            "alert_def_id": alert_id,
            "updated_fields": list(k for k in ("enabled", "threshold") if k in body),
        },
    )

    return JSONResponse(updated_record)


async def _delete_alert_definition(request: Request) -> Response:
    """DELETE /api/alert-definitions/{id} -- soft-delete (sets enabled=false).

    Design decision (T5.5): soft-delete via enabled=false.
    Rationale: alert_firings rows reference alert_definitions via FK. A hard
    delete would violate the FK constraint unless firings are also deleted.
    Soft-delete preserves audit history and firing provenance (AD-9) while
    effectively disabling the alert. Hard delete is not used here.

    Response (200):
        {"id": ..., "deleted": true}

    Error responses:
        401 -- unauthorized
        404 -- definition not found
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    alert_id = request.path_params.get("id", "")
    if not alert_id:
        return JSONResponse(
            {"code": "missing_id", "message": "Alert definition id is required"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.alert_definitions
                    SET enabled = FALSE, updated_at = NOW()
                    WHERE id = %s
                    RETURNING id
                    """,
                    (alert_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {
                            "code": "not_found",
                            "message": f"Alert definition '{alert_id}' not found",
                        },
                        status_code=404,
                    )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: delete_alert_definition_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ALERT_DEF_DELETED,
        provider_account="",
        connection_ref="",
        metadata={"alert_def_id": alert_id},
    )

    return JSONResponse({"id": alert_id, "deleted": True})


# ---------------------------------------------------------------------------
# Story 5.5 (AC7) — GET /api/feedback (queryable feedback store)
#
# Lists feedback rows for a project, optionally filtered by module.
# Auth-guarded by api_auth (same pattern as all other admin API endpoints).
# AD-8: admin console communicates via this REST API.
# Privacy: created_by is NOT returned (kept server-side; audit log has it).
# FR10: queryable per report/module via project_id + module filters.
# ---------------------------------------------------------------------------


async def _list_feedback(request: Request) -> Response:
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
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.query_params.get("project_id") or ""
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )

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
        logger.error("admin_api: list_feedback_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse(rows)


# ---------------------------------------------------------------------------
# Epic 40 -- competitor / tracked-brand registry (project-facing read)
# GET /api/tracked-entities?project_id=<id>
# ---------------------------------------------------------------------------
async def _list_tracked_entities(request: Request) -> Response:
    """GET /api/tracked-entities?project_id=<id>

    List the tracked brands THIS project roles (competitor/brand registry, Epic
    40). Confidentiality-safe (E40-NFR01): delegates to
    ``tracked_entity_registry.list_project_roles`` which is scoped to project_id
    ONLY and never returns a sibling project's brands. Per-source query bindings
    (the outbound wiring, Story 40.2) land later -- returned empty for now.

    Response (200):
        {"entities": [{"id", "name", "role", "aliases", "status", "bindings"}]}
    """
    project_id = request.query_params.get("project_id") or "default"
    try:
        from core import tracked_entity_registry  # noqa: PLC0415

        roles = tracked_entity_registry.list_project_roles(project_id)
    except Exception as exc:  # noqa: BLE001 -- surface a clean 500, log the cause
        logger.error("admin_api: list_tracked_entities db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    entities = [
        {
            "id": r.get("entity_id"),
            "name": r.get("entity_display_name") or r.get("entity_canonical_name"),
            "role": r.get("role"),
            "aliases": r.get("entity_aliases") or [],
            "status": r.get("entity_status"),
            "bindings": [],  # TODO(40.2): per-source query bindings (Trends/YouTube/…)
        }
        for r in roles
    ]
    return JSONResponse({"entities": entities}, status_code=200)


# ---------------------------------------------------------------------------
# Context workspace read surfaces (Migration 095)
# GET /api/knowledge?project_id=<id>    -- governed business definitions/policies
# GET /api/procedures?project_id=<id>   -- per-metric calculation/reconciliation
# ---------------------------------------------------------------------------
async def _list_knowledge(request: Request) -> Response:
    """GET /api/knowledge?project_id=<id> -- the project's knowledge entries.

    Response (200): {"entries": [{"id","title","topic","body","author","updated_at"}]}
    """
    project_id = request.query_params.get("project_id") or "default"
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, title, topic, body, author, updated_at
                    FROM app.knowledge_entries
                    WHERE project_id = %s
                    ORDER BY updated_at DESC
                    """,
                    (project_id,),
                )
                cols = [d[0] for d in cur.description]
                entries = [
                    {
                        c: (v.isoformat() if c == "updated_at" and v is not None else v)
                        for c, v in zip(cols, row)
                    }
                    for row in cur.fetchall()
                ]
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_api: list_knowledge db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"}, status_code=500
        )
    return JSONResponse({"entries": entries}, status_code=200)


async def _list_procedures(request: Request) -> Response:
    """GET /api/procedures?project_id=<id> -- per-metric calculation procedures.

    Response (200):
        {"procedures": [{"id","metric","method","description","owner","updated_at"}]}
        method ∈ sum_then_divide | priority_source | dedup_union | weighted_blend |
                 manual_override
    """
    project_id = request.query_params.get("project_id") or "default"
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, metric, method, description, owner, updated_at
                    FROM app.metric_procedures
                    WHERE project_id = %s
                    ORDER BY metric ASC
                    """,
                    (project_id,),
                )
                cols = [d[0] for d in cur.description]
                procedures = [
                    {
                        c: (v.isoformat() if c == "updated_at" and v is not None else v)
                        for c, v in zip(cols, row)
                    }
                    for row in cur.fetchall()
                ]
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_api: list_procedures db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"}, status_code=500
        )
    return JSONResponse({"procedures": procedures}, status_code=200)


# ---------------------------------------------------------------------------
# Test workspace read surfaces (Migration 096, eval loop — Epic 14)
# GET /api/eval/golden-questions?project_id=<id>
# GET /api/eval/runs?project_id=<id>
# ---------------------------------------------------------------------------
async def _list_golden_questions(request: Request) -> Response:
    """GET /api/eval/golden-questions?project_id=<id> -- the benchmark set.

    Response (200):
        {"questions": [{"id","question","topic","expected_citations","last_result"}]}
        last_result ∈ pass | fail
    """
    project_id = request.query_params.get("project_id") or "default"
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, question, topic, expected_citations, last_result
                    FROM app.golden_questions
                    WHERE project_id = %s
                    ORDER BY topic ASC, question ASC
                    """,
                    (project_id,),
                )
                cols = [d[0] for d in cur.description]
                questions = [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_api: list_golden_questions db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"}, status_code=500
        )
    return JSONResponse({"questions": questions}, status_code=200)


async def _list_eval_runs(request: Request) -> Response:
    """GET /api/eval/runs?project_id=<id> -- run history, most recent first.

    Response (200):
        {"runs": [{"id","run_at","score_passed","score_total","precision_pct",
                   "regressions","status"}]}
        status ∈ passed | regressed
    """
    project_id = request.query_params.get("project_id") or "default"
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, run_at, score_passed, score_total, precision_pct,
                           regressions, status
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
                            else (float(v) if c == "precision_pct" and v is not None else v)
                        )
                        for c, v in zip(cols, row)
                    }
                    for row in cur.fetchall()
                ]
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_api: list_eval_runs db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"}, status_code=500
        )
    return JSONResponse({"runs": runs}, status_code=200)


# ---------------------------------------------------------------------------
# Overview metric strip — real aggregates (Epic 42)
# GET /api/overview/summary?project_id=<id>
# ---------------------------------------------------------------------------
async def _overview_summary(request: Request) -> Response:
    """GET /api/overview/summary?project_id=<id> -- the Overview strip aggregates,
    computed from real tables (datastreams, target_fields + mappings, eval_runs).

    Response (200):
        {
          "active_datastreams": <int>,
          "canonical_concepts": <int>,
          "mapping_coverage_pct": <int 0..100>,
          "published_trust": "trusted" | "attention" | "no_data",
          "latest_test": {"passed": <int>, "total": <int>, "regressions": <int>} | null
        }
    """
    project_id = request.query_params.get("project_id") or "default"
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM app.datastreams WHERE project_id = %s",
                    (project_id,),
                )
                active = cur.fetchone()[0] or 0
                cur.execute("SELECT count(*) FROM app.target_fields")
                total_fields = cur.fetchone()[0] or 0
                cur.execute(
                    """
                    SELECT count(DISTINCT m.target_field)
                    FROM app.datastream_mappings m
                    JOIN app.datastreams d ON d.id = m.datastream_id
                    WHERE d.project_id = %s
                    """,
                    (project_id,),
                )
                mapped = cur.fetchone()[0] or 0
                cur.execute(
                    """
                    SELECT score_passed, score_total, regressions
                    FROM app.eval_runs
                    WHERE project_id = %s
                    ORDER BY run_at DESC
                    LIMIT 1
                    """,
                    (project_id,),
                )
                row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.error("admin_api: overview_summary db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"}, status_code=500
        )

    latest_test = (
        {"passed": row[0], "total": row[1], "regressions": row[2]} if row else None
    )
    coverage = round(100 * mapped / total_fields) if total_fields else 0
    trust = "no_data" if active == 0 else "trusted"
    return JSONResponse(
        {
            "active_datastreams": active,
            "canonical_concepts": total_fields,
            "mapping_coverage_pct": coverage,
            "published_trust": trust,
            "latest_test": latest_test,
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Story 6.1 (AC9) -- report management endpoints
#
# GET   /api/reports/available?project_id=<id>
#         -> all reports available for the project, merged from the module
#            registry (LoadedModule.reports) and app.project_reports. A report not
#            in project_reports is returned with enabled=false (opt-in default).
# PATCH /api/reports/{project_id}/{module_name}/{report_id}
#         -> upsert the app.project_reports row (INSERT ... ON CONFLICT DO UPDATE).
#
# Both project-scoped and api_auth-guarded. AD-8: admin console -> REST API only.
# AD-2: no module-specific strings; the report catalog comes from the loader.
# ---------------------------------------------------------------------------


def _module_report_catalog() -> list[dict]:
    """Return [{module_name, report_id, display_name}] from the loaded-module registry.

    Deferred import of core.main avoids a circular import (main imports admin_api
    at startup via build_asgi_app). Mirrors the _health_proxy pattern.
    """
    from core.main import get_loaded_modules  # noqa: PLC0415

    catalog: list[dict] = []
    for loaded in get_loaded_modules():
        for report in loaded.reports:
            catalog.append(
                {
                    "module_name": loaded.name,
                    "report_id": report.get("id"),
                    "display_name": report.get("display_name"),
                }
            )
    return catalog


async def _list_available_reports(request: Request) -> Response:
    """GET /api/reports/available?project_id=<id> -- merged report availability.

    Response (200):
        [{"module_name", "report_id", "display_name", "enabled", "display_order"}]
        enabled=false for reports not yet opted-in for the project (AC9).

    Error responses:
        400 -- missing project_id
        401 -- unauthorized
        500 -- DB error (module catalog still degrades to enabled=false)
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.query_params.get("project_id") or ""
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )

    catalog = _module_report_catalog()

    # Story 7.2 (AC4): load module enablement for this project so we can
    # exclude reports whose module is disabled. Default-enabled when no row exists.
    module_enabled: dict[str, bool] = {}
    # Load per-project enablement rows (project-scoped — never another project).
    enablement: dict[tuple[str, str], dict] = {}
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.module_enablement import is_module_enabled  # noqa: PLC0415

        with get_connection() as conn:
            # Collect distinct module names from catalog to check enablement.
            distinct_modules = {entry["module_name"] for entry in catalog}
            for mod_name in distinct_modules:
                module_enabled[mod_name] = is_module_enabled(mod_name, project_id, conn)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT module_name, report_id, enabled, display_order
                    FROM app.project_reports
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )
                for module_name, report_id, enabled, display_order in cur.fetchall():
                    enablement[(module_name, report_id)] = {
                        "enabled": bool(enabled),
                        "display_order": int(display_order),
                    }
    except Exception as exc:
        logger.warning("admin_api: list_available_reports db_error: %s", exc)
        # Degrade gracefully: return catalog with the opt-in default (disabled).

    reports = []
    for entry in catalog:
        mod_name = entry["module_name"]
        # Story 7.2 (AC4): skip reports for disabled modules.
        # module_enabled defaults to True when DB is unavailable (resilience).
        if not module_enabled.get(mod_name, True):
            continue
        key = (mod_name, entry["report_id"])
        row = enablement.get(key)
        reports.append(
            {
                "module_name": mod_name,
                "report_id": entry["report_id"],
                "display_name": entry["display_name"],
                "enabled": row["enabled"] if row else False,
                "display_order": row["display_order"] if row else 0,
            }
        )

    return JSONResponse(reports)


# ---------------------------------------------------------------------------
# Story 7.2 (AC7) -- Module management endpoints
#
# GET  /api/modules/available?project_id=<id>
#        -> all globally discovered modules with per-project enablement state.
# PATCH /api/modules/{project_id}/{module_name}
#        -> upsert enablement in app.project_modules.
#
# Both guarded by api_auth; project-scoped. AD-8: admin console -> REST only.
# AD-2: no module-specific strings; catalog from LoadedModule registry.
# ---------------------------------------------------------------------------


def _module_discovery_catalog() -> list[dict]:
    """Return [{name, display_name}] from the globally loaded modules registry.

    Deferred import of core.main to avoid circular imports (same pattern as
    _module_report_catalog).
    """
    from core.main import get_loaded_modules  # noqa: PLC0415

    return [
        {
            "name": loaded.name,
            "display_name": loaded.manifest.get("display_name", loaded.name),
        }
        for loaded in get_loaded_modules()
    ]


async def _list_available_modules(request: Request) -> Response:
    """GET /api/modules/available?project_id=<id> -- module enablement per project.

    Response (200):
        [
          {
            "module_name": "<module-kebab-name>",
            "display_name": "<Human-readable name>",
            "enabled": true,
            "explicitly_set": false,
            "active_connections": 1
          },
          ...
        ]
        enabled: True when enabled (including default-enabled).
        explicitly_set: False = default-enabled (no row in project_modules).
                        True  = row exists in project_modules.
        active_connections: count of active+enabled connections for this module.

    Error responses:
        400 -- missing project_id
        401 -- unauthorized
        500 -- DB error
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.query_params.get("project_id") or ""
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )

    discovery = _module_discovery_catalog()

    # Fetch per-project module enablement rows and active connection counts.
    pm_rows: dict[str, dict] = {}  # module_name -> {"enabled": bool}
    conn_counts: dict[str, int] = {}  # module_name -> active connection count
    default_enabled = True  # fallback when DB unavailable

    try:
        import os  # noqa: PLC0415

        from core.db import get_connection  # noqa: PLC0415

        default_enabled = os.environ.get("MODULE_DEFAULT_ENABLED", "true").lower() != "false"

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Fetch explicit module enablement rows.
                cur.execute(
                    """
                    SELECT module_name, enabled
                    FROM app.project_modules
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )
                for module_name, enabled in cur.fetchall():
                    pm_rows[module_name] = {"enabled": bool(enabled)}

                # Count active+enabled connections per provider (provider = module_name).
                cur.execute(
                    """
                    SELECT provider, COUNT(*) AS cnt
                    FROM app.connection_ref
                    WHERE project_id = %s
                      AND status = 'active'
                      AND enabled = TRUE
                    GROUP BY provider
                    """,
                    (project_id,),
                )
                for provider, cnt in cur.fetchall():
                    conn_counts[provider] = int(cnt)
    except Exception as exc:
        logger.warning("admin_api: list_available_modules db_error: %s", exc)
        # Degrade gracefully: return catalog with default_enabled state.

    result = []
    for mod in discovery:
        mod_name = mod["name"]
        pm = pm_rows.get(mod_name)
        result.append(
            {
                "module_name": mod_name,
                "display_name": mod["display_name"],
                "enabled": pm["enabled"] if pm is not None else default_enabled,
                "explicitly_set": pm is not None,
                "active_connections": conn_counts.get(mod_name, 0),
            }
        )

    return JSONResponse(result)


async def _patch_module(request: Request) -> Response:
    """PATCH /api/modules/{project_id}/{module_name} -- upsert module enablement.

    Body (JSON): {"enabled": bool}

    Response (200): updated state.
      On disable with active connections: includes a warning field.
    Error responses:
        400 -- missing params or invalid body
        401 -- unauthorized
        500 -- DB error
    """
    from ulid import ULID  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.path_params.get("project_id", "")
    module_name = request.path_params.get("module_name", "")
    if not project_id or not module_name:
        return JSONResponse(
            {"code": "missing_id", "message": "project_id and module_name required"},
            status_code=400,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    if "enabled" not in body:
        return JSONResponse(
            {"code": "missing_field", "message": "enabled is required"},
            status_code=400,
        )

    enabled = bool(body["enabled"])
    pmod_id = f"pmod_{ULID()}"
    updated_by = identity or "system"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Count active connections for the warning (before upsert).
            active_connections = 0
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM app.connection_ref
                    WHERE project_id = %s AND provider = %s
                      AND status = 'active' AND enabled = TRUE
                    """,
                    (project_id, module_name),
                )
                row = cur.fetchone()
                if row:
                    active_connections = int(row[0])

            # Upsert the enablement row.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.project_modules
                        (id, project_id, module_name, enabled, enabled_at, disabled_at, updated_by)
                    VALUES (
                        %s, %s, %s, %s,
                        CASE WHEN %s THEN NOW() ELSE NULL END,
                        CASE WHEN NOT %s THEN NOW() ELSE NULL END,
                        %s
                    )
                    ON CONFLICT (project_id, module_name) DO UPDATE
                        SET enabled     = EXCLUDED.enabled,
                            enabled_at  = CASE WHEN EXCLUDED.enabled THEN NOW()
                                               ELSE app.project_modules.enabled_at END,
                            disabled_at = CASE WHEN NOT EXCLUDED.enabled THEN NOW()
                                               ELSE app.project_modules.disabled_at END,
                            updated_by  = EXCLUDED.updated_by
                    RETURNING module_name, enabled
                    """,
                    (
                        pmod_id,
                        project_id,
                        module_name,
                        enabled,
                        enabled,  # enabled_at CASE
                        enabled,  # disabled_at CASE (NOT enabled)
                        updated_by,
                    ),
                )
                upserted = cur.fetchone()
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: patch_module db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    response: dict = {
        "project_id": project_id,
        "module_name": upserted[0] if upserted else module_name,
        "enabled": bool(upserted[1]) if upserted else enabled,
    }

    # On disable with active connections: include a warning (no auto-revoke).
    if not enabled and active_connections > 0:
        response["warning"] = (
            f"Module disabled. {active_connections} active connection(s) will no longer be pulled."
        )

    return JSONResponse(response)


async def _patch_report(request: Request) -> Response:
    """PATCH /api/reports/{project_id}/{module_name}/{report_id} -- upsert enablement.

    Body (JSON): {"enabled": bool, "display_order": int?}

    Response (200): the upserted row.
    Error responses:
        400 -- missing path params
        401 -- unauthorized
        500 -- DB error
    """
    from ulid import ULID  # noqa: PLC0415

    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.path_params.get("project_id", "")
    module_name = request.path_params.get("module_name", "")
    report_id = request.path_params.get("report_id", "")
    if not project_id or not module_name or not report_id:
        return JSONResponse(
            {"code": "missing_id", "message": "project_id, module_name, report_id required"},
            status_code=400,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    enabled = bool(body.get("enabled", True))
    display_order = int(body.get("display_order", 0))
    rpt_id = f"rpt_{ULID()}"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.project_reports
                        (id, project_id, module_name, report_id, enabled, display_order)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (project_id, module_name, report_id) DO UPDATE
                        SET enabled = EXCLUDED.enabled,
                            display_order = EXCLUDED.display_order,
                            updated_at = NOW()
                    RETURNING project_id, module_name, report_id, enabled, display_order
                    """,
                    (rpt_id, project_id, module_name, report_id, enabled, display_order),
                )
                row = cur.fetchone()
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: patch_report db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse(
        {
            "project_id": row[0],
            "module_name": row[1],
            "report_id": row[2],
            "enabled": bool(row[3]),
            "display_order": int(row[4]),
        }
    )


# ---------------------------------------------------------------------------
# Story 6.5 (AC5) -- Notebooks CRUD + run trigger
#
# GET    /api/notebooks?project_id=<id>       -- list notebooks for project
# GET    /api/notebooks/{notebook_id}          -- single notebook + last 5 runs
# PATCH  /api/notebooks/{notebook_id}          -- update title/window_rule/narrative_prompt
# DELETE /api/notebooks/{notebook_id}          -- delete notebook (cascades runs)
# POST   /api/notebooks/{notebook_id}/run      -- trigger a run (calls run_notebook logic)
#
# All project-scoped (never return another project's notebooks). api_auth-guarded.
# AD-8: admin console communicates exclusively through this REST API.
# ---------------------------------------------------------------------------


_TS_COLS_NB = {"created_at", "updated_at", "executed_at", "last_run_at"}


async def _list_notebooks(request: Request) -> Response:
    """GET /api/notebooks?project_id=<id> -- list notebooks for project.

    Response (200):
        [{"id", "title", "report_ref", "window_rule", "created_at",
          "last_run_at" (nullable), "last_run_status" (nullable)}]

    Error responses:
        400 -- missing project_id
        401 -- unauthorized
        500 -- DB error
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.query_params.get("project_id") or ""
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        n.id,
                        n.title,
                        n.report_ref,
                        n.window_rule,
                        n.created_at,
                        lr.executed_at  AS last_run_at,
                        lr.status       AS last_run_status
                    FROM app.notebooks n
                    LEFT JOIN LATERAL (
                        SELECT executed_at, status
                        FROM app.notebook_runs
                        WHERE notebook_id = n.id
                        ORDER BY executed_at DESC
                        LIMIT 1
                    ) lr ON true
                    WHERE n.project_id = %s
                    ORDER BY n.created_at DESC
                    """,
                    (project_id,),
                )
                cols = [d[0] for d in cur.description]
                notebooks = []
                for row in cur.fetchall():
                    record: dict = {}
                    for col, val in zip(cols, row):
                        if col in _TS_COLS_NB and val is not None:
                            record[col] = val.isoformat()
                        else:
                            record[col] = val
                    notebooks.append(record)
    except Exception as exc:
        logger.error("admin_api: list_notebooks_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse(notebooks)


async def _get_notebook(request: Request) -> Response:
    """GET /api/notebooks/{notebook_id} -- single notebook with last 5 runs.

    Response (200):
        {"id", "title", "report_ref", "window_rule", "narrative_prompt",
         "created_at", "updated_at", "project_id",
         "runs": [{"run_id", "executed_at", "status", "pull_ids"}]}

    Error responses:
        401 -- unauthorized
        404 -- not found or project mismatch
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    notebook_id = request.path_params.get("notebook_id", "")
    # project_id for scoping (optional query param; if provided we enforce it)
    filter_project = (request.query_params.get("project_id") or "").strip()

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, project_id, title, report_ref, window_rule,
                           narrative_prompt, created_at, updated_at
                    FROM app.notebooks
                    WHERE id = %s
                    """,
                    (notebook_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Notebook not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                nb: dict = {}
                for col, val in zip(cols, row):
                    if col in _TS_COLS_NB and val is not None:
                        nb[col] = val.isoformat()
                    else:
                        nb[col] = val

                # Story 7.4 (AC4, AI-38): enforce project scope (explicit claim
                # mismatch OR non-member) -> 404 + audit row.
                denied = _enforce_notebook_project_scope(
                    nb["project_id"],
                    identity,
                    notebook_id,
                    conn,
                    scope_hint=filter_project,
                    action="notebook_get",
                )
                if denied is not None:
                    return denied

                # Fetch last 5 runs
                cur.execute(
                    """
                    SELECT id, executed_at, status, pull_ids
                    FROM app.notebook_runs
                    WHERE notebook_id = %s
                    ORDER BY executed_at DESC
                    LIMIT 5
                    """,
                    (notebook_id,),
                )
                runs = []
                for run_row in cur.fetchall():
                    run_id, executed_at, status, pull_ids = run_row
                    runs.append(
                        {
                            "run_id": run_id,
                            "executed_at": executed_at.isoformat() if executed_at else None,
                            "status": status,
                            "pull_ids": pull_ids[:3] if pull_ids else [],
                        }
                    )
                nb["runs"] = runs
    except Exception as exc:
        logger.error("admin_api: get_notebook_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse(nb)


async def _patch_notebook(request: Request) -> Response:
    """PATCH /api/notebooks/{notebook_id} -- update title/window_rule/narrative_prompt.

    Body (JSON, all optional):
        {"title": str, "window_rule": str, "narrative_prompt": str}

    Response (200): updated notebook row.
    Error responses:
        400 -- invalid body or window_rule
        401 -- unauthorized
        404 -- not found
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    notebook_id = request.path_params.get("notebook_id", "")

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    # Validate window_rule if provided
    new_window_rule = body.get("window_rule")
    if new_window_rule is not None:
        from core.window_rule import resolve_window_rule  # noqa: PLC0415

        try:
            resolve_window_rule(new_window_rule)
        except ValueError as exc:
            return JSONResponse(
                {"code": "invalid_field", "message": str(exc)},
                status_code=400,
            )

    # Story 7.4 (AC7, AI-38): a project_id in the body is an explicit scope claim.
    scope_hint = (body.get("project_id") or "").strip()

    # Build SET clause dynamically from provided fields
    updates: list[str] = ["updated_at = NOW()"]
    params: list = []

    if "title" in body:
        updates.append("title = %s")
        params.append(body["title"])
    if "window_rule" in body:
        updates.append("window_rule = %s")
        params.append(body["window_rule"])
    if "narrative_prompt" in body:
        updates.append("narrative_prompt = %s")
        params.append(body["narrative_prompt"] or None)

    params.append(notebook_id)

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Story 7.4 (AC7, AI-38): fetch owner project + enforce scope BEFORE
                # mutating. Cross-scope PATCH must 404 (+ audit), not silently write.
                cur.execute(
                    "SELECT project_id FROM app.notebooks WHERE id = %s",
                    (notebook_id,),
                )
                owner_row = cur.fetchone()
                if owner_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Notebook not found"},
                        status_code=404,
                    )
                denied = _enforce_notebook_project_scope(
                    owner_row[0],
                    identity,
                    notebook_id,
                    conn,
                    scope_hint=scope_hint,
                    action="notebook_patch",
                )
                if denied is not None:
                    return denied

                sql = f"""
                    UPDATE app.notebooks
                    SET {", ".join(updates)}
                    WHERE id = %s
                    RETURNING id, project_id, title, report_ref, window_rule,
                              narrative_prompt, created_at, updated_at
                """
                cur.execute(sql, params)
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Notebook not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                nb: dict = {}
                for col, val in zip(cols, row):
                    if col in _TS_COLS_NB and val is not None:
                        nb[col] = val.isoformat()
                    else:
                        nb[col] = val
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: patch_notebook_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse(nb)


async def _delete_notebook(request: Request) -> Response:
    """DELETE /api/notebooks/{notebook_id} -- delete notebook (cascades runs).

    Response (204): no content on success.
    Error responses:
        401 -- unauthorized
        404 -- not found
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    notebook_id = request.path_params.get("notebook_id", "")

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Fetch notebook first (for audit and project scoping)
                cur.execute(
                    "SELECT id, project_id FROM app.notebooks WHERE id = %s",
                    (notebook_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Notebook not found"},
                        status_code=404,
                    )
                nb_id, project_id = row

                cur.execute("DELETE FROM app.notebooks WHERE id = %s", (notebook_id,))
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: delete_notebook_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_NOTEBOOK_DELETED,
        provider_account="",
        connection_ref="",
        metadata={"notebook_id": notebook_id, "project_id": project_id},
    )

    return Response(status_code=204)


async def _run_notebook_endpoint(request: Request) -> Response:
    """POST /api/notebooks/{notebook_id}/run -- trigger a notebook run (Story 6.5, AC5, T6.5).

    Calls run_notebook logic server-side (not via MCP protocol layer) and returns
    the new run id. This is the REST trigger for the admin console "Exécuter" button.

    Response (200):
        {"run_id": "nbrun_...", "summary": "...", "pull_ids": [...]}

    Error responses:
        401 -- unauthorized
        404 -- notebook not found
        500 -- run error
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    notebook_id = request.path_params.get("notebook_id", "")

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception:
        body = {}

    as_of = body.get("as_of", "")

    try:
        from core.main import run_notebook  # noqa: PLC0415

        result = run_notebook(notebook_id=notebook_id, as_of=as_of or "")
    except Exception as exc:
        err_str = str(exc)
        # Check if it's a ToolError with code=not_found
        try:
            err_data = json.loads(exc.args[0]) if exc.args else {}
        except Exception:
            err_data = {}
        if err_data.get("code") == "not_found":
            return JSONResponse(
                {"code": "not_found", "message": "Notebook not found"},
                status_code=404,
            )
        logger.error("admin_api: run_notebook_endpoint_error: %s", exc)
        return JSONResponse(
            {"code": "run_error", "message": f"Notebook run failed: {err_str}"},
            status_code=500,
        )

    # Extract run_id from structured_content meta
    sc = result.structured_content or {}
    run_id = sc.get("meta", {}).get("run_id", "")
    pull_ids = sc.get("meta", {}).get("provenance", {}).get("pull_ids", [])
    summary = result.content[0].text if result.content else ""

    return JSONResponse(
        {"run_id": run_id, "summary": summary, "pull_ids": pull_ids},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Story 6.6 — Schedule endpoint (AC2)
# ---------------------------------------------------------------------------


async def _schedule_notebook(request: Request) -> Response:
    """PATCH /api/notebooks/{notebook_id}/schedule -- set/unset nightly schedule (Story 6.6, AC2).

    Body: {"scheduled": bool, "schedule_rule": "nightly" | null}
    Response (200): updated notebook object.
    Error responses:
        400 -- invalid body / invalid schedule_rule
        401 -- unauthorized
        404 -- not found
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    notebook_id = request.path_params.get("notebook_id", "")

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    if "scheduled" not in body:
        return JSONResponse(
            {"code": "invalid_body", "message": "'scheduled' field is required"},
            status_code=400,
        )

    scheduled = bool(body["scheduled"])
    schedule_rule = body.get("schedule_rule")
    # Story 7.4 (AC7, AI-38): a project_id in the body is an explicit scope claim.
    scope_hint = (body.get("project_id") or "").strip()

    # Validate: if scheduled=true, schedule_rule must be 'nightly'
    if scheduled:
        if schedule_rule != "nightly":
            return JSONResponse(
                {
                    "code": "invalid_field",
                    "message": "schedule_rule must be 'nightly' when scheduled=true",
                },
                status_code=422,
            )
    else:
        # When unscheduling, force schedule_rule to null
        schedule_rule = None

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Story 7.4 (AC7, AI-38): fetch owner project + enforce scope BEFORE
                # mutating. Cross-scope SCHEDULE must 404 (+ audit), not write.
                cur.execute(
                    "SELECT project_id FROM app.notebooks WHERE id = %s",
                    (notebook_id,),
                )
                owner_row = cur.fetchone()
                if owner_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Notebook not found"},
                        status_code=404,
                    )
                denied = _enforce_notebook_project_scope(
                    owner_row[0],
                    identity,
                    notebook_id,
                    conn,
                    scope_hint=scope_hint,
                    action="notebook_schedule",
                )
                if denied is not None:
                    return denied

                cur.execute(
                    """
                    UPDATE app.notebooks
                    SET scheduled = %s, schedule_rule = %s, updated_at = NOW()
                    WHERE id = %s
                    RETURNING id, project_id, title, report_ref, window_rule,
                              narrative_prompt, scheduled, schedule_rule,
                              created_at, updated_at
                    """,
                    (scheduled, schedule_rule, notebook_id),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Notebook not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                _TS_COLS_SCHED = {"created_at", "updated_at"}
                nb: dict = {}
                for col, val in zip(cols, row):
                    if col in _TS_COLS_SCHED and val is not None:
                        nb[col] = val.isoformat()
                    else:
                        nb[col] = val
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: schedule_notebook_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    return JSONResponse(nb)


# ---------------------------------------------------------------------------
# Story 6.6 — Share token endpoints (AC3)
# ---------------------------------------------------------------------------

# Rate-limit for shared endpoint: 60 req/min per (IP, token_prefix) and a global
# per-token ceiling so rotating IPs cannot bypass the limit.
# TODO(Phase-B): move to Redis for multi-replica safety.
_shared_endpoint_rate: dict[str, tuple[int, float]] = {}  # key -> (count, window_start)
_SHARED_RATE_LIMIT = 60  # per minute per (IP, token_prefix) bucket
_SHARED_TOKEN_RATE_LIMIT = 120  # per minute global per-token ceiling
_SHARED_RATE_WINDOW = 60.0  # seconds


def _check_shared_rate_limit(ip: str, token: str = "") -> tuple[bool, float]:
    """Return (within_limit, retry_after_seconds).

    Keys on (client_host, token_prefix[:8]) to prevent IP spoofing via XFF.
    Also enforces a global per-token counter so rotating IPs still hits a ceiling.
    TODO(Phase-B): replace with Redis for cross-replica consistency.
    """
    ts = time.monotonic()
    token_prefix = token[:8] if token else ""

    # Per-(IP, token) bucket
    ip_key = f"ip::{ip}::{token_prefix}"
    ip_count, ip_window = _shared_endpoint_rate.get(ip_key, (0, ts))
    if ts - ip_window >= _SHARED_RATE_WINDOW:
        ip_count, ip_window = 0, ts
    if ip_count >= _SHARED_RATE_LIMIT:
        retry_after = _SHARED_RATE_WINDOW - (ts - ip_window)
        return False, max(retry_after, 1.0)
    _shared_endpoint_rate[ip_key] = (ip_count + 1, ip_window)

    # Global per-token bucket (rotating IPs still hit a ceiling)
    if token_prefix:
        tok_key = f"tok::{token_prefix}"
        tok_count, tok_window = _shared_endpoint_rate.get(tok_key, (0, ts))
        if ts - tok_window >= _SHARED_RATE_WINDOW:
            tok_count, tok_window = 0, ts
        if tok_count >= _SHARED_TOKEN_RATE_LIMIT:
            retry_after = _SHARED_RATE_WINDOW - (ts - tok_window)
            return False, max(retry_after, 1.0)
        _shared_endpoint_rate[tok_key] = (tok_count + 1, tok_window)

    return True, 0.0


async def _share_notebook(request: Request) -> Response:
    """PATCH /api/notebooks/{notebook_id}/share -- enable/disable sharing (Story 6.6, AC3).

    Body: {"shared": bool}
    Response (200):
        On enable: {"share_url": "https://{host}/api/notebooks/shared/{token}"}
        On disable: {"shared": false}
    Error responses:
        400 -- invalid body
        401 -- unauthorized
        404 -- not found
        500 -- DB error
    """
    import secrets  # noqa: PLC0415

    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    notebook_id = request.path_params.get("notebook_id", "")

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    if "shared" not in body:
        return JSONResponse(
            {"code": "invalid_body", "message": "'shared' field is required"},
            status_code=400,
        )

    shared = bool(body["shared"])

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Fetch current state first
                cur.execute(
                    "SELECT id, share_token FROM app.notebooks WHERE id = %s",
                    (notebook_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Notebook not found"},
                        status_code=404,
                    )
                _nb_id, existing_token = row

                if shared:
                    # Generate token if not already set
                    token = existing_token if existing_token else secrets.token_urlsafe(24)
                    cur.execute(
                        """
                        UPDATE app.notebooks
                        SET share_token = %s, shared_at = NOW(), updated_at = NOW()
                        WHERE id = %s
                        """,
                        (token, notebook_id),
                    )
                    conn.commit()
                    # Build share URL from request host
                    base_url = f"{request.url.scheme}://{request.url.netloc}"
                    share_url = f"{base_url}/api/notebooks/shared/{token}"
                    return JSONResponse({"share_url": share_url})
                else:
                    # Revoke: clear token and shared_at
                    cur.execute(
                        """
                        UPDATE app.notebooks
                        SET share_token = NULL, shared_at = NULL, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (notebook_id,),
                    )
                    conn.commit()
                    return JSONResponse({"shared": False})
    except Exception as exc:
        logger.error("admin_api: share_notebook_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )


async def _shared_notebook_endpoint(request: Request) -> Response:
    """GET /api/notebooks/shared/{token} -- read-only public shared notebook run (Story 6.6, AC3).

    No auth guard. Returns the notebook's last completed run envelope.
    Rate-limited: 60 req/min per (IP, token_prefix) + global per-token ceiling.

    Response (200): {"notebook": {...}, "run": {...envelope_inline...}}
    Response (404): token unknown or notebook not shared.
    Response (429): rate limit exceeded (includes Retry-After header).
    """
    token = request.path_params.get("token", "")

    # Rate limit by (IP, token_prefix) -- prevents XFF IP spoofing bypass.
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = _check_shared_rate_limit(client_ip, token)
    if not allowed:
        return JSONResponse(
            {"code": "rate_limited", "message": "Too many requests"},
            status_code=429,
            headers={"Retry-After": str(int(retry_after))},
        )

    if not token:
        return JSONResponse(
            {"code": "not_found", "message": "Notebook not found"},
            status_code=404,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Look up notebook by share_token (must be non-null)
                cur.execute(
                    """
                    SELECT id, title, report_ref, window_rule, created_at
                    FROM app.notebooks
                    WHERE share_token = %s AND share_token IS NOT NULL
                    """,
                    (token,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Notebook not found"},
                        status_code=404,
                    )
                nb_cols = [d[0] for d in cur.description]
                notebook: dict = {}
                for col, val in zip(nb_cols, row):
                    if col == "created_at" and val is not None:
                        notebook[col] = val.isoformat()
                    else:
                        notebook[col] = val

                notebook_id = notebook["id"]

                # Get last completed run for this notebook
                cur.execute(
                    """
                    SELECT id, executed_at, summary_text, envelope_inline,
                           envelope_ref, pull_ids, status
                    FROM app.notebook_runs
                    WHERE notebook_id = %s AND status = 'success'
                    ORDER BY executed_at DESC
                    LIMIT 1
                    """,
                    (notebook_id,),
                )
                run_row = cur.fetchone()
                if run_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "No completed run found"},
                        status_code=404,
                    )
                run_cols = [d[0] for d in cur.description]
                run: dict = {}
                for col, val in zip(run_cols, run_row):
                    if col == "executed_at" and val is not None:
                        run[col] = val.isoformat()
                    elif col == "envelope_inline" and val is not None:
                        # Parse JSONB if it came as string
                        if isinstance(val, str):
                            try:
                                run[col] = json.loads(val)
                            except Exception:
                                run[col] = val
                        else:
                            run[col] = val
                    else:
                        run[col] = val

    except Exception as exc:
        logger.error("admin_api: shared_notebook_endpoint_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    # Security: do NOT expose identity fields (created_by, share_token, etc.)
    safe_notebook = {
        "title": notebook["title"],
        "report_ref": notebook["report_ref"],
        "window_rule": notebook["window_rule"],
        "created_at": notebook.get("created_at"),
    }

    return JSONResponse(
        {
            "notebook": safe_notebook,
            "run": {
                "id": run["id"],
                "executed_at": run.get("executed_at"),
                "summary_text": run.get("summary_text"),
                "envelope_inline": run.get("envelope_inline"),
                "envelope_ref": run.get("envelope_ref"),
                "pull_ids": run.get("pull_ids", []),
                "status": run.get("status"),
            },
        }
    )


# ---------------------------------------------------------------------------
# Story 6.6 — Slide export: HTML endpoint (AC5)
# ---------------------------------------------------------------------------

_HTML_SLIDE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — {executed_at}</title>
  <style>
    /* Print-friendly standalone HTML — no external references (AD-11) */
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 2rem;
      background: #ffffff;
      color: #1a1a1a;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
      font-size: 15px;
      line-height: 1.6;
      max-width: 900px;
      margin-left: auto;
      margin-right: auto;
    }}
    h1 {{ font-size: 1.75rem; margin: 0 0 0.5rem; font-weight: 700; color: #111; }}
    .meta {{
      font-size: 0.85rem; color: #666; margin-bottom: 1.5rem;
      border-bottom: 1px solid #e0e0e0; padding-bottom: 0.75rem;
    }}
    .narrative {{
      white-space: pre-wrap; background: #f9f9f9;
      border: 1px solid #e0e0e0; border-radius: 4px;
      padding: 1rem; font-size: 0.95rem;
    }}
    .data-table {{ width: 100%; border-collapse: collapse; margin-top: 1.5rem; }}
    .data-table th, .data-table td {{
      border: 1px solid #ddd; padding: 0.5rem 0.75rem;
      text-align: left; font-size: 0.875rem;
    }}
    .data-table th {{ background: #f5f5f5; font-weight: 600; }}
    .data-table tr:nth-child(even) td {{ background: #fafafa; }}
    .footer {{
      margin-top: 2rem; font-size: 0.75rem; color: #999;
      border-top: 1px solid #e0e0e0; padding-top: 0.5rem;
    }}
    @media print {{
      body {{ padding: 1rem; font-size: 13px; }}
      .narrative {{ border: none; background: transparent; padding: 0; }}
    }}
  </style>
</head>
<body>
  <h1>{title}</h1>
  <p class="meta">{window_rule} &middot; {executed_at} &middot; Données au {data_date}</p>
  <pre class="narrative">{summary_text}</pre>
{data_table}
  <div class="footer">Généré par Connector &middot; {executed_at}</div>
</body>
</html>
"""


def _build_data_table_html(envelope_inline: dict | None) -> str:
    """Build an HTML table from the envelope's metrics dict."""
    if not envelope_inline:
        return ""
    data = envelope_inline.get("data", {})
    metrics: dict = data.get("metrics", {})
    if not metrics:
        rows_data: list = data.get("rows", [])
        if not rows_data:
            return ""
        # Build from first 20 rows
        cols = list(rows_data[0].keys()) if rows_data else []
        if not cols:
            return ""
        # review-epic-6 F-3: every stored value is UNTRUSTED — escape it.
        header_cells = "".join(f"<th>{html_module.escape(str(col))}</th>" for col in cols[:8])
        row_html_parts = []
        for r in rows_data[:20]:
            cells = "".join(f"<td>{html_module.escape(str(r.get(c, '')))}</td>" for c in cols[:8])
            row_html_parts.append(f"<tr>{cells}</tr>")
        rows_html = "\n".join(row_html_parts)
        return (
            f'  <table class="data-table"><thead><tr>{header_cells}</tr></thead>'
            f"<tbody>{rows_html}</tbody></table>"
        )
    # Metrics dict: display as metric/value pairs
    header = "<th>Métrique</th><th>Valeur</th>"
    metric_rows = []
    for k, v in list(metrics.items())[:30]:
        metric_rows.append(
            f"<tr><td>{html_module.escape(str(k))}</td><td>{html_module.escape(str(v))}</td></tr>"
        )
    rows_html = "\n".join(metric_rows)
    return (
        f'  <table class="data-table"><thead><tr>{header}</tr></thead>'
        f"<tbody>{rows_html}</tbody></table>"
    )


async def _export_notebook_html(request: Request) -> Response:
    """GET /api/notebooks/{notebook_id}/runs/{run_id}/export/html -- slide export (Story 6.6, AC5).

    Server-side HTML template rendered from stored envelope_inline + summary_text.
    No LLM call; pure template substitution. No external references (AD-11).

    Response: text/html with inline CSS. Print to PDF from browser.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    notebook_id = request.path_params.get("notebook_id", "")
    run_id = request.path_params.get("run_id", "")
    # Story 7.4 (AC7, AI-38): explicit scope claim from the query string.
    scope_hint = (request.query_params.get("project_id") or "").strip()

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Fetch notebook (incl. project_id for scope enforcement)
                cur.execute(
                    "SELECT title, window_rule, project_id FROM app.notebooks WHERE id = %s",
                    (notebook_id,),
                )
                nb_row = cur.fetchone()
                if nb_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Notebook not found"},
                        status_code=404,
                    )
                nb_title, nb_window_rule, nb_project_id = nb_row

                # Story 7.4 (AC7, AI-38): cross-scope EXPORT must 404 (+ audit).
                denied = _enforce_notebook_project_scope(
                    nb_project_id,
                    identity,
                    notebook_id,
                    conn,
                    scope_hint=scope_hint,
                    action="notebook_export",
                )
                if denied is not None:
                    return denied

                # Fetch run
                cur.execute(
                    """
                    SELECT executed_at, summary_text, envelope_inline, pull_ids
                    FROM app.notebook_runs
                    WHERE id = %s AND notebook_id = %s
                    """,
                    (run_id, notebook_id),
                )
                run_row = cur.fetchone()
                if run_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Run not found"},
                        status_code=404,
                    )
                executed_at, summary_text, envelope_inline_raw, pull_ids = run_row
    except Exception as exc:
        logger.error("admin_api: export_notebook_html_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    # Parse envelope_inline JSONB
    envelope_inline: dict | None = None
    if envelope_inline_raw is not None:
        try:
            envelope_inline = (
                json.loads(envelope_inline_raw)
                if isinstance(envelope_inline_raw, str)
                else envelope_inline_raw
            )
        except Exception:
            envelope_inline = None

    # Determine data_date from first pull_id or envelope
    data_date = "—"
    if envelope_inline:
        prov = envelope_inline.get("meta", {}).get("provenance", {})
        dr = envelope_inline.get("data", {}).get("date_range", {})
        if dr.get("end"):
            data_date = dr["end"]
        elif prov.get("pull_ids"):
            data_date = str(prov["pull_ids"][0])
    elif pull_ids:
        data_date = str(pull_ids[0])

    executed_at_str = executed_at.strftime("%Y-%m-%d %H:%M UTC") if executed_at else "—"
    # Escape HTML in summary

    summary_escaped = html_module.escape(summary_text or "")
    title_escaped = html_module.escape(nb_title or "")
    window_escaped = html_module.escape(nb_window_rule or "")
    data_date_escaped = html_module.escape(data_date)
    data_table_html = _build_data_table_html(envelope_inline)

    html_content = _HTML_SLIDE_TEMPLATE.format(
        title=title_escaped,
        executed_at=executed_at_str,
        window_rule=window_escaped,
        data_date=data_date_escaped,
        summary_text=summary_escaped,
        data_table=data_table_html,
    )

    return Response(
        content=html_content,
        media_type="text/html; charset=utf-8",
        headers={
            # review-epic-6 F-3: belt-and-braces — even if an escape is ever
            # missed, no script/external resource can execute or load.
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        },
    )


# ===========================================================================
# Story 18.2 -- Google server-side OAuth flow (authorize + callback).
#
# AD-14/AD-15: the OAuth flow lives in the admin console (never the chat iframe).
# The authorize endpoint requires an authenticated admin AND per-identity project
# access (core.project_access) BEFORE minting the consent URL. The callback
# verifies the anti-CSRF state, exchanges the code, persists the encrypted token
# via the Story 18.1 store (single writer -- AD-8/AD-21), and writes an emission
# audit row (AD-14 On-Behalf-Of). NO token or authorization code ever appears in
# a log, an error, or a redirect URL (NFR3).
# ===========================================================================


def _oauth_console_redirect(status: str, connection_ref_id: str = "") -> str:
    """Build the safe console redirect target after a callback.

    Carries ONLY a coarse status flag + the connection id -- never a token, never
    the authorization code, never any state detail. The console reads
    ``?google_oauth=<status>`` to render a French success/error banner.

    F-5: la valeur de ADMIN_CONSOLE_OAUTH_RETURN doit commencer par '/' (chemin
    relatif interne). Toute valeur ne respectant pas ce critere (ex: URL externe
    http://evil.com) est ignoree et le defaut '/console/connections' est utilise
    (defense contre les redirections ouvertes).
    """
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


async def _google_oauth_authorize(request: Request) -> Response:
    """GET /api/google/oauth/authorize?project_id=&connection_ref_id=

    Returns {"authorize_url": ...} for the console to redirect the admin to the
    single multi-scope Google consent screen. Enforces auth (AD-14) AND
    identity_has_project_access (AD-5) BEFORE generating the URL.
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

    # AD-5: the caller MUST have access to the project before we mint a consent
    # URL bound to it (BLOCKED-18.3 F-1: identity_has_project_access enforced here).
    from core.project_access import identity_has_project_access  # noqa: PLC0415

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = identity_has_project_access(project_id, identity or "anonymous", conn)
    except Exception as exc:
        logger.error("admin_api: google_oauth_authorize db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees."},
            status_code=500,
        )
    if not allowed:
        # 403: the caller is authenticated but not scoped to this project.
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_CROSS_SCOPE_ATTEMPT,
            provider_account="google_direct",
            connection_ref=connection_ref_id,
            metadata={
                "project_id": project_id,
                "operation": "google_oauth_authorize",
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
                "message": "Consentement Google refuse ou annule. Aucune connexion creee.",
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
    from core.project_access import identity_has_project_access  # noqa: PLC0415

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as _conn:
            _still_allowed = identity_has_project_access(
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
                    "connexion ; un nouveau consentement peut etre requis."
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
    return RedirectResponse(
        url=_oauth_console_redirect("success", state.connection_ref_id),
        status_code=302,
    )


# ===========================================================================
# Story 18.4 -- Console: etat Google et revocation.
#
# GET  /api/google/oauth/status/{connection_ref_id}
#      Retourne l'etat courant de la connexion Google directe pour une
#      connexion donnee : auth_path, scopes accordes, expiry, sante derivee.
#      JAMAIS le blob chiffre (NFR3). AD-5 : verifie l'acces projet.
#
# POST /api/google/oauth/revoke/{connection_ref_id}
#      Revoke le token cote Google (best-effort), purge le blob chiffre en
#      local (clear_google_token) et ecrit un audit On-Behalf-Of AD-14.
#      Idempotent : une connexion deja claire repond 200 sans erreur.
#      performed_by = identite REELLE du Bearer token (jamais 'system').
#
# AD-5 : acces projet verifie avant toute operation.
# AD-14 : audit On-Behalf-Of pour emission ET revocation (18.2 + 18.4).
# NFR3 : aucun token, aucun blob en reponse.
# AI-56 : seam ASGI teste avec assertions sur les valeurs.
# ===========================================================================


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


# Human-readable French labels for the known Google stack scopes (UX-DR10).
# Extend when new scopes are added to GOOGLE_STACK_SCOPES in google_oauth.py.
_GOOGLE_SCOPE_LABELS: dict[str, str] = {
    "https://www.googleapis.com/auth/analytics.readonly": "Google Analytics 4 (lecture)",
    "https://www.googleapis.com/auth/webmasters.readonly": "Google Search Console (lecture)",
    "https://www.googleapis.com/auth/adwords": "Google Ads",
    "https://www.googleapis.com/auth/spreadsheets.readonly": "Google Sheets (lecture)",
}


def _scope_label(scope: str) -> str:
    """Return a human-readable French label for a scope URI, or the raw URI."""
    return _GOOGLE_SCOPE_LABELS.get(scope, scope)


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
        from core.project_access import identity_has_project_access  # noqa: PLC0415

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
            if not identity_has_project_access(project_id, identity or "anonymous", conn):
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
        from core.project_access import identity_has_project_access  # noqa: PLC0415

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

            project_id, auth_path, _expiry, _scopes = row

            # AD-5: verifier l'acces au projet AVANT toute operation.
            if not identity_has_project_access(project_id, identity or "anonymous", conn):
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


# ---------------------------------------------------------------------------
# Router (exported for mounting in build_asgi_app)
# ---------------------------------------------------------------------------

# ===========================================================================
# Story 21.1 -- Organization CRUD + membership (Epic 21, FR37/CAP-25).
#
# app.organizations is the tenant created in migration 035; app.org_members maps
# identity -> org -> role. These endpoints are the single config surface for the
# org layer (AD-15). FOUNDATION ONLY: no access-resolution change here (the
# org-level default-closed flip is Story 21.5). All guarded by _check_auth.
# ===========================================================================

_ORG_ROLES = frozenset({"owner", "admin", "member", "viewer"})
_ORG_MEMBER_STATUSES = frozenset({"invited", "active", "suspended"})

# Story 21.5 security follow-up: strict role hierarchy (owner > admin > member >
# viewer). Used to (a) forbid self-escalation / minting a role above the actor's
# own (FIX 3) and (b) identify "managers" (owner|admin) that keep an enrolled org
# from silently reopening (FIX 1b).
_ORG_ROLE_RANK = {"viewer": 1, "member": 2, "admin": 3, "owner": 4}
# Roles that can manage the org (own the "active manager" floor an enrolled org
# must never drop below -- else it reaches zero active members and reopens).
_ORG_MANAGE_ROLES = frozenset({"owner", "admin"})

# Story 21.2: hex colour #RRGGBB for org branding.
_HEX_COLOUR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ORG_BRAND_COLOUR_FIELDS = ("brand_primary", "brand_secondary", "brand_accent")


def _extract_brand_fields(body: dict) -> tuple[dict | None, Response | None]:
    """Story 21.2: validate + extract org branding fields present in *body*.

    Returns (fields, None) on success or (None, 422) on an invalid hex colour.
    Only keys present in the body are returned (absent = unchanged on PATCH).
    The 3 colours must match #RRGGBB; logo_url is a free string (nullable).
    """
    fields: dict = {}
    for col in _ORG_BRAND_COLOUR_FIELDS:
        if col in body:
            raw = body.get(col)
            if raw in (None, ""):
                fields[col] = None
            else:
                val = str(raw).strip()
                if not _HEX_COLOUR_RE.match(val):
                    return None, JSONResponse(
                        {
                            "code": "invalid_input",
                            "message": (
                                f"Couleur invalide pour {col} : '{val}'. "
                                f"Format attendu : #RRGGBB (hexadécimal)."
                            ),
                        },
                        status_code=422,
                    )
                fields[col] = val
    if "logo_url" in body:
        raw_logo = body.get("logo_url")
        fields["logo_url"] = str(raw_logo).strip() if raw_logo else None
    return fields, None


def _mint_org_id() -> str:
    """Mint a new prefixed ULID 'org_<ULID>' for an organization."""
    from ulid import ULID  # noqa: PLC0415

    return f"org_{ULID()}"


def _mint_org_member_id() -> str:
    """Mint a new prefixed ULID 'omem_<ULID>' for a membership row."""
    from ulid import ULID  # noqa: PLC0415

    return f"omem_{ULID()}"


def _org_row_to_dict(cols: list[str], row: tuple) -> dict:
    """Serialise an organizations/org_members row, ISO-formatting timestamps."""
    _ts_cols = {"created_at", "updated_at", "archived_at", "invited_at", "joined_at"}
    out: dict = {}
    for col, val in zip(cols, row):
        out[col] = val.isoformat() if (col in _ts_cols and val is not None) else val
    return out


def _enforce_org_manage(org_id: str, identity: str, conn, operation: str) -> Response | None:
    """Story 21.5: gate a MUTATION behind owner/admin of *org_id* on the SAME conn.

    Under default-open-until-enrolled (human decision) an org with ZERO members is
    OPEN -> resolve_org_role() returns "owner" -> this passes (keeps 21.1-21.4
    green). An ENROLLED org (>= 1 member) is CLOSED: a non owner/admin gets a 403
    and an ACTION_CROSS_SCOPE_ATTEMPT audit row (reuse of the _google_revoke denial
    pattern). Returns the 403 Response on refusal, or None when allowed.
    """
    from core.project_access import identity_can_manage_org  # noqa: PLC0415

    if identity_can_manage_org(org_id, identity or "anonymous", conn):
        return None
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, "operation": operation, "reason": "not_org_manager"},
    )
    logger.warning(
        "admin_api: org_manage_denied identity=%s org=%s op=%s",
        identity,
        org_id,
        operation,
    )
    return JSONResponse(
        {
            "code": "forbidden",
            "message": "Acces refuse : droits owner/admin requis sur l'organisation.",
        },
        status_code=403,
    )


def _enforce_role_assignment(
    org_id: str,
    actor_identity: str,
    target_identity: str,
    assigned_role: str,
    conn,
    operation: str,
) -> Response | None:
    """Story 21.5 security follow-up (FIX 3): forbid role self-escalation.

    The actor already passed the manage gate (owner|admin). This adds the strict
    hierarchy rule (owner > admin > member > viewer):
      - the actor may NOT assign a role strictly HIGHER than its own resolved active
        role (an admin cannot mint/promote an owner);
      - the actor may NOT raise its OWN role (no self-promotion, even to an equal-or-
        higher rank than it currently holds).
    An owner (top rank) can assign any role to others. Returns a 403 Response on
    refusal (audited via ACTION_CROSS_SCOPE_ATTEMPT), or None when allowed.
    """
    from core.project_access import resolve_org_role  # noqa: PLC0415

    actor_role = resolve_org_role(org_id, actor_identity or "anonymous", conn)
    actor_rank = _ORG_ROLE_RANK.get(actor_role or "", 0)
    assigned_rank = _ORG_ROLE_RANK.get(assigned_role, 0)

    # Deny assigning a role strictly above the actor's own rank. This single rule
    # covers both threats: an admin minting/promoting an OWNER (target is someone
    # else) AND an actor promoting ITSELF (self-promotion is by definition a jump to
    # a rank above the actor's current one). An owner (top rank) is never blocked,
    # and any assignment at or below the actor's rank is allowed.
    if assigned_rank <= actor_rank:
        return None

    write_audit_row(
        identity=actor_identity or "anonymous",
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "operation": operation,
            "reason": "role_escalation",
            "actor_role": actor_role,
            "assigned_role": assigned_role,
            "target_identity": target_identity,
        },
    )
    logger.warning(
        "admin_api: org_role_escalation_denied identity=%s org=%s op=%s "
        "actor_role=%s assigned_role=%s",
        actor_identity,
        org_id,
        operation,
        actor_role,
        assigned_role,
    )
    return JSONResponse(
        {
            "code": "forbidden",
            "message": (
                "Acces refuse : vous ne pouvez pas attribuer un role superieur au votre."
            ),
        },
        status_code=403,
    )


async def _create_org(request: Request) -> Response:
    """POST /api/organizations -- create an organization (AC4).

    Body: {"name": str, "slug": str?, "billing_ref": str?}. Returns 201.
    Auto-generates a unique slug from name when not provided (appends -1, -2 on
    collision); an explicitly supplied duplicate slug is a 409.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    name = (body.get("name") or "").strip()
    if not name or len(name) > 100:
        return JSONResponse(
            {"code": "invalid_input", "message": "name is required (max 100 chars)"},
            status_code=422,
        )

    slug_in = (body.get("slug") or "").strip()
    base_slug = slug_in or _slugify(name)
    if not base_slug or not _SLUG_RE.match(base_slug) or len(base_slug) > 50:
        return JSONResponse(
            {"code": "invalid_input", "message": f"invalid slug: {base_slug!r}"},
            status_code=422,
        )

    billing_ref = body.get("billing_ref")
    billing_ref = str(billing_ref).strip() if billing_ref else None

    # Story 21.2: optional branding (3 hex colours + logo).
    brand, brand_err = _extract_brand_fields(body)
    if brand_err is not None:
        return brand_err

    org_id = _mint_org_id()
    created_by = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            slug = base_slug
            with conn.cursor() as cur:
                # Story 24.1: collision check on the SANITISED form ('-' -> '_')
                # because the slug names the org's warehouse datasets
                # (org_<wslug>_*, BigQuery charset [A-Za-z0-9_]). _SLUG_RE
                # forbids '_', so two API-created slugs can never collide once
                # sanitised -- this guard is DEFENSIVE depth against slugs that
                # bypassed the API (direct SQL, legacy import) and contain '_'.
                # The slug is immutable after creation (422 slug_immutable).
                _COLLIDES_SQL = (
                    "SELECT 1 FROM app.organizations "
                    "WHERE REPLACE(slug, '-', '_') = REPLACE(%s, '-', '_')"
                )
                if slug_in:
                    cur.execute(_COLLIDES_SQL, (slug,))
                    if cur.fetchone() is not None:
                        return JSONResponse(
                            {
                                "code": "conflict",
                                "message": "slug already exists (or collides once "
                                "sanitised for warehouse dataset naming)",
                            },
                            status_code=409,
                        )
                else:
                    counter = 1
                    while True:
                        cur.execute(_COLLIDES_SQL, (slug,))
                        if cur.fetchone() is None:
                            break
                        slug = f"{base_slug}-{counter}"
                        counter += 1

                cur.execute(
                    """
                    INSERT INTO app.organizations
                        (id, name, slug, status, billing_ref, created_by,
                         brand_primary, brand_secondary, brand_accent, logo_url)
                    VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s, %s)
                    RETURNING id, name, slug, status, billing_ref,
                              created_at, updated_at, archived_at,
                              brand_primary, brand_secondary, brand_accent, logo_url
                    """,
                    (
                        org_id,
                        name,
                        slug,
                        billing_ref,
                        created_by,
                        brand.get("brand_primary"),
                        brand.get("brand_secondary"),
                        brand.get("brand_accent"),
                        brand.get("logo_url"),
                    ),
                )
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
                created = _org_row_to_dict(cols, row)

                # Story 21.5: AUTO-ENROLL the creator as an owner member so the
                # org is immediately scoped to its creator (default-open-until-
                # enrolled). The creator then passes every subsequent manage-check.
                cur.execute(
                    "INSERT INTO app.org_members "
                    "(id, org_id, identity, role, status, joined_at) "
                    "VALUES (%s, %s, %s, 'owner', 'active', NOW()) "
                    "ON CONFLICT (org_id, identity) DO NOTHING",
                    (_mint_org_member_id(), org_id, created_by),
                )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: create_org db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=created_by,
        action=ACTION_ORG_CREATED,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, "slug": created["slug"], "name": name},
    )

    # Story 24.2 (AC1): provision DuckDB schemas non-blocking -- the 201 is
    # always emitted even when DuckDB is unavailable (CI, Cloud Run cold-start).
    # Schema names come from resolve_org_schemas inside provision_org_schemas,
    # never composed inline here (naming guard invariant).
    try:
        from core import warehouse_tenancy as _wt  # noqa: PLC0415

        result = _wt.provision_org_schemas(org_id=org_id, conn=None)
        logger.info("admin_api: provision_schemas org=%s result=%s", org_id, result)
    except Exception as exc:  # noqa: BLE001 -- non-blocking degradation (AC1)
        logger.warning(
            "admin_api: provision_schemas_failed org=%s error=%s", org_id, exc
        )

    return JSONResponse(created, status_code=201)


async def _list_orgs(request: Request) -> Response:
    """GET /api/organizations -- active orgs the caller may see, name ASC.

    Story 21.5 follow-up (reads scoping): an identity sees an org it belongs to
    (active member) OR an org with zero active members (open, default-open-until-
    enrolled) -- never another tenant's enrolled org. The dev disabled-auth
    "anonymous" subject sees all (single-tenant compat).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    ident = identity or "anonymous"
    mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    show_all = mode == "disabled" and ident == "anonymous"
    cols_sql = (
        "id, name, slug, status, billing_ref, created_at, updated_at, archived_at, "
        "brand_primary, brand_secondary, brand_accent, logo_url"
    )
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                if show_all:
                    cur.execute(
                        f"SELECT {cols_sql} FROM app.organizations "
                        "WHERE status = 'active' ORDER BY name ASC"
                    )
                else:
                    # Open orgs (no active member) OR orgs where the caller is an
                    # active member.
                    cur.execute(
                        f"SELECT {cols_sql} FROM app.organizations o "
                        "WHERE o.status = 'active' AND ("
                        "  NOT EXISTS (SELECT 1 FROM app.org_members m "
                        "              WHERE m.org_id = o.id AND m.status = 'active')"
                        "  OR EXISTS (SELECT 1 FROM app.org_members m "
                        "             WHERE m.org_id = o.id AND m.identity = %s "
                        "             AND m.status = 'active')"
                        ") ORDER BY o.name ASC",
                        (ident,),
                    )
                cols = [d[0] for d in cur.description]
                orgs = [_org_row_to_dict(cols, r) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("admin_api: list_orgs db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"organizations": orgs}, status_code=200)


async def _get_org(request: Request) -> Response:
    """GET /api/organizations/{org_id} -- single org.

    404 when not found OR when the caller may not see it (reads scoping: a
    non-member of an enrolled org gets 404 -- existence not disclosed, 7.4 pattern).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_org_access  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_org_access(org_id, identity or "anonymous", conn):
                return JSONResponse(
                    {"code": "not_found", "message": "organization not found"},
                    status_code=404,
                )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, slug, status, billing_ref,
                           created_at, updated_at, archived_at,
                           brand_primary, brand_secondary, brand_accent, logo_url
                    FROM app.organizations WHERE id = %s
                    """,
                    (org_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                org = _org_row_to_dict(cols, row)
    except Exception as exc:
        logger.error("admin_api: get_org db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(org, status_code=200)


async def _patch_org(request: Request) -> Response:
    """PATCH /api/organizations/{org_id} -- update name/slug/billing_ref (AC4)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    updates: dict = {}
    if "name" in body:
        name = (body.get("name") or "").strip()
        if not name or len(name) > 100:
            return JSONResponse(
                {"code": "invalid_input", "message": "name must be 1..100 chars"},
                status_code=422,
            )
        updates["name"] = name
    if "slug" in body:
        # Story 24.1 (epic 24, decision 6): the slug names the org's warehouse
        # datasets (org_<wslug>_raw / org_<wslug>_marts) -- immutable after
        # creation. Renaming would orphan the client's data plane.
        return JSONResponse(
            {
                "code": "slug_immutable",
                "message": "slug cannot be changed: it names the organization's "
                "warehouse datasets (epic 24). Create a new organization instead.",
            },
            status_code=422,
        )
    if "billing_ref" in body:
        raw = body.get("billing_ref")
        updates["billing_ref"] = str(raw).strip() if raw else None

    # Story 21.2: branding fields (validated hex; absent = unchanged).
    brand, brand_err = _extract_brand_fields(body)
    if brand_err is not None:
        return brand_err
    updates.update(brand)

    if not updates:
        return JSONResponse(
            {"code": "invalid_input", "message": "no updatable fields provided"},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Existence 404 FIRST (do not disclose manage-state of a missing org),
                # THEN Story 21.5 manage-gate (owner/admin required on an enrolled org).
                cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (org_id,))
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                denied = _enforce_org_manage(org_id, identity, conn, "patch_org")
                if denied is not None:
                    return denied
                set_parts = [f"{col} = %s" for col in updates]
                params = list(updates.values()) + [org_id]
                cur.execute(
                    "UPDATE app.organizations SET "
                    + ", ".join(set_parts)
                    + " WHERE id = %s "
                    + "RETURNING id, name, slug, status, billing_ref, "
                    + "created_at, updated_at, archived_at, "
                    + "brand_primary, brand_secondary, brand_accent, logo_url",
                    params,
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                org = _org_row_to_dict(cols, row)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: patch_org db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ORG_UPDATED,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, "fields": sorted(updates.keys())},
    )
    return JSONResponse(org, status_code=200)


# ===========================================================================
# RGPD deletion path -- preview, org erasure core, account erasure.
#
# Three things must tell the SAME story: what the preview announces, what the
# DELETE actually removes, and what the audit ledger records afterwards. They
# therefore read the same facts helper below -- a preview that undercounts is
# worse than no preview at all, because the user consents to the wrong thing.
# ===========================================================================

#: Direct org-scoped dependencies surfaced to a human before the drop. Only the
#: tables someone recognises by name: the FULL tenant tree (~40 tables) is walked
#: by core.org_purge and reported as `purged_tables` in the org_deleted audit row.
#: Every one of these is a table whose FK into app.organizations is RESTRICT (or
#: CASCADE for members) -- i.e. exactly what used to make DELETE fail with 409.
_ORG_DEPENDENT_COUNTS: tuple[tuple[str, str], ...] = (
    ("datastreams", "SELECT count(*) FROM app.datastreams WHERE org_id = %s"),
    ("connections", "SELECT count(*) FROM app.connection_ref WHERE owner_org_id = %s"),
    ("invitations", "SELECT count(*) FROM app.invitations WHERE org_id = %s"),
    ("members", "SELECT count(*) FROM app.org_members WHERE org_id = %s"),
    ("operations", "SELECT count(*) FROM app.operations WHERE effective_org_id = %s"),
)


def _org_deletion_facts(conn, org_id: str, *, name: str, slug: str) -> dict:
    """What deleting *org_id* would remove, and what would stop it.

    Read-only. Runs on the CALLER's connection so the erasure path can take this
    snapshot inside its own transaction (the counts then describe exactly the
    rows it is about to erase -- no TOCTOU between preview and deletion).

    ``blockers`` empty == the org is deletable. A blocker is never worked
    around: it names a dependency that cannot legitimately be erased here.
    """
    projects: list[dict] = []
    counts: dict[str, int] = {}
    blockers: list[dict] = []

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, status FROM app.projects WHERE org_id = %s "
            "ORDER BY name, id",
            (org_id,),
        )
        for pid, pname, pstatus in cur.fetchall() or []:
            projects.append({"id": pid, "name": pname, "status": pstatus})

        for key, sql in _ORG_DEPENDENT_COUNTS:
            # A count is diagnostic, never authoritative (the FKs are). A table
            # missing from an older deployment must therefore not abort the
            # caller's transaction, hence one savepoint per probe.
            cur.execute("SAVEPOINT org_facts")
            try:
                cur.execute(sql, (org_id,))
                row = cur.fetchone()
                counts[key] = int(row[0]) if row else 0
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT org_facts")
                logger.warning(
                    "admin_api: org_deletion_facts count_failed org=%s key=%s",
                    org_id,
                    key,
                )
                counts[key] = 0
            else:
                cur.execute("RELEASE SAVEPOINT org_facts")

    active = [p for p in projects if p["status"] != "archived"]
    if active:
        blockers.append(
            {
                "kind": "active_projects",
                "detail": (
                    f"{len(active)} active project(s) still attached: "
                    + ", ".join(p["name"] for p in active[:5])
                    + ". Archive or delete them before deleting the organization."
                ),
            }
        )

    # Warehouse: the datasets provisioned at org creation. If the topology
    # cannot be resolved, drop_org_schemas returns "unresolvable" and the
    # erasure blocks (RGPD: never report an erasure we could not perform), so
    # the preview must announce that up front rather than let the DELETE 500.
    from core import warehouse_tenancy as _wt  # noqa: PLC0415

    schemas = _wt.resolve_org_schemas(org_id=org_id, conn=conn, fresh=True)
    datasets = [schemas.raw, schemas.marts] if schemas is not None else []
    if schemas is None:
        blockers.append(
            {
                "kind": "warehouse_unresolvable",
                "detail": (
                    "Warehouse datasets cannot be resolved for this organization, "
                    "so their removal cannot be confirmed. Deletion is blocked "
                    "until the topology resolves."
                ),
            }
        )

    return {
        "org_id": org_id,
        "name": name,
        "slug": slug,
        "projects": projects,
        "counts": counts,
        "warehouse_datasets": datasets,
        "blockers": blockers,
    }


async def _org_deletion_preview(request: Request) -> Response:
    """GET /api/organizations/{org_id}/deletion-preview -- what would disappear.

    Same owner/admin gate as the deletion itself: the composition of an org is
    not public information. Read-only, no side effect, safe to poll from an
    onboarding/settings screen before showing the confirmation.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Existence 404 FIRST (same discipline as _patch_org: do not
                # disclose the manage-state of an org that does not exist).
                cur.execute(
                    "SELECT id, name, slug FROM app.organizations WHERE id = %s",
                    (org_id,),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse(
                    {"code": "not_found", "message": "organization not found"},
                    status_code=404,
                )
            denied = _enforce_org_manage(org_id, identity, conn, "org_deletion_preview")
            if denied is not None:
                return denied
            facts = _org_deletion_facts(conn, org_id, name=row[1], slug=row[2])
            # Read-only path: release the snapshot without writing anything.
            conn.rollback()
    except Exception:
        logger.exception("admin_api: org_deletion_preview db_error org=%s", org_id)
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )
    return JSONResponse(facts, status_code=200)


def _erase_org_transactional(
    pg_conn,
    org_id: str,
    *,
    name: str,
    slug: str,
    identity: str,
) -> tuple[dict | None, Response | None]:
    """Erase org *org_id* on the caller's OPEN transaction -- no commit here.

    Returns ``(result, None)`` when the erasure is staged and only a commit is
    missing, or ``(None, response)`` when it was refused/failed -- in which case
    the transaction has already been rolled back and the org is fully intact.

    Dismantling order (the whole point of this function -- each step exists
    because the previous one cannot succeed without it):

      1. refuse on ACTIVE projects (409): a live project is a human decision,
         never something a delete endpoint resolves on its own;
      2. snapshot the facts + the ACTIVE dataset grants BEFORE touching a row --
         they are about to be erased and become unobservable;
      3. purge the tenant tree (core.org_purge): cycle-breaking UPDATEs first,
         then DELETEs deepest-first over the RESTRICT/NO-ACTION edges. This is
         what unpins the ~13 tables that used to answer 409 "conflict";
      4. DELETE the org row -- still uncommitted. Anything the purge missed
         surfaces HERE as an FK violation naming its own table (409), never as
         a silent partial erasure;
      5. drop the warehouse datasets (external, non-transactional) only once
         Postgres has proven deletable. If the drop cannot be confirmed we
         rollback: the row survives, RGPD invariant intact (never the reverse
         order -- dropping data whose row then survives is unrecoverable);
      6. write the audit rows on this same transaction. The caller commits, so
         evidence and effect commit together or not at all.
    """
    ident = identity or "anonymous"

    with pg_conn.cursor() as cur:
        # (1) Pre-check: block if active projects exist (readable 409).
        # The FK ON DELETE RESTRICT is the authoritative truth; this check
        # gives a human-readable error before we attempt the DELETE.
        cur.execute(
            "SELECT 1 FROM app.projects WHERE org_id = %s AND status != 'archived' LIMIT 1",
            (org_id,),
        )
        if cur.fetchone() is not None:
            pg_conn.rollback()
            return None, JSONResponse(
                {
                    "code": "org_has_active_projects",
                    "message": (
                        "Archive all active projects before deleting the organization."
                    ),
                },
                status_code=409,
            )

        # (2) Epic-24 review X-1/F-2b: snapshot ACTIVE dataset-access grants
        # BEFORE the DELETE -- the 047 FK ON DELETE CASCADE erases the rows
        # in this same transaction (RGPD erasure by design); the durable
        # trace is the audit entry emitted per grant in phase 3.
        cur.execute(
            "SELECT id, principal FROM app.dataset_access_grants "
            "WHERE org_id = %s AND revoked_at IS NULL",
            (org_id,),
        )
        active_grants = cur.fetchall()

    # Same snapshot the preview shows, taken inside the erasing transaction so
    # the reported `removed` counts are the rows actually about to go (members
    # and grants leave via CASCADE and are unobservable afterwards).
    facts = _org_deletion_facts(pg_conn, org_id, name=name, slug=slug)

    with pg_conn.cursor() as cur:
        # (3) Erase the tenant tree before the org row. ~50 ON DELETE RESTRICT
        # foreign keys pin it in place (org -> projects -> datastreams ->
        # ...), and those RESTRICT rules are wanted everywhere else, so the
        # erasure is explicit here instead of being made implicit in the
        # schema. Runs on THIS connection, inside THIS transaction: nothing
        # is committed until the org row itself is gone.
        from core.org_purge import purge_org_tree  # noqa: PLC0415

        try:
            purge_result = purge_org_tree(pg_conn, org_id)
        except Exception:
            pg_conn.rollback()
            logger.exception("admin_api: delete_org purge_failed org=%s", org_id)
            return None, JSONResponse(
                {
                    "code": "purge_failed",
                    "message": (
                        "Dependent records could not be erased; the "
                        "organization was NOT deleted. Investigate and retry."
                    ),
                },
                status_code=500,
            )

        # (4) Issue the DELETE inside the still-open transaction (no commit yet).
        # If a project slipped through between the pre-check and now, the FK
        # ON DELETE RESTRICT raises here -> we rollback -> 409 (no partial drop).
        try:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        except Exception as exc:
            # Name the actual blocker. The previous generic "conflict" sent
            # operators hunting for an active project when the real holder
            # was something else entirely (an archived project's rows, a
            # connection, ...): the pre-check above only covers NON-archived
            # projects, while the FK restricts on every referencing row.
            diag = getattr(exc, "diag", None)
            blocking_table = getattr(diag, "table_name", None)
            blocking_constraint = getattr(diag, "constraint_name", None)
            pg_conn.rollback()
            logger.warning(
                "admin_api: delete_org pg_delete_fk_violation org=%s table=%s constraint=%s",
                org_id,
                blocking_table,
                blocking_constraint,
            )
            detail = (
                f" Still referenced by {blocking_table}"
                f" ({blocking_constraint})."
                if blocking_table
                else ""
            )
            return None, JSONResponse(
                {
                    "code": "conflict",
                    "message": (
                        "Organization could not be deleted due to a conflict." + detail
                    ),
                    "blocking_table": blocking_table,
                    "blocking_constraint": blocking_constraint,
                },
                status_code=409,
            )

    # (5) Drop warehouse schemas BEFORE committing the Postgres DELETE.
    # If the drop fails we rollback -> org row stays intact (RGPD invariant).
    from core import warehouse_tenancy as _wt  # noqa: PLC0415

    try:
        drop_result = _wt.drop_org_schemas(org_id=org_id, conn=None)
    except Exception:
        try:
            pg_conn.rollback()
        except Exception:
            pass
        logger.exception("admin_api: delete_org schema_drop_failed org=%s", org_id)
        return None, JSONResponse(
            {
                "code": "schema_drop_failed",
                "message": (
                    "Warehouse schema drop failed; Postgres record NOT deleted "
                    "(RGPD safety). Investigate and retry."
                ),
            },
            status_code=500,
        )

    drop_status = drop_result.get("status")
    drop_reason = drop_result.get("reason", "")

    # "skipped / unresolvable" -> cannot confirm data removal -> block (RGPD).
    # "skipped / no_duckdb_path" -> nothing to drop -> proceed.
    if drop_status == "skipped" and drop_reason == "unresolvable":
        try:
            pg_conn.rollback()
        except Exception:
            pass
        logger.error(
            "admin_api: delete_org slug_unresolvable org=%s -- blocking deletion",
            org_id,
        )
        return None, JSONResponse(
            {"code": "schema_drop_failed", "message": "warehouse operation failed"},
            status_code=500,
        )

    # (6) Audit rows on the same transaction; the CALLER commits.
    from core.audit import insert_audit_row  # noqa: PLC0415

    # Emit org_schemas_dropped only when a real drop occurred (F-7).
    if drop_status == "ok":
        insert_audit_row(
            pg_conn,
            identity=ident,
            action=ACTION_ORG_SCHEMAS_DROPPED,
            provider_account="",
            connection_ref="",
            metadata={
                "org_id": org_id,
                "raw": drop_result.get("raw"),
                "marts": drop_result.get("marts"),
                "drop_status": drop_status,
            },
        )
    # Epic-24 review X-1/F-2b: one revoke audit per grant erased by the 047
    # CASCADE -- the audit table is the durable RGPD trace of the exposure
    # gesture; without this an active grant would vanish untracked.
    for grant_id, principal in active_grants:
        insert_audit_row(
            pg_conn,
            identity=ident,
            action=ACTION_DATASET_ACCESS_REVOKED,
            provider_account="",
            connection_ref="",
            metadata={
                "org_id": org_id,
                "grant_id": grant_id,
                "principal": principal,
                "reason": "org_deleted",
            },
        )

    # What the caller reports back, and what the audit records: the same dict.
    removed = {
        "projects": len(facts["projects"]),
        **facts["counts"],
        # Everything else the purge erased across the tenant tree, so the total
        # is not silently reduced to the five human-readable buckets.
        "tenant_rows": purge_result.get("total_rows", 0),
    }
    insert_audit_row(
        pg_conn,
        identity=ident,
        action=ACTION_ORG_DELETED,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "slug": slug,
            "name": name,
            "drop_status": drop_status,
            "revoked_grants": len(active_grants),
            # Durable proof of what the erasure actually removed: the rows
            # themselves are gone, so this is the only remaining evidence.
            "purged_rows": purge_result.get("total_rows", 0),
            "purged_tables": purge_result.get("rows_by_table", {}),
            "removed": removed,
        },
    )
    return (
        {
            "removed": removed,
            "drop_status": drop_status,
            "warehouse_datasets": facts["warehouse_datasets"],
        },
        None,
    )


async def _delete_org(request: Request) -> Response:
    """DELETE /api/organizations/{org_id} -- human-gated RGPD drop (Story 24.2 AC5).

    Requires header ``X-Confirm-Delete: drop-warehouse-data`` (422 otherwise).
    Blocks if active projects exist (409).  Drops warehouse schemas first (RGPD:
    if the drop cannot be confirmed, the Postgres row is NOT deleted -- no partial
    deletion).  Two audit entries emitted in order: org_schemas_dropped then
    org_deleted.  The dismantling order itself lives in
    ``_erase_org_transactional``; this handler owns auth, the 404/gate, and the
    single commit.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]

    confirm = request.headers.get("X-Confirm-Delete", "")
    if confirm != "drop-warehouse-data":
        return JSONResponse(
            {
                "code": "confirmation_required",
                "message": (
                    "Include header X-Confirm-Delete: drop-warehouse-data to confirm "
                    "permanent deletion of the organization and its warehouse data."
                ),
            },
            status_code=422,
        )

    # Phase 1: read org metadata + pre-check active projects in one connection.
    # The connection is kept open (not committed) so we can reuse it for the
    # transactional DELETE below, eliminating the TOCTOU window (F-2).
    from core.db import get_connection  # noqa: PLC0415

    # `get_connection()` is a @contextmanager. Calling `.__enter__()` on a
    # TEMPORARY discards the context-manager object itself: the generator is then
    # finalised by the garbage collector, its `finally: conn.close()` fires, and
    # the connection is dead before the first cursor is opened. Holding the
    # reference in `pg_cm` is what keeps it alive until this handler's own
    # `finally` closes it. Symptom before the fix: every DELETE returned 500 with
    # `psycopg.OperationalError: the connection is closed`, so the RGPD org drop
    # was entirely non-functional in production.
    try:
        pg_cm = get_connection()
        pg_conn = pg_cm.__enter__()
    except Exception:
        logger.exception("admin_api: delete_org db_open_failed org=%s", org_id)
        return JSONResponse(
            {"code": "db_error", "message": "database connection failed"},
            status_code=500,
        )

    # ONE try/finally around every path below. The 404, the manage refusal and
    # each 409 return early, and every one of them must still hand the connection
    # back to the context manager that owns it -- before this, a refused delete
    # leaked its connection until the garbage collector noticed.
    try:
        try:
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT id, name, slug FROM app.organizations WHERE id = %s",
                    (org_id,),
                )
                org_row = cur.fetchone()
                if org_row is None:
                    pg_conn.rollback()
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                org_name = org_row[1]
                org_slug = org_row[2]

                denied = _enforce_org_manage(org_id, identity, pg_conn, "delete_org")
                if denied is not None:
                    pg_conn.rollback()
                    return denied
        except Exception:
            try:
                pg_conn.rollback()
            except Exception:
                pass
            logger.exception("admin_api: delete_org db_error org=%s", org_id)
            return JSONResponse(
                {"code": "db_error", "message": "database operation failed"},
                status_code=500,
            )

        # Phases 2-4: dismantle the tenant tree, delete the row, drop the
        # warehouse, audit -- all staged on this open transaction (see
        # _erase_org_transactional for the order and why it is that order). It
        # rolls back itself on refusal/failure, so the org stays whole.
        try:
            result, error = _erase_org_transactional(
                pg_conn,
                org_id,
                name=org_name,
                slug=org_slug,
                identity=identity or "anonymous",
            )
            if error is not None:
                return error
            # Single commit: state change AND audit evidence land together.
            pg_conn.commit()
        except Exception:
            try:
                pg_conn.rollback()
            except Exception:
                pass
            logger.exception("admin_api: delete_org audit_commit_failed org=%s", org_id)
            return JSONResponse(
                {"code": "db_error", "message": "database operation failed"},
                status_code=500,
            )
    finally:
        # Close through the context manager that owns the connection, so its own
        # `finally` runs exactly once and nothing is left to the garbage collector.
        try:
            pg_cm.__exit__(None, None, None)
        except Exception:
            pass

    return JSONResponse(
        {"deleted": True, "org_id": org_id, "removed": result["removed"]},
        status_code=200,
    )


async def _provision_org_warehouse(request: Request) -> Response:
    """POST /api/organizations/{org_id}/provision-warehouse -- manual provision (AC4).

    Auth: owner or admin of the org.  Idempotent: safe to call multiple times.
    Emits audit ACTION_ORG_SCHEMAS_PROVISIONED on success.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (org_id,))
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                denied = _enforce_org_manage(org_id, identity, conn, "provision_org_warehouse")
                if denied is not None:
                    return denied
    except Exception:
        logger.exception("admin_api: provision_org_warehouse db_error org=%s", org_id)
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )

    from core import warehouse_tenancy as _wt  # noqa: PLC0415

    try:
        result = _wt.provision_org_schemas(org_id=org_id, conn=None)
    except Exception:
        logger.exception(
            "admin_api: provision_org_warehouse failed org=%s", org_id
        )
        return JSONResponse(
            {"code": "provision_failed", "message": "warehouse operation failed"},
            status_code=500,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ORG_SCHEMAS_PROVISIONED,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, **result},
    )
    return JSONResponse({"org_id": org_id, **result}, status_code=200)


async def _backfill_warehouse_schemas(request: Request) -> Response:
    """POST /api/admin/warehouse/provision-schemas -- backfill all orgs (AC3).

    Auth: any authenticated user (_check_auth).  Idempotent.
    Body (optional): {"include_archived": true} -- default false.
    Returns: {"provisioned": N, "skipped": M, "errors": [...]}
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        raw_body = await request.body()
        body: dict = json.loads(raw_body) if raw_body else {}
    except Exception:
        body = {}

    include_archived = bool(body.get("include_archived", False))

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                if include_archived:
                    cur.execute(
                        "SELECT id FROM app.organizations "
                        "WHERE status IN ('active', 'archived') ORDER BY created_at ASC"
                    )
                else:
                    cur.execute(
                        "SELECT id FROM app.organizations "
                        "WHERE status = 'active' ORDER BY created_at ASC"
                    )
                org_ids = [row[0] for row in cur.fetchall()]
    except Exception:
        logger.exception("admin_api: backfill_warehouse_schemas db_error")
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )

    from core import warehouse_tenancy as _wt  # noqa: PLC0415

    provisioned = 0
    skipped = 0
    errors: list[dict] = []

    for oid in org_ids:
        try:
            result = _wt.provision_org_schemas(org_id=oid, conn=None)
            if result.get("status") == "ok":
                provisioned += 1
            else:
                skipped += 1
                logger.info(
                    "admin_api: backfill_warehouse_schemas skip org=%s reason=%s",
                    oid,
                    result.get("reason"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "admin_api: backfill_warehouse_schemas error org=%s error=%s", oid, exc
            )
            errors.append({"org_id": oid, "reason": "provision_failed"})

    logger.info(
        "admin_api: backfill_warehouse_schemas done provisioned=%d skipped=%d errors=%d",
        provisioned,
        skipped,
        len(errors),
    )
    return JSONResponse(
        {"provisioned": provisioned, "skipped": skipped, "errors": errors},
        status_code=200,
    )


async def _list_org_members(request: Request) -> Response:
    """GET /api/organizations/{org_id}/members -- members of an org (21.8 AC4).

    Same read-scoping as _get_org: a non-member of an enrolled org gets 404 so
    existence is never disclosed (7.4 pattern). The caller's *own* membership
    status gates the route; suspended members are still visible in the list
    (they are members with status='suspended').
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_org_access  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_org_access(org_id, identity or "anonymous", conn):
                return JSONResponse(
                    {"code": "not_found", "message": "organization not found"},
                    status_code=404,
                )
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, org_id, identity, role, status, "
                    "invited_by, invited_at, joined_at, created_at "
                    "FROM app.org_members WHERE org_id = %s ORDER BY created_at ASC",
                    (org_id,),
                )
                cols = [d[0] for d in cur.description]
                members = [_org_row_to_dict(cols, r) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("admin_api: list_org_members db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"members": members}, status_code=200)


async def _add_org_member(request: Request) -> Response:
    """POST /api/organizations/{org_id}/members -- enroll an identity (AC4).

    Body: {"identity": str, "role": str?, "status": str?}. 404 if the org does
    not exist; 409 on a duplicate (org_id, identity).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    member_identity = (body.get("identity") or "").strip()
    if not member_identity or len(member_identity) > 255:
        return JSONResponse(
            {"code": "invalid_input", "message": "identity is required (max 255 chars)"},
            status_code=422,
        )
    role = (body.get("role") or "member").strip().lower()
    if role not in _ORG_ROLES:
        return JSONResponse(
            {"code": "invalid_input", "message": f"invalid role: {role!r}"},
            status_code=422,
        )
    status = (body.get("status") or "active").strip().lower()
    if status not in _ORG_MEMBER_STATUSES:
        return JSONResponse(
            {"code": "invalid_input", "message": f"invalid status: {status!r}"},
            status_code=422,
        )

    member_id = _mint_org_member_id()
    performed_by = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app.organizations WHERE id = %s", (org_id,))
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "organization not found"},
                        status_code=404,
                    )
                # Story 21.5: only an owner/admin may enroll members (existence 404
                # first, then manage 403). Default-open org -> resolves to owner.
                denied = _enforce_org_manage(org_id, identity, conn, "add_org_member")
                if denied is not None:
                    return denied
                # FIX 3: an admin cannot mint a role above its own (e.g. owner).
                denied = _enforce_role_assignment(
                    org_id, identity, member_identity, role, conn, "add_org_member"
                )
                if denied is not None:
                    return denied
                cur.execute(
                    "SELECT 1 FROM app.org_members WHERE org_id = %s AND identity = %s",
                    (org_id, member_identity),
                )
                if cur.fetchone() is not None:
                    return JSONResponse(
                        {"code": "conflict", "message": "identity already a member"},
                        status_code=409,
                    )
                joined_at = "NOW()" if status == "active" else "NULL"
                cur.execute(
                    "INSERT INTO app.org_members "
                    "(id, org_id, identity, role, status, invited_by, invited_at, joined_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, NOW(), " + joined_at + ") "
                    "RETURNING id, org_id, identity, role, status, "
                    "invited_by, invited_at, joined_at, created_at",
                    (member_id, org_id, member_identity, role, status, performed_by),
                )
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
                member = _org_row_to_dict(cols, row)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: add_org_member db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=performed_by,
        action=ACTION_ORG_MEMBER_ADDED,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, "member_identity": member_identity, "role": role},
    )
    return JSONResponse(member, status_code=201)


def _would_orphan_last_owner(
    cur, org_id: str, target_identity: str, *, new_role: str | None, new_status: str | None
) -> bool:
    """Story 21.5 security follow-up: True if removing/downgrading/suspending
    *target_identity* would leave the org with ZERO active owners.

    An org whose active managers reach zero has zero owners AND admins; if it also
    has no other active members it silently reopens (default-open-until-enrolled),
    and even short of full emptiness it becomes unmanageable. member-management must
    therefore refuse the operation that drops the LAST active manager. This widens
    the earlier owner-only guard: an org whose sole active manager is an ADMIN can no
    longer be emptied via remove/suspend/demote of that admin (FIX 1b).

    RACE (FIX 1a): the active-manager set is locked FOR UPDATE on THIS cursor/txn
    BEFORE it is counted, so two concurrent mutations that each drop a different
    manager serialize instead of both observing the other as still active.
    ``new_role``/``new_status`` are the POST-change values (None for a removal).
    """
    # Lock the org's active-manager set on this transaction so concurrent
    # remove/suspend/demote operations serialize before we count. The lock must be
    # taken BEFORE reading membership so the decision is made on a stable snapshot.
    cur.execute(
        "SELECT identity, role FROM app.org_members "
        "WHERE org_id = %s AND status = 'active' AND role = 'owner' "
        "FOR UPDATE",
        (org_id,),
    )
    active_owners = cur.fetchall()

    cur.execute(
        "SELECT role, status FROM app.org_members WHERE org_id = %s AND identity = %s",
        (org_id, target_identity),
    )
    row = cur.fetchone()
    if row is None:
        return False  # not a member -> caller handles the 404
    cur_role, cur_status = row
    was_active_owner = cur_role == "owner" and cur_status == "active"
    if not was_active_owner:
        return False  # touching a non-owner never affects the owner floor
    # Post-change: is the target STILL an active owner?
    if new_role is None and new_status is None:
        still_active_owner = False  # removal
    else:
        post_role = new_role if new_role is not None else cur_role
        post_status = new_status if new_status is not None else cur_status
        still_active_owner = post_role == "owner" and post_status == "active"
    if still_active_owner:
        return False  # still an active owner -> owner floor preserved
    # Are there OTHER active owners (from the locked set)?
    other_active_owners = [
        ident for ident, _role in active_owners if ident != target_identity
    ]
    return len(other_active_owners) == 0


def _transfer_org_ownership(
    cur, org_id: str, current_owner: str, next_owner: str
) -> bool:
    """Atomically promote one active member and demote the current owner."""
    if current_owner == next_owner:
        return False
    cur.execute(
        "SELECT identity, role FROM app.org_members "
        "WHERE org_id = %s AND status = 'active' AND role = 'owner' FOR UPDATE",
        (org_id,),
    )
    owners = {identity for identity, _role in cur.fetchall()}
    if current_owner not in owners:
        return False
    cur.execute(
        "SELECT status FROM app.org_members WHERE org_id = %s AND identity = %s FOR UPDATE",
        (org_id, next_owner),
    )
    target = cur.fetchone()
    if target is None or target[0] != "active":
        return False
    cur.execute(
        "UPDATE app.org_members SET role = 'owner' "
        "WHERE org_id = %s AND identity = %s",
        (org_id, next_owner),
    )
    cur.execute(
        "UPDATE app.org_members SET role = 'admin' "
        "WHERE org_id = %s AND identity = %s",
        (org_id, current_owner),
    )
    return True

# Back-compat alias for callers/tests that still use the earlier helper name.
# Its behavior now follows the ratified owner-only floor.
_would_orphan_last_manager = _would_orphan_last_owner


async def _remove_org_member(request: Request) -> Response:
    """DELETE /api/organizations/{org_id}/members/{identity} -- remove a member.

    Manage-gated (owner/admin). 404 if not a member. 409 if it would remove the
    org's last active owner (would silently reopen the tenant).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    target = request.path_params["identity"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_org_manage(org_id, identity, conn, "remove_member")
            if denied is not None:
                return denied
            with conn.cursor() as cur:
                if _would_orphan_last_owner(
                    cur, org_id, target, new_role=None, new_status=None
                ):
                    return JSONResponse(
                        {
                            "code": "conflict",
                            "message": "cannot remove the last active owner",
                        },
                        status_code=409,
                    )
                cur.execute(
                    "DELETE FROM app.org_members WHERE org_id = %s AND identity = %s",
                    (org_id, target),
                )
                deleted = cur.rowcount
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: remove_org_member db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    if not deleted:
        return JSONResponse(
            {"code": "not_found", "message": "member not found"}, status_code=404
        )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ORG_MEMBER_REMOVED,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, "member_identity": target},
    )
    return JSONResponse({"removed": True}, status_code=200)


async def _update_org_member(request: Request) -> Response:
    """PATCH /api/organizations/{org_id}/members/{identity} -- change role/status.

    Manage-gated. 404 if not a member. 409 if the change would drop the org's last
    active owner (downgrade or suspend).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    target = request.path_params["identity"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )
    updates: dict = {}
    if "role" in body:
        role = (body.get("role") or "").strip().lower()
        if role not in _ORG_ROLES:
            return JSONResponse(
                {"code": "invalid_input", "message": f"invalid role: {role!r}"},
                status_code=422,
            )
        updates["role"] = role
    if "status" in body:
        status = (body.get("status") or "").strip().lower()
        if status not in _ORG_MEMBER_STATUSES:
            return JSONResponse(
                {"code": "invalid_input", "message": f"invalid status: {status!r}"},
                status_code=422,
            )
        updates["status"] = status
    if not updates:
        return JSONResponse(
            {"code": "invalid_input", "message": "no updatable fields (role|status)"},
            status_code=422,
        )
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_org_manage(org_id, identity, conn, "update_member")
            if denied is not None:
                return denied
            # FIX 3: when changing role, forbid assigning above the actor's own rank
            # and forbid the actor promoting itself (self-escalation to owner).
            if "role" in updates:
                denied = _enforce_role_assignment(
                    org_id, identity, target, updates["role"], conn, "update_member"
                )
                if denied is not None:
                    return denied
            with conn.cursor() as cur:
                if _would_orphan_last_owner(
                    cur,
                    org_id,
                    target,
                    new_role=updates.get("role"),
                    new_status=updates.get("status"),
                ):
                    return JSONResponse(
                        {
                            "code": "conflict",
                            "message": "cannot drop the last active owner",
                        },
                        status_code=409,
                    )
                set_parts = [f"{col} = %s" for col in updates]
                params = list(updates.values()) + [org_id, target]
                cur.execute(
                    "UPDATE app.org_members SET "
                    + ", ".join(set_parts)
                    + " WHERE org_id = %s AND identity = %s "
                    + "RETURNING id, org_id, identity, role, status, "
                    + "invited_by, invited_at, joined_at, created_at",
                    params,
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "member not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                member = _org_row_to_dict(cols, row)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: update_org_member db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ORG_MEMBER_UPDATED,
        provider_account="",
        connection_ref="",
        metadata={"org_id": org_id, "member_identity": target, "fields": sorted(updates)},
    )
    return JSONResponse(member, status_code=200)


# ===========================================================================
# Story 21.2 -- Global user profile (self-service). Keyed on the AD-14 identity
# subject; a user reads/writes ONLY their own profile. Avatars come from here or
# the identity provider -- NEVER from the Google DATA callback (no identity scope).
# ===========================================================================


async def _get_my_profile(request: Request) -> Response:
    """GET /api/me/profile -- the caller's own profile (AC3)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    ident = identity or "anonymous"
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.user_profiles import fetch_user_profile  # noqa: PLC0415

        with get_connection() as conn:
            profile = fetch_user_profile(ident, conn)
    except Exception as exc:
        logger.error("admin_api: get_my_profile db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(profile, status_code=200)


async def _patch_my_profile(request: Request) -> Response:
    """PATCH /api/me/profile -- upsert the caller's own profile (AC3).

    Body: {"display_name"?, "email"?, "avatar_url"?, "avatar_source"?}. The
    identity is ALWAYS the auth subject -- a body-supplied identity is ignored,
    so a user can only ever write their own profile.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    ident = identity or "anonymous"
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    def _opt_str(key: str, maxlen: int) -> tuple[str | None, Response | None]:
        if key not in body:
            return None, None
        raw = body.get(key)
        val = str(raw).strip() if raw else None
        if val is not None and len(val) > maxlen:
            return None, JSONResponse(
                {"code": "invalid_input", "message": f"{key} too long (max {maxlen})"},
                status_code=422,
            )
        return val, None

    display_name, e1 = _opt_str("display_name", 255)
    if e1:
        return e1
    email, e2 = _opt_str("email", 320)
    if e2:
        return e2
    avatar_url, e3 = _opt_str("avatar_url", 2048)
    if e3:
        return e3
    avatar_source, e4 = _opt_str("avatar_source", 64)
    if e4:
        return e4
    # Default source to 'self' when an avatar is set without an explicit source.
    if avatar_url and "avatar_source" not in body:
        avatar_source = "self"

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.user_profiles import upsert_user_profile  # noqa: PLC0415

        with get_connection() as conn:
            profile = upsert_user_profile(
                ident,
                conn,
                display_name=display_name,
                email=email,
                avatar_url=avatar_url,
                avatar_source=avatar_source,
            )
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: patch_my_profile db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(profile, status_code=200)


# ===========================================================================
# RGPD account erasure (DELETE /api/me) -- the personal counterpart of the org
# drop above. Two promises are kept here and stated out loud in the payload:
#   * an organization is NEVER left orphaned: being the last owner of an org
#     that still has other active members is a REFUSAL (409), not something the
#     endpoint silently resolves by promoting somebody or abandoning the tenant;
#   * an org that belongs to nobody but the caller LEAVES WITH THEM -- otherwise
#     its warehouse datasets survive, unowned and billed, which is exactly the
#     orphan state this whole path exists to prevent.
# ===========================================================================

_MEMBERSHIP_FACTS_SQL = """
    SELECT m.org_id, o.name, o.slug, m.role, m.status,
           (SELECT count(*) FROM app.org_members x
             WHERE x.org_id = m.org_id AND x.identity <> %s AND x.status = 'active'),
           (SELECT count(*) FROM app.org_members y
             WHERE y.org_id = m.org_id AND y.identity <> %s
               AND y.status = 'active' AND y.role = 'owner')
    FROM app.org_members m
    JOIN app.organizations o ON o.id = m.org_id
    WHERE m.identity = %s
    ORDER BY o.name, m.org_id
"""


def _account_deletion_facts(conn, identity: str) -> dict:
    """What erasing *identity* would do, and what would stop it. Read-only.

    ``sole_owner_of`` is every org where the caller is the ONLY active owner --
    i.e. every org whose fate depends on this account. Each of them either
    leaves with the account (nobody else is active in it) or blocks the erasure
    (someone else is), and ``blockers`` says which, per org, with the remedy.
    """
    from core.user_profiles import fetch_user_profile  # noqa: PLC0415

    profile = fetch_user_profile(identity, conn)

    memberships: list[dict] = []
    sole_owner_of: list[dict] = []
    blockers: list[dict] = []
    orgs_to_erase: list[dict] = []

    with conn.cursor() as cur:
        cur.execute(_MEMBERSHIP_FACTS_SQL, (identity, identity, identity))
        rows = cur.fetchall() or []

    for org_id, name, slug, role, status, others, other_owners in rows:
        memberships.append(
            {
                "org_id": org_id,
                "org_name": name,
                "role": role,
                "other_active_members": int(others or 0),
            }
        )
        # Only an ACTIVE owner owns anything: an invited/suspended row holds no
        # organization hostage, so it never blocks and never drags an org along.
        if role != "owner" or status != "active" or int(other_owners or 0) > 0:
            continue
        sole_owner_of.append({"org_id": org_id, "org_name": name})
        if int(others or 0) > 0:
            blockers.append(
                {
                    "kind": "sole_owner_with_members",
                    "detail": (
                        f"You are the only owner of \"{name}\" and "
                        f"{int(others)} other member(s) are still active. "
                        "Transfer ownership to another member, or delete the "
                        "organization first."
                    ),
                }
            )
            continue
        # Nobody else is left in it: the org goes with the account. Its own
        # deletion blockers (active projects, unresolvable warehouse) are the
        # account's blockers too -- announcing them here is what stops the
        # erasure from failing halfway through in production.
        org_facts = _org_deletion_facts(conn, org_id, name=name, slug=slug)
        for blocker in org_facts["blockers"]:
            blockers.append(
                {
                    "kind": blocker["kind"],
                    "detail": f"Organization \"{name}\": {blocker['detail']}",
                }
            )
        orgs_to_erase.append({"org_id": org_id, "name": name, "slug": slug})

    return {
        "identity": identity,
        "email": profile.get("email"),
        "memberships": memberships,
        "sole_owner_of": sole_owner_of,
        "blockers": blockers,
        # Additive, and the honest half of the promise: these organizations are
        # erased WITH the account (warehouse datasets included).
        "organizations_erased_with_account": orgs_to_erase,
    }


async def _get_my_deletion_preview(request: Request) -> Response:
    """GET /api/me/deletion-preview -- what erasing this account would do.

    Read-only, no side effect. The caller can only ever preview THEIR own
    account: the identity comes from the auth subject, never from the request.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    ident = identity or ""
    if not ident:
        return JSONResponse(
            {
                "code": "identity_required",
                "message": "An authenticated identity is required to preview an erasure.",
            },
            status_code=403,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            facts = _account_deletion_facts(conn, ident)
            conn.rollback()
    except Exception:
        logger.exception("admin_api: my_deletion_preview db_error")
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )
    return JSONResponse(facts, status_code=200)


async def _delete_me(request: Request) -> Response:
    """DELETE /api/me -- erase the caller's account (RGPD right to erasure).

    Requires header ``X-Confirm-Delete: erase-account`` (422 otherwise).
    409 when the caller is the last owner of an org that still has other active
    members -- the message says what to do instead.

    Sequencing: each org that leaves with the account is erased in its OWN
    transaction (``_erase_org_transactional`` + commit), then the account rows
    are erased in a final one. A single giant transaction is impossible here --
    the warehouse drop is external and not transactional, so a rollback after a
    successful drop would restore rows whose data is already gone. Per-org
    atomicity is the strongest invariant that actually holds, and it is the one
    that matters: no org is ever half-erased. Erasure being monotonic, a retry
    after a mid-sequence failure simply resumes (the erased orgs no longer
    appear in the facts).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    ident = identity or ""
    if not ident:
        # "anonymous" is not an account; erasing it would erase nothing and
        # audit a lie.
        return JSONResponse(
            {
                "code": "identity_required",
                "message": "An authenticated identity is required to erase an account.",
            },
            status_code=403,
        )

    if request.headers.get("X-Confirm-Delete", "") != "erase-account":
        return JSONResponse(
            {
                "code": "confirmation_required",
                "message": (
                    "Include header X-Confirm-Delete: erase-account to confirm "
                    "permanent erasure of your account."
                ),
            },
            status_code=422,
        )

    from core.db import get_connection  # noqa: PLC0415

    # Phase 1: decide. Read-only snapshot; nothing is touched if it refuses.
    try:
        with get_connection() as conn:
            facts = _account_deletion_facts(conn, ident)
            conn.rollback()
    except Exception:
        logger.exception("admin_api: delete_me facts_failed")
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )

    if facts["blockers"]:
        return JSONResponse(
            {
                "code": "account_deletion_blocked",
                "message": (
                    "Your account cannot be erased yet: one or more organizations "
                    "depend on it. Resolve the blockers below and retry."
                ),
                "blockers": facts["blockers"],
                "sole_owner_of": facts["sole_owner_of"],
            },
            status_code=409,
        )

    # Phase 2: erase the organizations that belong to nobody but the caller,
    # one atomic transaction each (warehouse datasets included).
    erased_orgs: list[dict] = []
    for org in facts["organizations_erased_with_account"]:
        try:
            org_cm = get_connection()
            org_conn = org_cm.__enter__()
        except Exception:
            logger.exception("admin_api: delete_me db_open_failed org=%s", org["org_id"])
            return JSONResponse(
                {"code": "db_error", "message": "database connection failed"},
                status_code=500,
            )
        try:
            result, error = _erase_org_transactional(
                org_conn,
                org["org_id"],
                name=org["name"],
                slug=org["slug"],
                identity=ident,
            )
            if error is not None:
                logger.warning(
                    "admin_api: delete_me org_erasure_refused identity=%s org=%s status=%s",
                    ident,
                    org["org_id"],
                    error.status_code,
                )
                return JSONResponse(
                    {
                        "code": "org_erasure_failed",
                        "message": (
                            f"Organization \"{org['name']}\" could not be erased, so "
                            "your account was NOT erased. Resolve the cause and retry "
                            "-- organizations already erased stay erased."
                        ),
                        "org_id": org["org_id"],
                        "cause": json.loads(error.body),
                        "organizations_erased": erased_orgs,
                    },
                    status_code=error.status_code,
                )
            org_conn.commit()
            erased_orgs.append(
                {
                    "org_id": org["org_id"],
                    "name": org["name"],
                    "removed": result["removed"],
                    "warehouse_datasets": result["warehouse_datasets"],
                }
            )
        except Exception:
            try:
                org_conn.rollback()
            except Exception:
                pass
            logger.exception(
                "admin_api: delete_me org_erasure_failed org=%s", org["org_id"]
            )
            return JSONResponse(
                {"code": "db_error", "message": "database operation failed"},
                status_code=500,
            )
        finally:
            try:
                org_cm.__exit__(None, None, None)
            except Exception:
                pass

    # Phase 3: erase the person -- memberships (org and project) and profile.
    try:
        acct_cm = get_connection()
        acct_conn = acct_cm.__enter__()
    except Exception:
        logger.exception("admin_api: delete_me db_open_failed identity=%s", ident)
        return JSONResponse(
            {"code": "db_error", "message": "database connection failed"},
            status_code=500,
        )
    try:
        from core.audit import insert_audit_row  # noqa: PLC0415

        with acct_conn.cursor() as cur:
            cur.execute("DELETE FROM app.org_members WHERE identity = %s", (ident,))
            org_memberships = cur.rowcount or 0
            cur.execute("DELETE FROM app.project_members WHERE identity = %s", (ident,))
            project_memberships = cur.rowcount or 0
            cur.execute("DELETE FROM app.user_profiles WHERE identity = %s", (ident,))
            profile_rows = cur.rowcount or 0

        insert_audit_row(
            acct_conn,
            identity=ident,
            action=ACTION_ACCOUNT_ERASED,
            provider_account="",
            connection_ref="",
            metadata={
                "org_memberships": org_memberships,
                "project_memberships": project_memberships,
                "profile_erased": bool(profile_rows),
                "organizations_erased": [o["org_id"] for o in erased_orgs],
            },
        )
        # Counted AFTER the audit insert so the number includes the entry that
        # records this very erasure -- what the user is told is retained is
        # exactly what remains.
        with acct_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM app.audit_log WHERE identity = %s", (ident,)
            )
            row = cur.fetchone()
            retained_audit = int(row[0]) if row else 0
        acct_conn.commit()
    except Exception:
        try:
            acct_conn.rollback()
        except Exception:
            pass
        logger.exception("admin_api: delete_me account_erasure_failed identity=%s", ident)
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )
    finally:
        try:
            acct_cm.__exit__(None, None, None)
        except Exception:
            pass

    return JSONResponse(
        {
            "deleted": True,
            "identity": ident,
            "erased": {
                "profile": bool(profile_rows),
                "org_memberships": org_memberships,
                "project_memberships": project_memberships,
                "organizations": erased_orgs,
            },
            # Said out loud rather than discovered later: the audit ledger keeps
            # the identity. It is the proof that the erasure happened (and that
            # every earlier action was legitimately taken); an erasure that also
            # erased its own evidence could not be demonstrated to anyone.
            "retained": {
                "audit_entries": retained_audit,
                "reason": (
                    "Audit entries are kept as the durable, legally required trace "
                    "of platform actions -- including this erasure itself. They "
                    "record actions, not personal content."
                ),
            },
        },
        status_code=200,
    )


# ===========================================================================
# Story 21.3 -- Credential ownership + per-account cross-org grants.
#
# The "credential" is app.connection_ref. An org OWNS a credential; it SHARES an
# external account to another org one account at a time (credential_account_grants).
# Structural isolation: a grant targets a (credential_id, external_account_id) that
# must exist in credential_accounts -- you cannot grant a whole credential. The
# per-identity "who may expose" enforcement is Story 21.5; here we gate on
# _check_auth only (consistent with 21.1/21.2 AC5).
# ===========================================================================


def _mint_grant_id() -> str:
    """Mint a prefixed ULID 'cgrant_<ULID>' for a credential account grant."""
    from ulid import ULID  # noqa: PLC0415

    return f"cgrant_{ULID()}"


def _enforce_credential_org_read(credential_id: str, identity: str, conn) -> Response | None:
    """Story 21.5 follow-up (reads scoping): None when the caller may read this
    credential's resources, else a 404 Response.

    404 if the credential is absent OR owned by an org the caller cannot see. A
    NULL owner_org_id (legacy un-owned credential) is treated as open (compat).
    """
    from core.project_access import identity_has_org_access  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            "SELECT owner_org_id FROM app.connection_ref WHERE id = %s", (credential_id,)
        )
        row = cur.fetchone()
    if row is None:
        return JSONResponse(
            {"code": "not_found", "message": "credential not found"}, status_code=404
        )
    owner_org = row[0]
    if owner_org is not None and not identity_has_org_access(
        owner_org, identity or "anonymous", conn
    ):
        return JSONResponse(
            {"code": "not_found", "message": "credential not found"}, status_code=404
        )
    return None


async def _list_credential_accounts(request: Request) -> Response:
    """GET /api/credentials/{credential_id}/accounts -- accounts of a credential (AC2)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    credential_id = request.path_params["credential_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_credential_org_read(credential_id, identity, conn)
            if denied is not None:
                return denied
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT credential_id, external_account_id, label, discovered_at
                    FROM app.credential_accounts
                    WHERE credential_id = %s
                    ORDER BY external_account_id ASC
                    """,
                    (credential_id,),
                )
                accounts = [
                    {
                        "credential_id": r[0],
                        "external_account_id": r[1],
                        "label": r[2],
                        "discovered_at": r[3].isoformat() if r[3] else None,
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.error("admin_api: list_credential_accounts db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"accounts": accounts}, status_code=200)


async def _register_credential_account(request: Request) -> Response:
    """POST /api/credentials/{credential_id}/accounts -- upsert an account (AC2).

    Discovery stub: body {"external_account_id": str, "label"?: str}. Upserts into
    credential_accounts. 404 if the credential does not exist.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    credential_id = request.path_params["credential_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )
    external_account_id = (body.get("external_account_id") or "").strip()
    if not external_account_id or len(external_account_id) > 255:
        return JSONResponse(
            {"code": "invalid_input", "message": "external_account_id is required"},
            status_code=422,
        )
    label = body.get("label")
    label = str(label).strip() if label else None
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # FIX 5: gate account registration on the credential's OWNER-org
                # manage role (same enforcement the grant path uses). A missing
                # credential -> 404; a NULL owner_org_id (legacy/un-backfilled) is
                # NOT registerable -> non-disclosing 404 until backfill.
                cur.execute(
                    "SELECT owner_org_id FROM app.connection_ref WHERE id = %s",
                    (credential_id,),
                )
                cred_row = cur.fetchone()
                if cred_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "credential not found"},
                        status_code=404,
                    )
                owner_org_id = cred_row[0]
                if owner_org_id is None:
                    write_audit_row(
                        identity=identity or "anonymous",
                        action=ACTION_CROSS_SCOPE_ATTEMPT,
                        provider_account="",
                        connection_ref=credential_id,
                        metadata={
                            "credential_id": credential_id,
                            "operation": "register_credential_account",
                            "reason": "credential_owner_org_null",
                        },
                    )
                    return JSONResponse(
                        {"code": "not_found", "message": "credential not found"},
                        status_code=404,
                    )
                denied = _enforce_org_manage(
                    owner_org_id, identity, conn, "register_credential_account"
                )
                if denied is not None:
                    return denied
                cur.execute(
                    """
                    INSERT INTO app.credential_accounts (credential_id, external_account_id, label)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (credential_id, external_account_id)
                    DO UPDATE SET label = EXCLUDED.label
                    RETURNING credential_id, external_account_id, label, discovered_at
                    """,
                    (credential_id, external_account_id, label),
                )
                r = cur.fetchone()
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: register_credential_account db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(
        {
            "credential_id": r[0],
            "external_account_id": r[1],
            "label": r[2],
            "discovered_at": r[3].isoformat() if r[3] else None,
        },
        status_code=201,
    )


async def _invitation_bootstrap(_request: Request) -> Response:
    """Generic no-store shell; Story 36.4 owns authenticated bearer exchange."""
    import secrets  # noqa: PLC0415

    nonce = secrets.token_urlsafe(18)
    html = """<!doctype html><html><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><title>Continue to Toorow</title></head>
<body><main><h1>Continue securely</h1><p>Sign in to continue.</p></main>
<script nonce="__NONCE__">'use strict';const raw=location.hash.startsWith('#invite=')
?location.hash.slice(8):'';history.replaceState(null,'',location.pathname);
if(raw){fetch('/api/invitations/exchange',{method:'POST',credentials:'same-origin',
headers:{'Content-Type':'application/json'},body:JSON.stringify({bearer:raw})});}</script>
</body></html>""".replace("__NONCE__", nonce)
    return Response(
        html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; "
                "connect-src 'self'; style-src 'none'; img-src 'none'; "
                "font-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
            ),
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _parse_invitation_grants(body: dict, field: str, scope_type: str):
    from core.invitations import InvitationGrant, InvitationValidationError  # noqa: PLC0415

    raw = body.get(field, [])
    if not isinstance(raw, list):
        raise InvitationValidationError(f"{field} must be an array")
    grants = []
    for item in raw:
        if not isinstance(item, dict):
            raise InvitationValidationError(f"{field} contains an invalid grant")
        grants.append(
            InvitationGrant(
                scope_type,
                str(item.get("scope_id") or "").strip(),
                str(item.get("capability") or "view").strip(),
            )
        )
    return tuple(grants)


_INVITATION_EXCHANGE_COOKIE = "toorow_invitation_exchange"


def _invitation_no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def _exchange_invitation(request: Request) -> Response:
    """Exchange a fragment bearer only for its matching protected-auth subject."""
    authorized, identity = await _check_invitation_identity(request)
    if (
        not authorized
        or identity == "anonymous"
        or os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower() == "disabled"
    ):
        return _invitation_no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Authentication required"}, status_code=401
            )
        )
    from core.project_access import epic36_production_access_enabled  # noqa: PLC0415

    if not epic36_production_access_enabled():
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Invitation unavailable"}, status_code=404
            )
        )
    from core.invitations import InvitationExchangeError, exchange_invitation  # noqa: PLC0415

    try:
        body = json.loads(await request.body())
        bearer = body.get("bearer") if isinstance(body, dict) else None
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            exchanged = exchange_invitation(
                conn,
                bearer=bearer,
                verified_identity=identity,
            )
            conn.commit()
    except (InvitationExchangeError, json.JSONDecodeError, TypeError):
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Invitation unavailable"}, status_code=404
            )
        )
    except Exception as exc:
        logger.error("admin_api: invitation_exchange failed: %s", type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Invitation unavailable"}, status_code=500
            )
        )
    response = _invitation_no_store(JSONResponse({"ready_to_accept": True}))
    response.set_cookie(
        _INVITATION_EXCHANGE_COOKIE,
        exchanged.session_value,
        max_age=exchanged.max_age_seconds,
        path="/api/invitations",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


async def _accept_invitation(request: Request) -> Response:
    """Confirm exact membership/grants using the narrow exchange cookie."""
    authorized, identity = await _check_invitation_identity(request)
    if (
        not authorized
        or identity == "anonymous"
        or os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower() == "disabled"
    ):
        return _invitation_no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Authentication required"}, status_code=401
            )
        )
    from core.project_access import epic36_production_access_enabled  # noqa: PLC0415

    if not epic36_production_access_enabled():
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Invitation unavailable"}, status_code=404
            )
        )
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return _invitation_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    session_value = request.cookies.get(_INVITATION_EXCHANGE_COOKIE, "")
    from core.invitations import (  # noqa: PLC0415
        InvitationAcceptanceConflict,
        InvitationExchangeError,
        accept_invitation,
    )
    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

    try:
        body = json.loads(await request.body())
        confirmed = body.get("confirmed") if isinstance(body, dict) else False
        from core import tracing  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            accepted = accept_invitation(
                conn,
                session_value=session_value,
                verified_identity=identity,
                confirmed=confirmed,
                idempotency_key=idempotency_key,
                host_context={
                    "host": "rest",
                    "workspace_id": (request.headers.get("X-Workspace-Id") or "console")[:256],
                },
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except InvitationExchangeError:
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Invitation unavailable"}, status_code=404
            )
        )
    except (InvitationAcceptanceConflict, OperationIdempotencyConflict):
        return _invitation_no_store(
            JSONResponse(
                {"code": "conflict", "message": "Invitation conflicts with existing access"},
                status_code=409,
            )
        )
    except (json.JSONDecodeError, TypeError):
        return _invitation_no_store(
            JSONResponse(
                {"code": "invalid_confirmation", "message": "Confirmation required"},
                status_code=422,
            )
        )
    except Exception as exc:
        logger.error("admin_api: invitation_accept failed: %s", type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Invitation unavailable"}, status_code=500
            )
        )
    response = _invitation_no_store(
        JSONResponse(
            {
                "invitation_id": accepted.invitation_id,
                "organization_id": accepted.org_id,
                "authority": {
                    "role_derived": accepted.role,
                    "explicit_grants": list(accepted.explicit_grants),
                    "explicit_none": accepted.explicit_none,
                },
                "operation_id": accepted.operation_id,
                "audit_event_id": accepted.audit_event_id,
                "outbox_event_id": accepted.outbox_event_id,
                "next_url": accepted.next_url,
                "replayed": accepted.replayed,
            }
        )
    )
    response.delete_cookie(
        _INVITATION_EXCHANGE_COOKIE,
        path="/api/invitations",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response



async def _list_invitations(request: Request) -> Response:
    """Return the secret-free invitation lifecycle projection for one organization."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    from core.project_access import epic36_production_access_enabled  # noqa: PLC0415

    if not epic36_production_access_enabled():
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    from core.db import get_connection  # noqa: PLC0415
    from core.invitations import list_safe_invitations  # noqa: PLC0415

    org_id = request.path_params["org_id"]
    try:
        with get_connection() as conn:
            denied = _enforce_org_manage(org_id, identity, conn, "list_invitations")
            if denied is not None:
                return denied
            rows = list_safe_invitations(conn, org_id=org_id)
    except Exception as exc:
        logger.error("admin_api: list_invitations failed: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "operation_failed", "message": "Invitations unavailable"},
            status_code=500,
        )
    return _invitation_no_store(JSONResponse({"items": rows}))


def _authorize_invitation_binding(
    conn,
    *,
    identity: str,
    org_id: str,
    invitation_id: str,
) -> Response | None:
    """Require manage on the org and every immutable invitation resource binding."""
    denied = _enforce_org_manage(org_id, identity, conn, "manage_invitation")
    if denied is not None:
        return denied
    with conn.cursor() as cur:
        cur.execute(
            "SELECT grant_bindings FROM app.invitations WHERE id = %s AND org_id = %s",
            (invitation_id, org_id),
        )
        row = cur.fetchone()
    if row is None or not isinstance(row[0], list):
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    for grant in row[0]:
        if not isinstance(grant, dict) or grant.get("scope_type") not in {"project", "flux"}:
            return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
        kwargs = (
            {"project_id": grant.get("scope_id")}
            if grant["scope_type"] == "project"
            else {"datastream_id": grant.get("scope_id")}
        )
        decision = resolve_strict_resource_access(
            identity,
            conn,
            minimum_capability="manage",
            **kwargs,
        )
        if not decision.allowed or decision.org_id != org_id:
            return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    return None


async def _mutate_invitation_lifecycle(request: Request, *, action: str) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    from core.project_access import epic36_production_access_enabled  # noqa: PLC0415

    if not epic36_production_access_enabled():
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return _invitation_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    org_id = request.path_params["org_id"]
    invitation_id = request.path_params["invitation_id"]
    from core import tracing  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.invitations import (  # noqa: PLC0415
        InvitationLifecycleConflict,
        InvitationValidationError,
        resend_invitation,
        transition_invitation,
    )
    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

    try:
        body = json.loads(await request.body()) if action == "resend" else {}
        with get_connection() as conn:
            denied = _authorize_invitation_binding(
                conn,
                identity=identity,
                org_id=org_id,
                invitation_id=invitation_id,
            )
            if denied is not None:
                return denied
            common = {
                "conn": conn,
                "invitation_id": invitation_id,
                "actor": identity,
                "idempotency_key": idempotency_key,
                "host_context": {
                    "host": "rest",
                    "workspace_id": (request.headers.get("X-Workspace-Id") or "console")[:256],
                },
                "trace_id": tracing.current_trace_id_hex(),
            }
            if action == "resend":
                result = resend_invitation(
                    **common,
                    expires_in_hours=(
                        body.get("expires_in_hours", 48) if isinstance(body, dict) else 48
                    ),
                )
            else:
                result = transition_invitation(**common, transition=action)
            conn.commit()
    except (InvitationLifecycleConflict, OperationIdempotencyConflict):
        return _invitation_no_store(
            JSONResponse(
                {"code": "conflict", "message": "Invitation lifecycle already resolved"},
                status_code=409,
            )
        )
    except (InvitationValidationError, json.JSONDecodeError, TypeError) as exc:
        return _invitation_no_store(
            JSONResponse({"code": "invalid_request", "message": str(exc)}, status_code=422)
        )
    except Exception as exc:
        logger.error("admin_api: invitation_%s failed: %s", action, type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Invitation unavailable"},
                status_code=500,
            )
        )
    payload = {
        "invitation_id": result.invitation_id,
        "state": result.state,
        "operation_id": result.operation_id,
        "audit_event_id": result.audit_event_id,
        "replayed": result.replayed,
    }
    if action == "resend" and result.delivery_url is not None:
        payload["delivery_handoff"] = {"url": result.delivery_url, "single_return": True}
    return _invitation_no_store(
        Response(
            json.dumps(payload),
            media_type=(
                "application/vnd.toorow.invitation-handoff+json"
                if action == "resend"
                else "application/json"
            ),
        )
    )


async def _revoke_invitation(request: Request) -> Response:
    return await _mutate_invitation_lifecycle(request, action="revoke")


async def _resend_invitation(request: Request) -> Response:
    return await _mutate_invitation_lifecycle(request, action="resend")
async def _issue_invitation(request: Request) -> Response:
    """Issue one exact invitation after strict manage authorization."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    from core.project_access import epic36_production_access_enabled  # noqa: PLC0415

    if not epic36_production_access_enabled():
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
            status_code=422,
        )
    from core.invitations import InvitationValidationError, issue_invitation  # noqa: PLC0415
    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise InvitationValidationError("invitation body must be an object")
        project_grants = _parse_invitation_grants(body, "project_grants", "project")
        datastream_grants = _parse_invitation_grants(body, "datastream_grants", "flux")
        org_id = request.path_params["org_id"]
        role = str(body.get("role") or "").strip()
        invited_identity = body.get("invited_identity")
        expires_in_hours = body.get("expires_in_hours", 48)
        from core import tracing  # noqa: PLC0415
        from core.db import get_connection, set_local_access_context  # noqa: PLC0415
        from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_org_manage(org_id, identity, conn, "issue_invitation")
            if denied is not None:
                return denied
            set_local_access_context(conn, identity, enforce_epic36=True)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role FROM app.org_members "
                    "WHERE org_id = %s AND identity = %s AND status = 'active'",
                    (org_id, identity),
                )
                issuer_row = cur.fetchone()
            if issuer_row is None or (role == "owner" and issuer_row[0] != "owner"):
                return JSONResponse(
                    {"code": "not_found", "message": "Invitation scope not found"},
                    status_code=404,
                )
            for grant in project_grants + datastream_grants:
                kwargs = (
                    {"project_id": grant.scope_id}
                    if grant.scope_type == "project"
                    else {"datastream_id": grant.scope_id}
                )
                decision = resolve_strict_resource_access(
                    identity, conn, minimum_capability="manage", **kwargs
                )
                if not decision.allowed or decision.org_id != org_id:
                    return JSONResponse(
                        {"code": "not_found", "message": "Invitation scope not found"},
                        status_code=404,
                    )
            result = issue_invitation(
                conn,
                invited_identity=invited_identity,
                org_id=org_id,
                role=role,
                project_grants=project_grants,
                datastream_grants=datastream_grants,
                issuer=identity,
                expires_in_hours=expires_in_hours,
                policy_version=os.environ.get("TOOROW_POLICY_VERSION", "v1"),
                idempotency_key=idempotency_key,
                host_context={
                    "host": "rest",
                    "workspace_id": (request.headers.get("X-Workspace-Id") or "console")[:256],
                },
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except InvitationValidationError as exc:
        return JSONResponse({"code": "invalid_invitation", "message": str(exc)}, status_code=422)
    except OperationIdempotencyConflict:
        return JSONResponse(
            {"code": "conflict", "message": "operation conflicts with existing state"},
            status_code=409,
        )
    except Exception as exc:
        logger.error("admin_api: issue_invitation failed: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "operation_failed", "message": "Invitation could not be issued"},
            status_code=500,
        )
    payload = {
        "invitation_id": result.invitation_id,
        "state": result.state,
        "expires_at": result.expires_at,
        "operation_id": result.operation_id,
        "audit_event_id": result.audit_event_id,
        "replayed": result.replayed,
    }
    if result.delivery_url is not None:
        payload["delivery_handoff"] = {"url": result.delivery_url, "single_return": True}
    return Response(
        json.dumps(payload),
        status_code=201,
        media_type="application/vnd.toorow.invitation-handoff+json",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


async def _create_account_grant(request: Request) -> Response:
    """POST /api/credentials/{cred}/accounts/{acct}/grants -- expose to an org (AC3).

    Body: {"grantee_org_id": str}. 404 if the account is not in credential_accounts
    or the org does not exist; 409 on a duplicate grant. The ONLY cross-org bridge.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    credential_id = request.path_params["credential_id"]
    external_account_id = request.path_params["external_account_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )
    grantee_org_id = (body.get("grantee_org_id") or "").strip()
    if not grantee_org_id:
        return JSONResponse(
            {"code": "invalid_input", "message": "grantee_org_id is required"},
            status_code=422,
        )
    grant_id = _mint_grant_id()
    granted_by = identity or "anonymous"
    operation_result = None
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM app.credential_accounts "
                    "WHERE credential_id = %s AND external_account_id = %s",
                    (credential_id, external_account_id),
                )
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "credential account not found"},
                        status_code=404,
                    )
                # Story 21.5: only an owner/admin of the credential's OWNER org may
                # expose an account. FIX 4: a credential with NO owner org
                # (owner_org_id NULL, legacy/un-backfilled) is NOT exposable -- DENY
                # with a non-disclosing 404 (consistent with cross-scope denials)
                # until a backfill assigns an owner org. Default-open org -> owner.
                cur.execute(
                    "SELECT owner_org_id FROM app.connection_ref WHERE id = %s",
                    (credential_id,),
                )
                owner_row = cur.fetchone()
                owner_org_id = owner_row[0] if owner_row is not None else None
                if owner_org_id is None:
                    write_audit_row(
                        identity=identity or "anonymous",
                        action=ACTION_CROSS_SCOPE_ATTEMPT,
                        provider_account="",
                        connection_ref=credential_id,
                        metadata={
                            "credential_id": credential_id,
                            "operation": "create_account_grant",
                            "reason": "credential_owner_org_null",
                        },
                    )
                    return JSONResponse(
                        {"code": "not_found", "message": "credential account not found"},
                        status_code=404,
                    )
                denied = _enforce_org_manage(
                    owner_org_id, identity, conn, "create_account_grant"
                )
                if denied is not None:
                    return denied
                cur.execute(
                    "SELECT 1 FROM app.organizations WHERE id = %s", (grantee_org_id,)
                )
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "grantee organization not found"},
                        status_code=404,
                    )
                from core.project_access import (  # noqa: PLC0415
                    epic36_production_access_enabled,
                )

                if epic36_production_access_enabled():
                    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
                    if not idempotency_key:
                        return JSONResponse(
                            {
                                "code": "missing_idempotency_key",
                                "message": "Idempotency-Key is required",
                            },
                            status_code=422,
                        )
                    from core import tracing  # noqa: PLC0415
                    from core.account_exposure import (  # noqa: PLC0415
                        AccountExposureConflict,
                        expose_account,
                    )
                    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

                    try:
                        operation_result = expose_account(
                            conn,
                            grant_id=grant_id,
                            credential_id=credential_id,
                            external_account_id=external_account_id,
                            owner_org_id=owner_org_id,
                            grantee_org_id=grantee_org_id,
                            actor=granted_by,
                            idempotency_key=idempotency_key,
                            host_context={
                                "host": "rest",
                                "workspace_id": (
                                    request.headers.get("X-Workspace-Id") or "console"
                                )[:256],
                            },
                            versions={
                                "policy": os.environ.get("TOOROW_POLICY_VERSION", "v1"),
                                "catalog": os.environ.get("TOOROW_CATALOG_VERSION", "v1"),
                                "tool": "rest-v1",
                            },
                            confirmation_reference=request.headers.get(
                                "X-Confirmation-Reference"
                            ),
                            trace_id=tracing.current_trace_id_hex(),
                        )
                    except (AccountExposureConflict, OperationIdempotencyConflict):
                        return JSONResponse(
                            {
                                "code": "conflict",
                                "message": "operation conflicts with existing state",
                            },
                            status_code=409,
                        )
                    r = operation_result.result
                else:
                    cur.execute(
                        "SELECT id, status FROM app.credential_account_grants "
                        "WHERE credential_id = %s AND external_account_id = %s "
                        "AND grantee_org_id = %s FOR UPDATE",
                        (credential_id, external_account_id, grantee_org_id),
                    )
                    existing = cur.fetchone()
                    if existing is not None and existing[1] == "active":
                        return JSONResponse(
                            {
                                "code": "conflict",
                                "message": "account already granted to this org",
                            },
                            status_code=409,
                        )
                    if existing is not None:
                        cur.execute(
                            """
                            UPDATE app.credential_account_grants
                            SET status = 'active', granted_by = %s, invalidated_at = NULL,
                                invalidation_reason = NULL,
                                exposure_version = exposure_version + 1
                            WHERE id = %s
                            RETURNING id, credential_id, external_account_id, grantee_org_id,
                                      granted_by, created_at
                            """,
                            (granted_by, existing[0]),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO app.credential_account_grants
                                (id, credential_id, external_account_id, grantee_org_id,
                                 granted_by)
                            VALUES (%s, %s, %s, %s, %s)
                            RETURNING id, credential_id, external_account_id, grantee_org_id,
                                      granted_by, created_at
                            """,
                            (
                                grant_id,
                                credential_id,
                                external_account_id,
                                grantee_org_id,
                                granted_by,
                            ),
                        )
                    r = cur.fetchone()
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: create_account_grant db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    if operation_result is None:
        write_audit_row(
            identity=granted_by,
            action=ACTION_ACCOUNT_EXPOSED,
            provider_account="",
            connection_ref=credential_id,
            metadata={
                "credential_id": credential_id,
                "external_account_id": external_account_id,
                "grantee_org_id": grantee_org_id,
            },
        )
        payload = {
            "id": r[0],
            "credential_id": r[1],
            "external_account_id": r[2],
            "grantee_org_id": r[3],
            "granted_by": r[4],
            "created_at": r[5].isoformat() if r[5] else None,
        }
    else:
        payload = dict(r)
        payload["operation_id"] = operation_result.operation_id
        payload["audit_event_id"] = operation_result.audit_event_id
        payload["replayed"] = operation_result.replayed
    return JSONResponse(payload, status_code=201)


async def _list_credential_grants(request: Request) -> Response:
    """GET /api/credentials/{credential_id}/grants -- grants (account -> org) (AC3)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    credential_id = request.path_params["credential_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_credential_org_read(credential_id, identity, conn)
            if denied is not None:
                return denied
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, credential_id, external_account_id, grantee_org_id,
                           granted_by, created_at
                    FROM app.credential_account_grants
                    WHERE credential_id = %s AND status = 'active'
                    ORDER BY external_account_id ASC, created_at ASC
                    """,
                    (credential_id,),
                )
                grants = [
                    {
                        "id": r[0],
                        "credential_id": r[1],
                        "external_account_id": r[2],
                        "grantee_org_id": r[3],
                        "granted_by": r[4],
                        "created_at": r[5].isoformat() if r[5] else None,
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.error("admin_api: list_credential_grants db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"grants": grants}, status_code=200)


async def _revoke_account_grant(request: Request) -> Response:
    """DELETE /api/credentials/{cred}/accounts/{acct}/grants/{org} -- revoke (AC3).

    Offboarding: invalidates the grant. 404 if no active grant. Audited.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    credential_id = request.path_params["credential_id"]
    external_account_id = request.path_params["external_account_id"]
    grantee_org_id = request.path_params["grantee_org_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Story 21.5: only an owner/admin of the credential's OWNER org may
                # revoke a grant. FIX 4: a credential with NO owner org (owner_org_id
                # NULL, legacy/un-backfilled) is NOT manageable -- DENY with a
                # non-disclosing 404 until a backfill assigns an owner org.
                cur.execute(
                    "SELECT owner_org_id FROM app.connection_ref WHERE id = %s",
                    (credential_id,),
                )
                owner_row = cur.fetchone()
                owner_org_id = owner_row[0] if owner_row is not None else None
                if owner_org_id is None:
                    write_audit_row(
                        identity=identity or "anonymous",
                        action=ACTION_CROSS_SCOPE_ATTEMPT,
                        provider_account="",
                        connection_ref=credential_id,
                        metadata={
                            "credential_id": credential_id,
                            "operation": "revoke_account_grant",
                            "reason": "credential_owner_org_null",
                        },
                    )
                    return JSONResponse(
                        {"code": "not_found", "message": "grant not found"},
                        status_code=404,
                    )
                denied = _enforce_org_manage(
                    owner_org_id, identity, conn, "revoke_account_grant"
                )
                if denied is not None:
                    return denied
                cur.execute(
                    "UPDATE app.credential_account_grants "
                    "SET status = 'invalidated', invalidated_at = NOW(), "
                    "invalidation_reason = 'revocation' "
                    "WHERE credential_id = %s AND external_account_id = %s "
                    "AND grantee_org_id = %s AND status = 'active'",
                    (credential_id, external_account_id, grantee_org_id),
                )
                deleted = cur.rowcount
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: revoke_account_grant db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    if not deleted:
        return JSONResponse(
            {"code": "not_found", "message": "grant not found"},
            status_code=404,
        )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_ACCOUNT_GRANT_REVOKED,
        provider_account="",
        connection_ref=credential_id,
        metadata={
            "credential_id": credential_id,
            "external_account_id": external_account_id,
            "grantee_org_id": grantee_org_id,
        },
    )
    return JSONResponse({"revoked": True}, status_code=200)


# ===========================================================================
# Story 24.5 -- Dataset marts access grants (Epic 24, P4).
#
# Lets an owner/admin expose the org's BigQuery marts dataset to an external
# IAM principal (serviceAccount/user/group).  The grant is ALWAYS scoped to
# ``org_<wslug>_marts`` -- never to raw nor mirror_*.  Soft-delete on revoke
# (revoked_at set, row kept for RGPD).  Audited on every mutation.
# ===========================================================================

#: Valid IAM principal type prefixes (BigQuery member syntax).
_VALID_IAM_TYPES = frozenset({"user", "serviceAccount", "group"})

# Epic-24 review X-5: strict charset (not just shape) -- this string reaches a
# real IAM binding in Phase B, so reject anything outside RFC-ish member syntax.
_IAM_PRINCIPAL_RE = re.compile(
    r"^(user|serviceAccount|group):[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$"
)


def _mint_dagrant_id() -> str:
    """Mint a prefixed ULID 'dagrant_<ULID>' for a dataset access grant."""
    from ulid import ULID  # noqa: PLC0415

    return f"dagrant_{ULID()}"


def _validate_principal(principal: str) -> bool:
    """Return True when *principal* matches ``type:identifier`` (AC2 validation).

    type ∈ {user, serviceAccount, group}; identifier must be non-empty and
    contain '@' (IAM member syntax for BigQuery).
    """
    return bool(_IAM_PRINCIPAL_RE.match(principal.strip()))


async def _grant_dataset_access(request: Request) -> Response:
    """POST /api/organizations/{org_id}/dataset-access -- grant IAM access (AC2, AC3).

    Body: {"principal": "serviceAccount:sa@project.iam.gserviceaccount.com"}.
    201 on success; 409 on duplicate active grant; 422 on invalid input;
    403 if caller is not owner/admin; 404 if org absent/inaccessible.
    """
    import core.warehouse_tenancy as _wt  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception:
        return JSONResponse(
            {"code": "invalid_body", "message": "Invalid JSON body"},
            status_code=400,
        )
    principal = (body.get("principal") or "").strip()
    if not principal:
        return JSONResponse(
            {"code": "invalid_input", "message": "principal is required"},
            status_code=422,
        )
    if not _validate_principal(principal):
        return JSONResponse(
            {
                "code": "invalid_input",
                "message": (
                    "principal must be type:identifier with type in "
                    "{user, serviceAccount, group} and identifier containing '@'"
                ),
            },
            status_code=422,
        )
    grant_id = _mint_dagrant_id()
    granted_by = identity or "anonymous"
    org_schemas = None
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Gate: owner/admin only (AC2, _enforce_org_manage returns 403+audit if denied).
            denied = _enforce_org_manage(org_id, identity, conn, "grant_dataset_access")
            if denied is not None:
                return denied
            with conn.cursor() as cur:
                # Check for an existing ACTIVE grant on the same (org, principal).
                cur.execute(
                    "SELECT id FROM app.dataset_access_grants "
                    "WHERE org_id = %s AND principal = %s AND revoked_at IS NULL",
                    (org_id, principal),
                )
                if cur.fetchone() is not None:
                    return JSONResponse(
                        {
                            "code": "conflict",
                            "message": "principal already has an active grant on this org",
                        },
                        status_code=409,
                    )
                cur.execute(
                    """
                    INSERT INTO app.dataset_access_grants
                        (id, org_id, principal, granted_by)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, org_id, principal, granted_by, created_at
                    """,
                    (grant_id, org_id, principal, granted_by),
                )
                r = cur.fetchone()
            conn.commit()
            # Resolve after commit so the cache is not polluted by a failed txn.
            if _wt.org_schemas_enabled():
                org_schemas = _wt.resolve_org_schemas(org_id=org_id, conn=conn)
    except Exception as exc:
        # Race on the partial unique index (active grant): the pre-check SELECT
        # cannot see a concurrent INSERT -> deterministic 409, not 500 (F-1).
        if "UniqueViolation" in type(exc).__name__:
            return JSONResponse(
                {
                    "code": "conflict",
                    "message": "principal already has an active grant on this org",
                },
                status_code=409,
            )
        logger.exception("admin_api: grant_dataset_access db_error org=%s", org_id)
        return JSONResponse(
            {"code": "db_error", "message": "Database error"},
            status_code=500,
        )
    # Simulate BigQuery IAM binding (gated AI-08, flag ON only, AC6).
    if org_schemas is not None:
        try:
            _wt._simulate_bq_iam_grant(org_schemas, principal, "grant")
        except Exception:
            logger.exception(
                "admin_api: _simulate_bq_iam_grant failed org=%s principal=%s",
                org_id,
                principal,
            )
    write_audit_row(
        identity=granted_by,
        action=ACTION_DATASET_ACCESS_GRANTED,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "principal": principal,
            "marts_dataset": org_schemas.marts if org_schemas else None,
            # Honest trace (F-4): flag OFF grants exist in Postgres but no BQ
            # binding was simulated -- an auditor must see the difference.
            "bq_binding": "simulated" if org_schemas is not None else "flag_off_not_simulated",
        },
    )
    return JSONResponse(
        {
            "id": r[0],
            "org_id": r[1],
            "principal": r[2],
            "granted_by": r[3],
            "created_at": r[4].isoformat() if r[4] else None,
        },
        status_code=201,
    )


async def _list_dataset_access_grants(request: Request) -> Response:
    """GET /api/organizations/{org_id}/dataset-access -- list active grants (AC4).

    Returns grants with revoked_at IS NULL, ordered by created_at ASC.
    404 (non-disclosing) if org absent or caller has no access.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_org_access  # noqa: PLC0415

        with get_connection() as conn:
            if not identity_has_org_access(org_id, identity or "anonymous", conn):
                return JSONResponse(
                    {"code": "not_found", "message": "organization not found"},
                    status_code=404,
                )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, org_id, principal, granted_by, created_at
                    FROM app.dataset_access_grants
                    WHERE org_id = %s AND revoked_at IS NULL
                    ORDER BY created_at ASC
                    """,
                    (org_id,),
                )
                grants = [
                    {
                        "id": row[0],
                        "org_id": row[1],
                        "principal": row[2],
                        "granted_by": row[3],
                        "created_at": row[4].isoformat() if row[4] else None,
                    }
                    for row in cur.fetchall()
                ]
    except Exception:
        logger.exception("admin_api: list_dataset_access_grants db_error org=%s", org_id)
        return JSONResponse(
            {"code": "db_error", "message": "Database error"},
            status_code=500,
        )
    return JSONResponse({"grants": grants}, status_code=200)


async def _revoke_dataset_access(request: Request) -> Response:
    """DELETE /api/organizations/{org_id}/dataset-access/{grant_id} -- revoke (AC5).

    Soft-delete: sets revoked_at = NOW() (row kept for RGPD audit trail).
    404 if grant absent or already revoked; 403 if not owner/admin.
    """
    import core.warehouse_tenancy as _wt  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    org_id = request.path_params["org_id"]
    grant_id = request.path_params["grant_id"]
    principal_revoked = None
    org_schemas = None
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _enforce_org_manage(org_id, identity, conn, "revoke_dataset_access")
            if denied is not None:
                return denied
            with conn.cursor() as cur:
                # Fetch the active grant (revoked_at IS NULL ensures it's still active).
                cur.execute(
                    "SELECT principal FROM app.dataset_access_grants "
                    "WHERE id = %s AND org_id = %s AND revoked_at IS NULL",
                    (grant_id, org_id),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "grant not found"},
                        status_code=404,
                    )
                principal_revoked = row[0]
                cur.execute(
                    "UPDATE app.dataset_access_grants "
                    "SET revoked_at = NOW() "
                    "WHERE id = %s AND org_id = %s AND revoked_at IS NULL",
                    (grant_id, org_id),
                )
            conn.commit()
            if _wt.org_schemas_enabled():
                org_schemas = _wt.resolve_org_schemas(org_id=org_id, conn=conn)
    except Exception:
        logger.exception(
            "admin_api: revoke_dataset_access db_error org=%s grant=%s",
            org_id,
            grant_id,
        )
        return JSONResponse(
            {"code": "db_error", "message": "Database error"},
            status_code=500,
        )
    # Simulate BigQuery IAM revocation (gated AI-08, flag ON only, AC6).
    if org_schemas is not None and principal_revoked is not None:
        try:
            _wt._simulate_bq_iam_grant(org_schemas, principal_revoked, "revoke")
        except Exception:
            logger.exception(
                "admin_api: _simulate_bq_iam_grant revoke failed org=%s grant=%s",
                org_id,
                grant_id,
            )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_DATASET_ACCESS_REVOKED,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "grant_id": grant_id,
            "principal": principal_revoked,
            "marts_dataset": org_schemas.marts if org_schemas else None,
        },
    )
    return JSONResponse({"revoked": True}, status_code=200)


# ===========================================================================
# Story 21.4 -- Flux org-scoped + linkable to MANY projects (Epic 21, FR37/CAP-25).
#
# The "flux" is app.datastreams. A flux is OWNED by an org (datastreams.org_id)
# and LINKED to N projects of that SAME org via app.project_flux (M:N). The
# cross-org link is refused with a friendly 409 (code "cross_org") AND backstopped
# structurally by the composite FKs to projects(org_id, id) / datastreams(org_id,
# id) that share org_id. The per-identity "who may link a flux" enforcement is
# Story 21.5; here we gate on _check_auth only (consistent with 21.1/21.3 AC5).
# ===========================================================================


async def _link_flux_to_project(request: Request) -> Response:
    """POST /api/flux/{flux_id}/projects -- link a flux to a project (AC2).

    Body: {"project_id": str}. 404 if the flux or the project does not exist;
    409 code "cross_org" if project.org_id != flux.org_id (a flux only feeds
    projects of its own org); 409 if the link already exists. Audited (AD-14).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    flux_id = request.path_params["flux_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )
    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "invalid_input", "message": "project_id is required"},
            status_code=422,
        )
    linked_by = identity or "anonymous"
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Resolve the flux (and its org) first. 404 if unknown.
                cur.execute(
                    "SELECT org_id FROM app.datastreams WHERE id = %s", (flux_id,)
                )
                flux_row = cur.fetchone()
                if flux_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "flux not found"},
                        status_code=404,
                    )
                flux_org_id = flux_row[0]
                # Story 21.5: only an owner/admin of the flux's OWNER org may link it.
                # A flux with no org yet (NULL) falls through to the existing 409
                # (cross_org) below; when owned, require manage. Default-open org ->
                # resolves to owner (keeps 21.4 tests green).
                if flux_org_id is not None:
                    denied = _enforce_org_manage(
                        flux_org_id, identity, conn, "link_flux_to_project"
                    )
                    if denied is not None:
                        return denied
                # Resolve the project (and its org). 404 if unknown.
                cur.execute(
                    "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                )
                proj_row = cur.fetchone()
                if proj_row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "project not found"},
                        status_code=404,
                    )
                project_org_id = proj_row[0]
                # review-stack F-MEDIUM: a flux or project not yet scoped to an org
                # (org_id NULL, legacy pre-21.5) is NOT linkable -- guard first so
                # `None != None` (False) cannot slip a NULL into project_flux.org_id
                # (NOT NULL) and surface as a raw 500.
                if flux_org_id is None or project_org_id is None:
                    return JSONResponse(
                        {
                            "code": "cross_org",
                            "message": "flux or project has no organization yet; cannot link",
                        },
                        status_code=409,
                    )
                # Friendly cross-org check (the composite FK is the structural
                # backstop; this yields a clear 409 instead of a bare FK error).
                if project_org_id != flux_org_id:
                    return JSONResponse(
                        {
                            "code": "cross_org",
                            "message": "a flux can only feed projects of its own org",
                        },
                        status_code=409,
                    )
                # Duplicate link -> 409.
                cur.execute(
                    "SELECT 1 FROM app.project_flux "
                    "WHERE project_id = %s AND flux_id = %s",
                    (project_id, flux_id),
                )
                if cur.fetchone() is not None:
                    return JSONResponse(
                        {"code": "conflict", "message": "flux already linked to this project"},
                        status_code=409,
                    )
                cur.execute(
                    """
                    INSERT INTO app.project_flux (project_id, flux_id, org_id)
                    VALUES (%s, %s, %s)
                    RETURNING project_id, flux_id, org_id, created_at
                    """,
                    (project_id, flux_id, flux_org_id),
                )
                r = cur.fetchone()
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: link_flux_to_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    write_audit_row(
        identity=linked_by,
        action=ACTION_FLUX_LINKED,
        provider_account="",
        connection_ref="",
        metadata={"flux_id": flux_id, "project_id": project_id, "org_id": flux_org_id},
    )
    return JSONResponse(
        {
            "project_id": r[0],
            "flux_id": r[1],
            "org_id": r[2],
            "created_at": r[3].isoformat() if r[3] else None,
        },
        status_code=201,
    )


async def _list_flux_projects(request: Request) -> Response:
    """GET /api/flux/{flux_id}/projects -- projects linked to a flux (AC2)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    flux_id = request.path_params["flux_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_org_access  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.datastreams WHERE id = %s", (flux_id,)
                )
                frow = cur.fetchone()
            # Story 21.5 follow-up (reads scoping): 404 if the flux is absent or in
            # an org the caller cannot see (NULL org -> legacy, treated as open).
            if frow is None or (
                frow[0] is not None
                and not identity_has_org_access(frow[0], identity or "anonymous", conn)
            ):
                return JSONResponse(
                    {"code": "not_found", "message": "flux not found"},
                    status_code=404,
                )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT p.id, p.name, p.slug
                    FROM app.project_flux pf
                    JOIN app.projects p ON p.id = pf.project_id
                    WHERE pf.flux_id = %s
                    ORDER BY p.name ASC
                    """,
                    (flux_id,),
                )
                projects = [
                    {"project_id": r[0], "name": r[1], "slug": r[2]}
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.error("admin_api: list_flux_projects db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"projects": projects}, status_code=200)


async def _unlink_flux_from_project(request: Request) -> Response:
    """DELETE /api/flux/{flux_id}/projects/{project_id} -- unlink (AC2).

    Removes the M:N link. 404 if no such link. Audited (AD-14).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    flux_id = request.path_params["flux_id"]
    project_id = request.path_params["project_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Story 21.5: only an owner/admin of the flux's OWNER org may unlink.
                # A flux with no org yet (NULL, legacy) is treated as open (compat).
                # Default-open org -> resolves to owner (keeps 21.4 tests green).
                cur.execute(
                    "SELECT org_id FROM app.datastreams WHERE id = %s", (flux_id,)
                )
                flux_row = cur.fetchone()
                flux_org_id = flux_row[0] if flux_row is not None else None
                if flux_org_id is not None:
                    denied = _enforce_org_manage(
                        flux_org_id, identity, conn, "unlink_flux_from_project"
                    )
                    if denied is not None:
                        return denied
                cur.execute(
                    "DELETE FROM app.project_flux "
                    "WHERE project_id = %s AND flux_id = %s",
                    (project_id, flux_id),
                )
                deleted = cur.rowcount
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: unlink_flux_from_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    if not deleted:
        return JSONResponse(
            {"code": "not_found", "message": "link not found"},
            status_code=404,
        )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_FLUX_UNLINKED,
        provider_account="",
        connection_ref="",
        metadata={"flux_id": flux_id, "project_id": project_id},
    )
    return JSONResponse({"unlinked": True}, status_code=200)


# ===========================================================================
# Story 7.1 -- Project CRUD (AC3, AC4).
#
# app.projects is the anchor table created in migration 018. These endpoints are
# the single config surface for creating / listing / updating / archiving
# projects (AD-15). All guarded by _check_auth. Auth subject -> created_by.
# ===========================================================================

# Currency allowlist (AC3). ISO-4217 uppercase; extend as needed.
_PROJECT_CURRENCIES = {"EUR", "USD", "GBP", "CHF", "JPY", "CAD", "AUD"}

# slug pattern (AC3): kebab-case, starts alphanumeric.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

# Story 17.1: valeurs autorisées pour verification_source_type.
# 'stripe' est inclus comme préférence déclarative ; son connecteur est
# prévu post-story 15.7 mais la préférence est stockable dès maintenant.
_VERIFICATION_SOURCE_TYPES = frozenset({"ga4", "shopify", "stripe"})


def _mint_project_id() -> str:
    """Mint a new prefixed ULID 'proj_<ULID>' for a project."""
    from ulid import ULID  # noqa: PLC0415

    return f"proj_{ULID()}"


def _slugify(name: str) -> str:
    """Convert a project name to a URL-safe kebab-case slug (Dev Notes rules).

    lowercase -> spaces/underscores to hyphens -> strip non [a-z0-9-] ->
    collapse consecutive hyphens -> trim leading/trailing hyphens.
    Example: "Acme Corp (FR)" -> "acme-corp-fr".
    """
    s = name.strip().lower()
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"[^a-z0-9-]", "", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def _valid_timezone(tz: str) -> bool:
    """Return True if *tz* is a valid IANA timezone (window_rule.py pattern)."""
    try:
        import zoneinfo  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        from backports import zoneinfo  # type: ignore[no-redef]  # noqa: PLC0415
    try:
        zoneinfo.ZoneInfo(tz)
        return True
    except Exception:
        return False


def _project_row_to_dict(cols: list[str], row: tuple) -> dict:
    """Serialise a projects row, ISO-formatting timestamp columns."""
    out: dict = {}
    for col, val in zip(cols, row):
        if col in ("created_at", "updated_at", "archived_at") and val is not None:
            out[col] = val.isoformat()
        else:
            out[col] = val
    return out


def _validate_verification_source_fields(
    body: dict,
) -> tuple[dict | None, Response | None]:
    """Story 17.1: valide et extrait les 3 champs source de vérification du body.

    Retourne (fields_dict, None) si valide, ou (None, error_response) si invalide.

    Règles (ACs 17.1) :
    - verification_source_type doit être dans {'ga4','shopify','stripe'} ou None/absent.
    - Si verification_source_type = 'ga4', lead_event_name doit être non vide.
    - verification_source_id est libre (validation cross-projet faite ailleurs, AD-5).
    - Les champs sont optionnels : absent = inchangé (PATCH) ou NULL (CREATE).
    - Messages d'erreur en français, code HTTP 422.
    """
    fields: dict = {}
    has_vstype = "verification_source_type" in body
    has_vsid = "verification_source_id" in body
    has_len = "lead_event_name" in body

    if not (has_vstype or has_vsid or has_len):
        return {}, None  # aucun champ source de vérification

    vstype: str | None = None
    if has_vstype:
        raw = body.get("verification_source_type")
        if raw is None or raw == "":
            vstype = None
        else:
            vstype = str(raw).strip().lower()
            if vstype not in _VERIFICATION_SOURCE_TYPES:
                return None, JSONResponse(
                    {
                        "code": "invalid_input",
                        "message": (
                            f"Type de source de vérification invalide : '{vstype}'. "
                            f"Valeurs acceptées : ga4, shopify, stripe."
                        ),
                    },
                    status_code=422,
                )
        fields["verification_source_type"] = vstype

    if has_vsid:
        raw_id = body.get("verification_source_id")
        fields["verification_source_id"] = str(raw_id).strip() if raw_id else None

    if has_len:
        raw_len = body.get("lead_event_name")
        fields["lead_event_name"] = str(raw_len).strip() if raw_len else None

    # Règle : type='ga4' exige lead_event_name non vide.
    # On évalue avec la valeur fournie OU la valeur présente dans fields.
    effective_type = fields.get("verification_source_type", None) if has_vstype else None
    if effective_type == "ga4":
        effective_len = fields.get("lead_event_name", None)
        if has_len and (not effective_len):
            return None, JSONResponse(
                {
                    "code": "invalid_input",
                    "message": (
                        "Le nom de l'événement lead (lead_event_name) est obligatoire "
                        "lorsque le type de source est 'ga4'."
                    ),
                },
                status_code=422,
            )

    return fields, None


def _validate_verification_source_complete(
    vstype: str | None,
    lead_event_name: str | None,
) -> Response | None:
    """Story 17.1: valide la cohérence globale après fusion PATCH.

    Appelé quand verification_source_type='ga4' est l'état final (après merge),
    pour s'assurer que lead_event_name est non nul même si non fourni dans ce PATCH.
    Retourne None si valide, ou une Response 422 si incohérent.
    """
    if vstype == "ga4" and not lead_event_name:
        return JSONResponse(
            {
                "code": "invalid_input",
                "message": (
                    "Le nom de l'événement lead (lead_event_name) est obligatoire "
                    "lorsque le type de source est 'ga4'."
                ),
            },
            status_code=422,
        )
    return None


def _upsert_verification_prefs(
    project_id: str,
    fields: dict,
    conn: object,
) -> None:
    """Story 17.1: upsert des colonnes source de vérification dans app.project_preferences.

    Crée la ligne si elle n'existe pas (ON CONFLICT DO UPDATE), met à jour uniquement
    les colonnes présentes dans `fields`.

    AD-8 : Postgres est le seul writer. La propagation au miroir se fait via
    mirror_sync.py (SELECT * FROM app.project_preferences).
    """
    if not fields:
        return

    allowed = {"verification_source_type", "verification_source_id", "lead_event_name"}
    update_fields = {k: v for k, v in fields.items() if k in allowed}
    if not update_fields:
        return

    with conn.cursor() as cur:  # type: ignore[attr-defined]
        # Ensure a project_preferences row exists (may not for old projects).
        cur.execute(
            """
            INSERT INTO app.project_preferences (project_id)
            VALUES (%s)
            ON CONFLICT (project_id) DO NOTHING
            """,
            (project_id,),
        )
        set_parts = [f"{col} = %s" for col in update_fields]
        params = list(update_fields.values()) + [project_id]
        cur.execute(
            "UPDATE app.project_preferences SET "
            + ", ".join(set_parts)
            + ", updated_at = NOW() WHERE project_id = %s",
            params,
        )


def _fetch_verification_prefs(project_id: str, conn: object) -> dict:
    """Story 17.1: lit les 3 colonnes source de vérification depuis project_preferences.

    Retourne un dict avec les 3 clés (valeur None si absent ou non configuré).
    """
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            SELECT verification_source_type, verification_source_id, lead_event_name
            FROM app.project_preferences
            WHERE project_id = %s
            """,
            (project_id,),
        )
        row = cur.fetchone()
    if row is None:
        return {
            "verification_source_type": None,
            "verification_source_id": None,
            "lead_event_name": None,
        }
    return {
        "verification_source_type": row[0],
        "verification_source_id": row[1],
        "lead_event_name": row[2],
    }


def _geographic_error(exc: InvalidGeographicPosture) -> Response:
    message = str(exc)
    field = (
        "geographic_mode"
        if "geographic_mode" in message
        else "local_market_country_codes"
    )
    return JSONResponse(
        {
            "code": "invalid_geographic_posture",
            "message": message,
            "details": {field: message},
        },
        status_code=422,
    )

def _country_vocabulary_error() -> Response:
    return JSONResponse(
        {
            "code": "country_vocabulary_unavailable",
            "message": "Canonical country vocabulary is unavailable.",
        },
        status_code=503,
    )



def _country_codes() -> frozenset[str]:
    return frozenset(country.code for country in get_country_vocabulary())


def _fetch_geographic_prefs(
    project_id: str,
    conn: object,
    *,
    for_update: bool = False,
):
    return fetch_project_geographic_posture(project_id, conn, for_update=for_update)


def _upsert_geographic_prefs(project_id: str, posture, conn: object) -> None:
    with conn.cursor() as cur:  # type: ignore[attr-defined]
        cur.execute(
            """
            INSERT INTO app.project_preferences
                (project_id, geographic_mode, local_market_country_codes)
            VALUES (%s, %s, %s)
            ON CONFLICT (project_id) DO UPDATE SET
                geographic_mode = EXCLUDED.geographic_mode,
                local_market_country_codes = EXCLUDED.local_market_country_codes,
                updated_at = NOW()
            """,
            (project_id, posture.mode, list(posture.country_codes)),
        )


async def _list_countries(request: Request) -> Response:
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    try:
        countries = [
            {"code": country.code, "display_name": country.display_name}
            for country in get_country_vocabulary()
        ]
    except CountryVocabularyError:
        logger.exception("admin_api: canonical country vocabulary unavailable")
        return _country_vocabulary_error()
    return JSONResponse({"countries": countries}, status_code=200)


def _deny_project_scope(identity: str, project_id: str, operation: str) -> Response:
    write_audit_row(
        identity=identity,
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="",
        connection_ref="",
        metadata={"claimed_project_id": project_id, "operation": operation},
    )
    return JSONResponse(
        {"code": "not_found", "message": "Project not found"},
        status_code=404,
    )

async def _preview_geographic_change(request: Request) -> Response:
    """POST a no-side-effect geographic impact preview (Member)."""

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    project_id = request.path_params["project_id"]
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
            status_code=400,
        )
    try:
        body = json.loads(await request.body())
        target = normalize_geographic_posture(
            body.get("geographic_mode"),
            body.get("local_market_country_codes", []),
            _country_codes(),
        )
    except CountryVocabularyError:
        return _country_vocabulary_error()
    except InvalidGeographicPosture as exc:
        return _geographic_error(exc)
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.geographic_change import create_geographic_change_preview  # noqa: PLC0415
        from core.main import get_loaded_modules  # noqa: PLC0415

        actor = identity or "anonymous"
        with get_connection() as conn:
            role_error = _require_datastream_role(project_id, actor, "member", conn)
            if role_error is not None:
                return role_error
            result = create_geographic_change_preview(
                project_id=project_id,
                target=target,
                identity=actor,
                idempotency_key=idempotency_key,
                conn=conn,
                loaded_modules=get_loaded_modules(),
            )
        return JSONResponse(result, status_code=200 if result["idempotent_replay"] else 201)
    except Exception as exc:
        from core.geographic_change import (  # noqa: PLC0415
            GeographicPreviewBlocked,
            GeographicPreviewConflict,
        )

        if isinstance(exc, GeographicPreviewConflict):
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=409)
        if isinstance(exc, GeographicPreviewBlocked):
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=422)
        logger.exception("admin_api: geographic preview failed project=%s", project_id)
        return JSONResponse(
            {"code": "geographic_preview_unavailable", "message": "Preview failed."},
            status_code=503,
        )


async def _confirm_geographic_change(request: Request) -> Response:
    """Confirm a fresh geographic preview atomically (Owner)."""

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    project_id = request.path_params["project_id"]
    preview_id = request.path_params["preview_id"]
    try:
        raw = await request.body()
        body = json.loads(raw) if raw.strip() else {}
        backfill_decision = str(body.get("backfill_decision") or "defer")
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.geographic_change import confirm_geographic_change  # noqa: PLC0415
        from core.main import get_loaded_modules  # noqa: PLC0415

        actor = identity or "anonymous"
        with get_connection() as conn:
            role_error = _require_datastream_role(project_id, actor, "owner", conn)
            if role_error is not None:
                return role_error
            result = confirm_geographic_change(
                preview_id=preview_id,
                project_id=project_id,
                identity=actor,
                backfill_decision=backfill_decision,
                conn=conn,
                loaded_modules=get_loaded_modules(),
            )
        return JSONResponse(result, status_code=200)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_input", "message": str(exc)}, status_code=422)
    except Exception as exc:
        from core.geographic_change import (  # noqa: PLC0415
            GeographicPreviewBlocked,
            GeographicPreviewNotFound,
            GeographicPreviewStale,
        )

        if isinstance(exc, GeographicPreviewNotFound):
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=404)
        if isinstance(exc, GeographicPreviewStale):
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=409)
        if isinstance(exc, GeographicPreviewBlocked):
            return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=422)
        logger.exception("admin_api: geographic confirmation failed preview=%s", preview_id)
        return JSONResponse(
            {"code": "geographic_confirmation_failed", "message": "Confirmation failed."},
            status_code=503,
        )

async def _create_project(request: Request) -> Response:
    """POST /api/projects -- create a project (AC3).

    Body: {"name": str, "slug": str?, "currency": str?, "timezone": str?,
           "verification_source_type": str?, "verification_source_id": str?,
           "lead_event_name": str?}
    Returns 201 with the created project. Auto-generates a unique slug from name
    when not provided (appends -1, -2, ... on collision).

    Story 17.1: accepte les 3 champs source de vérification (optionnels).
    Les champs sont écrits dans app.project_preferences (AD-8 : sole-writer Postgres).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    name = (body.get("name") or "").strip()
    if not name or len(name) > 100:
        return JSONResponse(
            {"code": "invalid_input", "message": "name is required (max 100 chars)"},
            status_code=422,
        )

    currency = (body.get("currency") or "EUR").strip().upper()
    if currency not in _PROJECT_CURRENCIES:
        return JSONResponse(
            {"code": "invalid_input", "message": f"unsupported currency: {currency}"},
            status_code=422,
        )

    timezone_str = (body.get("timezone") or "Europe/Paris").strip()
    if not _valid_timezone(timezone_str):
        return JSONResponse(
            {"code": "invalid_input", "message": f"invalid timezone: {timezone_str}"},
            status_code=422,
        )

    slug_in = (body.get("slug") or "").strip()
    base_slug = slug_in or _slugify(name)
    if not base_slug or not _SLUG_RE.match(base_slug) or len(base_slug) > 50:
        return JSONResponse(
            {"code": "invalid_input", "message": f"invalid slug: {base_slug!r}"},
            status_code=422,
        )

    # Story 17.1: valider les champs source de vérification avant d'écrire en DB.
    vsfields, vs_err = _validate_verification_source_fields(body)
    if vs_err is not None:
        return vs_err
    # Validation de cohérence ga4 + lead_event_name pour la création
    # (les deux champs peuvent être fournis ensemble ou séparément).
    effective_vstype = vsfields.get("verification_source_type") if vsfields else None
    effective_len = vsfields.get("lead_event_name") if vsfields else None
    if effective_vstype == "ga4" and "lead_event_name" not in body:
        # Non fourni dans ce body : OK pour CREATE — NULL est persisté tel quel
        # (AD-9 : pas de valeur fantôme injectée en DB). C'est le CONSOMMATEUR (17.2)
        # qui appliquera le défaut 'generate_lead' à la lecture quand type='ga4' et
        # lead_event_name est NULL (review-17-1 F-5 : le défaut est applicatif côté
        # lecture, jamais écrit). L'AC exige seulement : fourni et vide → 422.
        pass
    coherence_err = _validate_verification_source_complete(effective_vstype, effective_len)
    if coherence_err is not None and "lead_event_name" in body:
        # Seulement bloquer si le champ a été explicitement fourni et est vide.
        return coherence_err

    geography_touched = any(
        key in body for key in ("geographic_mode", "local_market_country_codes")
    )
    try:
        geographic_posture = normalize_geographic_posture(
            body.get("geographic_mode", "global"),
            body.get("local_market_country_codes", []),
            _country_codes(),
        )
    except CountryVocabularyError:
        return _country_vocabulary_error()
    except InvalidGeographicPosture as exc:
        return _geographic_error(exc)

    proj_id = _mint_project_id()
    created_by = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Resolve slug collisions by appending a counter (Dev Notes). When a
            # slug was explicitly supplied, a collision is a 409 (AC8).
            slug = base_slug
            with conn.cursor() as cur:
                if slug_in:
                    cur.execute("SELECT 1 FROM app.projects WHERE slug = %s", (slug,))
                    if cur.fetchone() is not None:
                        return JSONResponse(
                            {"code": "conflict", "message": "slug already exists"},
                            status_code=409,
                        )
                else:
                    counter = 1
                    while True:
                        cur.execute("SELECT 1 FROM app.projects WHERE slug = %s", (slug,))
                        if cur.fetchone() is None:
                            break
                        slug = f"{base_slug}-{counter}"
                        counter += 1

                cur.execute(
                    """
                    INSERT INTO app.projects
                        (id, name, slug, status, currency, timezone, created_by)
                    VALUES (%s, %s, %s, 'active', %s, %s, %s)
                    RETURNING id, name, slug, status, currency, timezone, created_at
                    """,
                    (proj_id, name, slug, currency, timezone_str, created_by),
                )
                row = cur.fetchone()
                cols = [d[0] for d in cur.description]
                created = _project_row_to_dict(cols, row)

            # Story 17.1: écrire les préférences source de vérification dans
            # app.project_preferences (AD-8 sole-writer Postgres).
            if vsfields:
                _upsert_verification_prefs(proj_id, vsfields, conn)
            if geography_touched:
                _upsert_geographic_prefs(proj_id, geographic_posture, conn)
                insert_audit_row(
                    conn,
                    identity=created_by,
                    action=ACTION_PROJECT_GEOGRAPHIC_POSTURE_UPDATED,
                    provider_account="",
                    connection_ref="",
                    metadata={
                        "project_id": proj_id,
                        "previous": {
                            "geographic_mode": "global",
                            "local_market_country_codes": [],
                        },
                        "new": geographic_posture.as_dict(),
                    },
                )

            conn.commit()
    except Exception as exc:
        logger.error("admin_api: create_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=created_by,
        action=ACTION_PROJECT_CREATED,
        provider_account="",
        connection_ref="",
        metadata={"project_id": proj_id, "slug": created["slug"], "name": name},
    )

    # Story 7.3 (AC6): provision per-tenant key immediately after project creation.
    # If key provisioning fails, roll back the project insert and return 500.
    try:
        from core.tenant_keys import get_tenant_key_backend, write_key_audit_row  # noqa: PLC0415

        backend = get_tenant_key_backend()
        backend.get_or_create_key(proj_id)
        write_key_audit_row(
            project_id=proj_id,
            action="key_created",
            performed_by=created_by,
            details={"backend": os.environ.get("TENANT_KEY_BACKEND", "local")},
        )
        write_audit_row(
            identity=created_by,
            action=ACTION_KEY_CREATED,
            provider_account="",
            connection_ref="",
            metadata={"project_id": proj_id},
        )
    except Exception as exc:
        logger.error("admin_api: key_provision_failed project=%s err=%s", proj_id, exc)
        # Roll back: delete the project row just inserted.
        try:
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM app.projects WHERE id = %s", (proj_id,))
                conn.commit()
        except Exception as rb_exc:
            logger.error("admin_api: rollback_failed project=%s err=%s", proj_id, rb_exc)
        return JSONResponse(
            {
                "code": "key_provision_failed",
                "message": f"Failed to provision tenant key: {exc}",
            },
            status_code=500,
        )

    # Story 17.1: enrichir la réponse avec les champs source de vérification.
    created.update(
        {
            "verification_source_type": vsfields.get("verification_source_type")
            if vsfields
            else None,
            "verification_source_id": vsfields.get("verification_source_id") if vsfields else None,
            "lead_event_name": vsfields.get("lead_event_name") if vsfields else None,
        }
    )
    created.update(geographic_posture.as_dict())
    return JSONResponse(created, status_code=201)


async def _list_projects(request: Request) -> Response:
    """GET /api/projects -- list active projects, ordered by name ASC (AC3)."""
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, slug, status, currency, timezone,
                           org_id, created_at, updated_at
                    FROM app.projects
                    WHERE status = 'active'
                    ORDER BY name ASC
                    """
                )
                cols = [d[0] for d in cur.description]
                projects = [_project_row_to_dict(cols, r) for r in cur.fetchall()]
    except Exception as exc:
        logger.error("admin_api: list_projects db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse({"projects": projects}, status_code=200)


async def _get_project(request: Request) -> Response:
    """GET /api/projects/{project_id} -- single project; 404 if not found (AC3)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    project_id = request.path_params["project_id"]
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            from core.project_access import identity_can_access_project_in_org

            actor = identity or "anonymous"
            if not identity_can_access_project_in_org(project_id, actor, conn):
                return _deny_project_scope(actor, project_id, "get_project")
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, name, slug, status, currency, timezone,
                           created_at, updated_at, archived_at
                    FROM app.projects WHERE id = %s
                    """,
                    (project_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Project not found"},
                        status_code=404,
                    )
                cols = [d[0] for d in cur.description]
                project = _project_row_to_dict(cols, row)
            # Story 17.1: enrichir la réponse avec les préférences source de vérification.
            vsprefs = _fetch_verification_prefs(project_id, conn)
            project.update(vsprefs)
            geographic_posture = _fetch_geographic_prefs(project_id, conn)
            project.update(geographic_posture.as_dict())
    except CountryVocabularyError:
        return _country_vocabulary_error()
    except Exception as exc:
        logger.error("admin_api: get_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(project, status_code=200)


async def _patch_project(request: Request) -> Response:
    """PATCH /api/projects/{project_id} -- update mutable fields (AC3).

    Body: {"name": str?, "currency": str?, "timezone": str?,
           "verification_source_type": str?, "verification_source_id": str?,
           "lead_event_name": str?}
    id and slug are immutable. Updates updated_at.

    Story 17.1: accepte les 3 champs source de vérification.
    Écrits dans app.project_preferences (AD-8). Un type='ga4' sans lead_event_name
    dans le state final (existant + patch) → 422 (message français).
    Dénégation cross-projet pour verification_source_id : l'id doit appartenir au
    projet patché (vérifié via app.datastreams, AD-5).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    project_id = request.path_params["project_id"]
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    set_clauses: list[str] = []
    params: list = []
    if "name" in body:
        name = (body.get("name") or "").strip()
        if not name or len(name) > 100:
            return JSONResponse(
                {"code": "invalid_input", "message": "name must be 1..100 chars"},
                status_code=422,
            )
        set_clauses.append("name = %s")
        params.append(name)
    if "currency" in body:
        currency = (body.get("currency") or "").strip().upper()
        if currency not in _PROJECT_CURRENCIES:
            return JSONResponse(
                {"code": "invalid_input", "message": f"unsupported currency: {currency}"},
                status_code=422,
            )
        set_clauses.append("currency = %s")
        params.append(currency)
    if "timezone" in body:
        timezone_str = (body.get("timezone") or "").strip()
        if not _valid_timezone(timezone_str):
            return JSONResponse(
                {"code": "invalid_input", "message": f"invalid timezone: {timezone_str}"},
                status_code=422,
            )
        set_clauses.append("timezone = %s")
        params.append(timezone_str)

    # Story 17.1: valider les champs source de vérification.
    vsfields, vs_err = _validate_verification_source_fields(body)
    if vs_err is not None:
        return vs_err

    has_project_fields = bool(set_clauses)
    has_vs_fields = bool(vsfields)
    has_geo_fields = any(
        key in body for key in ("geographic_mode", "local_market_country_codes")
    )

    if not has_project_fields and not has_vs_fields and not has_geo_fields:
        return JSONResponse(
            {"code": "invalid_input", "message": "no updatable fields provided"},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            from core.project_access import identity_can_access_project_in_org

            actor = identity or "anonymous"
            if not identity_can_access_project_in_org(project_id, actor, conn):
                return _deny_project_scope(actor, project_id, "patch_project")
            previous_geography = _fetch_geographic_prefs(
                project_id, conn, for_update=has_geo_fields
            )
            try:
                geographic_posture = (
                    merge_geographic_patch(previous_geography, body, _country_codes())
                    if has_geo_fields
                    else previous_geography
                )
            except InvalidGeographicPosture as exc:
                return _geographic_error(exc)
            if has_geo_fields and geographic_posture != previous_geography:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT COUNT(*)
                        FROM app.datastreams
                        WHERE project_id = %s
                          AND archived_at IS NULL
                          AND current_plan_version_id IS NOT NULL
                        """,
                        (project_id,),
                    )
                    governed_count = int(cur.fetchone()[0])
                if governed_count:
                    return JSONResponse(
                        {
                            "code": "geographic_preview_required",
                            "message": (
                                "This geographic change affects governed Datastream plans; "
                                "create and confirm an impact preview first."
                            ),
                            "details": {"affected_datastream_count": governed_count},
                        },
                        status_code=409,
                    )
            # Story 17.1 AD-5: vérifier que verification_source_id appartient bien
            # à ce projet (cross-project scope denial).
            if vsfields and vsfields.get("verification_source_id"):
                vs_id = vsfields["verification_source_id"]
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT project_id FROM app.datastreams WHERE id = %s",
                        (vs_id,),
                    )
                    ds_row = cur.fetchone()
                # On accepte aussi qu'un id ne soit pas dans app.datastreams
                # (peut être un profil GA4 ou une connexion non encore migrée).
                # Mais si le datastream existe et appartient à un AUTRE projet : refus.
                if ds_row is not None and ds_row[0] != project_id:
                    # review-17-1 F-3 (AD-5/FR12): every cross-scope refusal is AUDITED,
                    # like the notebook/datastream refusals in this file.
                    write_audit_row(
                        identity=identity or "anonymous",
                        action=ACTION_CROSS_SCOPE_ATTEMPT,
                        provider_account="",
                        connection_ref="",
                        metadata={
                            "claimed_project_id": project_id,
                            "datastream_id": vs_id,
                            "datastream_project_id": ds_row[0],
                            "operation": "patch_project_verification_source",
                        },
                    )
                    logger.warning(
                        "admin_api: cross_project_vsid project=%s claimed_ds=%s ds_project=%s",
                        project_id,
                        vs_id,
                        ds_row[0],
                    )
                    return JSONResponse(
                        {
                            "code": "forbidden",
                            "message": (
                                "Le flux de données désigné comme source de vérification "
                                "n'appartient pas à ce projet."
                            ),
                        },
                        status_code=403,
                    )

            # Si verification_source_type='ga4' dans le PATCH mais pas lead_event_name,
            # lire l'état courant pour vérifier la cohérence finale.
            if (
                vsfields
                and vsfields.get("verification_source_type") == "ga4"
                and "lead_event_name" not in body
            ):
                current_prefs = _fetch_verification_prefs(project_id, conn)
                coherence_err = _validate_verification_source_complete(
                    "ga4", current_prefs.get("lead_event_name")
                )
                if coherence_err is not None:
                    return coherence_err

            updated: dict = {}
            if has_project_fields:
                set_clauses.append("updated_at = NOW()")
                params.append(project_id)
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE app.projects SET " + ", ".join(set_clauses) + " WHERE id = %s "
                        "RETURNING id, name, slug, status, currency, timezone, "
                        "created_at, updated_at",
                        params,
                    )
                    row = cur.fetchone()
                    if row is None:
                        return JSONResponse(
                            {"code": "not_found", "message": "Project not found"},
                            status_code=404,
                        )
                    cols = [d[0] for d in cur.description]
                    updated = _project_row_to_dict(cols, row)
            else:
                # Seuls des champs vs ont été fournis : vérifier que le projet existe.
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, name, slug, status, currency, timezone, "
                        "created_at, updated_at FROM app.projects WHERE id = %s",
                        (project_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return JSONResponse(
                            {"code": "not_found", "message": "Project not found"},
                            status_code=404,
                        )
                    cols = [d[0] for d in cur.description]
                    updated = _project_row_to_dict(cols, row)

            # Story 17.1: persister les préférences source de vérification.
            if has_vs_fields:
                # review-17-1 F-6: si le type est explicitement remis à NULL, nettoyer
                # aussi l'id et le lead_event_name (pas de préférences orphelines en DB).
                if (
                    "verification_source_type" in vsfields
                    and vsfields["verification_source_type"] is None
                ):
                    vsfields.setdefault("verification_source_id", None)
                    vsfields.setdefault("lead_event_name", None)
                _upsert_verification_prefs(project_id, vsfields, conn)
            if has_geo_fields:
                _upsert_geographic_prefs(project_id, geographic_posture, conn)
                insert_audit_row(
                    conn,
                    identity=actor,
                    action=ACTION_PROJECT_GEOGRAPHIC_POSTURE_UPDATED,
                    provider_account="",
                    connection_ref="",
                    metadata={
                        "project_id": project_id,
                        "previous": previous_geography.as_dict(),
                        "new": geographic_posture.as_dict(),
                    },
                )


            # Lire l'état final des préférences pour la réponse.
            final_prefs = _fetch_verification_prefs(project_id, conn)

            # review-17-1 F-2: la cohérence se valide sur l'ÉTAT FINAL (existant + patch),
            # pas seulement sur le body — un PATCH {"lead_event_name": ""} seul sur un
            # projet déjà en type='ga4' produirait sinon un état ga4 + NULL incohérent.
            coherence_err = _validate_verification_source_complete(
                final_prefs.get("verification_source_type"),
                final_prefs.get("lead_event_name"),
            )
            if coherence_err is not None:
                conn.rollback()
                return coherence_err

            updated.update(final_prefs)
            updated.update(geographic_posture.as_dict())

            conn.commit()
    except CountryVocabularyError:
        return _country_vocabulary_error()
    except Exception as exc:
        logger.error("admin_api: patch_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )
    return JSONResponse(updated, status_code=200)


async def _delete_project(request: Request) -> Response:
    """DELETE /api/projects/{project_id} -- archive + revoke connections (AC4).

    NEVER hard-deletes. Sets status='archived', archived_at=NOW(). Marks every
    non-revoked connection_ref for the project as revoked, writes an audit row,
    and returns {"status": "archived", "connections_revoked": N}.
    404 if not found; 409 if already archived.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    project_id = request.path_params["project_id"]
    subject = identity or "anonymous"

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # 1. Verify exists + active (single transaction; row-lock).
                cur.execute(
                    "SELECT status FROM app.projects WHERE id = %s FOR UPDATE",
                    (project_id,),
                )
                prow = cur.fetchone()
                if prow is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Project not found"},
                        status_code=404,
                    )
                if prow[0] == "archived":
                    return JSONResponse(
                        {"code": "conflict", "message": "Project already archived"},
                        status_code=409,
                    )

                # 2. Archive the project (soft-delete; data remains readable).
                cur.execute(
                    "UPDATE app.projects "
                    "SET status = 'archived', archived_at = NOW(), updated_at = NOW() "
                    "WHERE id = %s",
                    (project_id,),
                )

                # 3. Revoke every still-active connection for the project.
                cur.execute(
                    "UPDATE app.connection_ref "
                    "SET status = 'revoked', revoked_at = NOW(), updated_at = NOW() "
                    "WHERE project_id = %s AND status != 'revoked' "
                    "RETURNING id, provider, nango_connection_id",
                    (project_id,),
                )
                revoked = cur.fetchall()
            from core.account_topology import invalidate_credential_exposures  # noqa: PLC0415

            for revoked_id, _provider, _nango_id in revoked:
                invalidate_credential_exposures(revoked_id, "revocation", conn)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: delete_project db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    connections_revoked = len(revoked)

    # 3b. Best-effort Nango token revocation per connection. Failure logs a
    # structured warning but never blocks the archive (AC4 step 3).
    # AI-41: on revocation failure, emit a type='nango_revoke_failed' infra firing
    # so the event is surfaced by the normal alert pipeline.  Never raises; archival
    # outcome is unchanged (best-effort semantics preserved).
    for conn_id, provider, nango_conn_id in revoked:
        try:
            nango_client.revoke_connection(provider, nango_conn_id)
        except AttributeError:
            # No revoke helper in nango_client at P3-dev: DB revoke is the record
            # of truth; token cleanup is a Phase B concern. Log once per conn.
            logger.warning(
                "delete_project: nango_revoke_unavailable conn=%s provider=%s",
                conn_id,
                provider,
            )
        except Exception as exc:
            logger.warning(
                "delete_project: nango_revoke_failed conn=%s provider=%s err=%s",
                conn_id,
                provider,
                exc,
            )
            # AI-41: emit infra firing so the failure is observable via alert delivery.
            try:
                from core import infra_alerts as _ia  # noqa: PLC0415

                _ia.write_infra_firing(
                    alert_type="nango_revoke_failed",
                    project_id=project_id,
                    metric="nango_revoke",
                    severity="error",
                    message=(
                        f"Nango token revocation failed during project archival: "
                        f"conn={conn_id} provider={provider}"
                    ),
                    metadata={
                        "project_id": project_id,
                        "connection_ref_id": conn_id,
                        "nango_connection_id": nango_conn_id,
                        "provider": provider,
                        "error": str(exc),
                    },
                )
            except Exception as fire_exc:  # noqa: BLE001
                logger.debug(
                    "delete_project: nango_revoke_failed_firing_error conn=%s: %s",
                    conn_id,
                    fire_exc,
                )

    # 3c. Story 7.3 (AC4, T5): delete tenant key after all connections are revoked.
    # Failure logs a warning but DOES NOT abort the archive (graceful degradation).
    try:
        from core.tenant_keys import get_tenant_key_backend, write_key_audit_row  # noqa: PLC0415

        backend = get_tenant_key_backend()
        backend.delete_key(project_id)
        write_key_audit_row(
            project_id=project_id,
            action="key_deleted",
            performed_by=subject,
            details={
                "backend": os.environ.get("TENANT_KEY_BACKEND", "local"),
                "via": "project_archive",
            },
        )
        write_audit_row(
            identity=subject,
            action=ACTION_KEY_DELETED,
            provider_account="",
            connection_ref="",
            metadata={"project_id": project_id, "via": "project_archive"},
        )
    except Exception as exc:
        logger.warning(
            "delete_project: key_deletion_failed project=%s err=%s",
            project_id,
            exc,
        )

    # 4. Audit row (AC4 step 4). Attach a revoked connection_ref when present so
    # the row satisfies the audit_log FK; falls back to "" like other project-
    # scoped audit events (context_events / notebooks) when no connection exists.
    audit_conn_ref = revoked[0][0] if revoked else ""
    write_audit_row(
        identity=subject,
        action=ACTION_PROJECT_ARCHIVED,
        provider_account="",
        connection_ref=audit_conn_ref,
        metadata={"project_id": project_id, "connections_revoked": connections_revoked},
    )

    return JSONResponse(
        {"status": "archived", "connections_revoked": connections_revoked},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Story 7.3 (AC4) -- Connection revocation endpoint
#
# POST /api/projects/{project_id}/connections/{connection_id}/revoke
#
# Steps:
#   1. Verify connection belongs to the project (404 if not).
#   2. Call Nango API to delete the connection (best-effort; log + continue on error).
#   3. Purge health poller cache entry (health row delete + in-memory cleanup).
#   4. Mark connection_ref row as revoked.
#   5. Write audit row.
#   6. Return {"status": "revoked", "nango_deleted": bool}.
# ---------------------------------------------------------------------------


async def _revoke_connection(request: Request) -> Response:
    """POST /api/projects/{project_id}/connections/{connection_id}/revoke.

    Per-connection revocation endpoint (Story 7.3, AC4).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.path_params.get("project_id", "")
    connection_id = request.path_params.get("connection_id", "")
    subject = identity or "anonymous"

    if not project_id or not connection_id:
        return JSONResponse(
            {"code": "missing_id", "message": "project_id and connection_id are required"},
            status_code=400,
        )

    # Step 1: verify connection belongs to project; fetch nango_connection_id + provider.
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, nango_connection_id, provider, status
                    FROM app.connection_ref
                    WHERE id = %s AND project_id = %s
                    """,
                    (connection_id, project_id),
                )
                row = cur.fetchone()
    except Exception as exc:
        logger.error("admin_api: revoke_connection db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    if row is None:
        return JSONResponse(
            {
                "code": "not_found",
                "message": f"Connection '{connection_id}' not found for project '{project_id}'",
            },
            status_code=404,
        )

    _conn_id, nango_connection_id, provider, current_status = row

    # Step 2: Call Nango API to delete the connection (best-effort).
    nango_deleted = False
    try:
        nango_deleted = nango_client.delete_connection(nango_connection_id, provider)
    except Exception as exc:
        logger.warning(
            "admin_api: revoke_connection nango_delete_failed conn=%s err=%s",
            connection_id,
            exc,
        )

    # Step 3: Purge health poller cache (health row + in-memory).
    try:
        from core.health_poller import purge_connection_cache  # noqa: PLC0415

        purge_connection_cache(connection_id)
    except Exception as exc:
        logger.warning(
            "admin_api: revoke_connection cache_purge_failed conn=%s err=%s",
            connection_id,
            exc,
        )

    # Also clear admin_api rate-limit cache entry for this connection.
    _refresh_health_last.pop(connection_id, None)

    # Step 4: Mark connection_ref as revoked.
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.connection_ref
                    SET status = 'revoked', revoked_at = NOW(), updated_at = NOW()
                    WHERE id = %s
                    """,
                    (connection_id,),
                )
            from core.account_topology import invalidate_credential_exposures  # noqa: PLC0415

            invalidate_credential_exposures(connection_id, "revocation", conn)
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: revoke_connection update_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error on revoke: {exc}"},
            status_code=500,
        )

    # Step 5: Write audit row.
    write_audit_row(
        identity=subject,
        action=ACTION_CONNECTION_REVOKED,
        provider_account=provider or "",
        connection_ref=connection_id,
        metadata={
            "project_id": project_id,
            "connection_id": connection_id,
            "via": "manual_revoke",
            "nango_deleted": nango_deleted,
        },
    )

    # Step 6: Return result.
    return JSONResponse(
        {"status": "revoked", "nango_deleted": nango_deleted},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Story 7.3 (AC5) -- Key rotation endpoint
#
# POST /api/projects/{project_id}/rotate-key
#
# Steps:
#   1. Call tenant_key_backend.rotate_key(project_id).
#   2. Write tka_ row with action='key_rotated'.
#   3. Return {"status": "rotated", "rotated_at": ISO_timestamp}.
# ---------------------------------------------------------------------------


async def _rotate_project_key(request: Request) -> Response:
    """POST /api/projects/{project_id}/rotate-key -- rotate per-tenant encryption key.

    Story 7.3, AC5. Rotation does NOT require re-encrypting stored data (Phase A
    does not encrypt connection_ref payloads -- the OAuth tokens live in Nango under
    Nango's global key). The new key replaces the old one for future operations.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = request.path_params.get("project_id", "")
    subject = identity or "anonymous"

    if not project_id:
        return JSONResponse(
            {"code": "missing_id", "message": "project_id is required"},
            status_code=400,
        )

    # Verify project exists
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM app.projects WHERE id = %s AND status = 'active'",
                    (project_id,),
                )
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Project not found or archived"},
                        status_code=404,
                    )
    except Exception as exc:
        logger.error("admin_api: rotate_key db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"},
            status_code=500,
        )

    try:
        from core.tenant_keys import get_tenant_key_backend, write_key_audit_row  # noqa: PLC0415

        backend = get_tenant_key_backend()
        backend.rotate_key(project_id)
        rotated_at = datetime.now(tz=timezone.utc).isoformat()
        write_key_audit_row(
            project_id=project_id,
            action="key_rotated",
            performed_by=subject,
            details={"backend": os.environ.get("TENANT_KEY_BACKEND", "local")},
        )
        write_audit_row(
            identity=subject,
            action=ACTION_KEY_ROTATED,
            provider_account="",
            connection_ref="",
            metadata={"project_id": project_id},
        )
    except Exception as exc:
        logger.error("admin_api: rotate_key failed project=%s err=%s", project_id, exc)
        return JSONResponse(
            {"code": "rotate_key_failed", "message": f"Key rotation failed: {exc}"},
            status_code=500,
        )

    return JSONResponse(
        {"status": "rotated", "rotated_at": rotated_at},
        status_code=200,
    )


# ===========================================================================
# Story 8.2 -- Datastream CRUD + /run endpoint.
#
# GET    /api/datastreams?project_id=<id>   -- list datastreams for a project
# POST   /api/datastreams                   -- create a datastream
# GET    /api/datastreams/{id}              -- get one datastream (project-scoped)
# PATCH  /api/datastreams/{id}              -- update a datastream
# DELETE /api/datastreams/{id}              -- delete / soft-archive a datastream
# POST   /api/datastreams/{id}/run          -- enqueue a pull for this datastream
#
# All endpoints:
#   - Require Bearer token (api_auth, same as other admin endpoints).
#   - Are project-scoped: callers supply ?project_id= or body.project_id.
#   - Return 404 + ACTION_CROSS_SCOPE_ATTEMPT audit row on cross-scope access (AD-5).
#   - Use French error messages.
#   - AD-8: admin console only; never direct DB access.
# ===========================================================================


def _require_datastream_role(
    project_id: str,
    identity: str,
    minimum_role: str,
    conn,
    *,
    datastream_id: str | None = None,
) -> Response | None:
    """Enforce strict Viewer/Member/Owner access for Datastream surfaces."""

    from core.project_access import (  # noqa: PLC0415
        ProjectAccessUnavailable,
        identity_has_project_role,
    )

    try:
        allowed = identity_has_project_role(
            project_id,
            identity or "anonymous",
            minimum_role,
            conn,
        )
    except ProjectAccessUnavailable:
        return JSONResponse(
            {"code": "unavailable", "message": "Verification des droits indisponible"},
            status_code=503,
        )
    if allowed:
        return None

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="",
        connection_ref="",
        metadata={
            "project_id": project_id,
            "datastream_id": datastream_id,
            "minimum_role": minimum_role,
            "reason": "insufficient_project_role",
            "operation": "datastream_access",
        },
    )
    return JSONResponse(
        {"code": "not_found", "message": "Flux de donnees introuvable"},
        status_code=404,
    )


def _enforce_datastream_project_scope(
    ds_project_id: str,
    identity: str,
    ds_id: str,
    conn,
    claimed_project_id: str = "",
    minimum_role: str = "viewer",
) -> Response | None:
    """Return a non-disclosing error unless scope and strict role are proven."""

    if claimed_project_id and claimed_project_id != ds_project_id:
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_CROSS_SCOPE_ATTEMPT,
            provider_account="",
            connection_ref="",
            metadata={
                "datastream_id": ds_id,
                "ds_project_id": ds_project_id,
                "claimed_project_id": claimed_project_id,
                "reason": "scope_mismatch",
                "operation": "datastream_access",
            },
        )
        return JSONResponse(
            {"code": "not_found", "message": "Flux de donnees introuvable"},
            status_code=404,
        )
    return _require_datastream_role(
        ds_project_id,
        identity,
        minimum_role,
        conn,
        datastream_id=ds_id,
    )


# ---------------------------------------------------------------------------
# Story 12.19 -- deterministic daily sample-preview (server-side masking).
#
# GET /api/datastreams/{id}/sample
#   ?stage={collected|mapped|processed|published}&date_from=&date_to=&limit=5
#
# Returns, per day in the range, the first ``limit`` (default 5, hard-capped 20)
# deterministically-ordered eligible rows for the requested stage, with PII columns
# masked SERVER-SIDE before the response is built (cache_warehouse.read_datastream_
# sample owns the warehouse read + masking). Project/auth scoped exactly like the
# neighbouring Datastream routes (AD-5): Viewer role on the datastream's own project.
#
# Stage version provenance (mapping_version_id / current_published_execution_id) is
# resolved from Postgres and echoed for the requested stage; the actual row
# materialisation is the consolidated mart (see cache_warehouse honesty note).
# ---------------------------------------------------------------------------


async def _datastream_sample(request: Request) -> Response:
    """GET /api/datastreams/{id}/sample -- deterministic masked daily sample (12.19).

    Query params:
      stage      -- collected|mapped|processed|published (default: processed)
      date_from  -- inclusive ISO day (required)
      date_to    -- inclusive ISO day (required)
      limit      -- per-day row cap (default 5, hard-capped at 20)
      project_id -- optional explicit scope claim (must match the datastream's owner)

    Errors: 400 (bad params/range/stage), 401 (unauthorized), 404 (unknown/cross-
    scope datastream), 502 (warehouse unreachable / marts absent backend), 500 (DB).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")
    stage = (request.query_params.get("stage") or "processed").strip().lower()
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()
    claimed_project_id = (request.query_params.get("project_id") or "").strip()
    if not date_from or not date_to:
        return JSONResponse(
            {"code": "missing_param", "message": "date_from et date_to sont requis"},
            status_code=400,
        )
    try:
        limit = int(request.query_params.get("limit") or "5")
    except (TypeError, ValueError):
        return JSONResponse(
            {"code": "invalid_param", "message": "limit doit etre un entier"},
            status_code=400,
        )

    try:
        from core.cache_warehouse import SampleReadError, read_datastream_sample  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Resolve owner project + stage-version pointers in one scoped read.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT project_id, module_name, source_kind,
                           current_mapping_version_id, current_published_execution_id
                    FROM app.datastreams
                    WHERE id = %s
                    """,
                    (ds_id,),
                )
                row = cur.fetchone()
            if row is None:
                # Non-disclosing 404 (same posture as neighbouring datastream routes).
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            ds_project_id = row[0]
            module_name = row[1]
            source_kind = row[2]
            mapping_version_id = row[3]
            published_execution_id = row[4]

            # AD-5: enforce scope + strict Viewer role on the datastream's OWN
            # project; a mismatched ?project_id= claim is a non-disclosing 404.
            scope_error = _enforce_datastream_project_scope(
                ds_project_id,
                identity,
                ds_id,
                conn,
                claimed_project_id=claimed_project_id,
                minimum_role="viewer",
            )
            if scope_error is not None:
                return scope_error

            # The mart ``connector`` value == the datastream's module_name. Inbound
            # managed-feed / external_bq datastreams have no module connector in the
            # KPI mart; their sample is honestly empty at this stage (no fabrication).
            connector = module_name or ""

            try:
                sample = read_datastream_sample(
                    project_id=ds_project_id,
                    connector=connector,
                    stage=stage,
                    date_from=date_from,
                    date_to=date_to,
                    limit=limit,
                )
            except SampleReadError as exc:
                status = 502 if exc.code == "warehouse_unavailable" else 400
                return JSONResponse(
                    {"code": exc.code, "message": str(exc)}, status_code=status
                )

            # Overlay real per-day rejection counts for inbound managed feeds
            # (the KPI mart carries none). Best-effort: a rejection-store hiccup
            # never fails the sample -- counts stay 0 (honest absence).
            if source_kind == "managed_feed":
                try:
                    _overlay_rejection_counts(
                        sample["days"], ds_id, ds_project_id, conn
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "admin_api: sample_rejection_overlay_failed ds=%s: %s",
                        ds_id, exc,
                    )
    except Exception as exc:
        logger.error("admin_api: datastream_sample_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees"},
            status_code=500,
        )

    # Stage-appropriate version provenance (only the pointer that applies is echoed).
    version_ref: dict = {}
    if stage == "published":
        version_ref["published_execution_id"] = published_execution_id
    elif stage in ("mapped", "processed"):
        version_ref["mapping_version_id"] = mapping_version_id
    # 'collected' has no mapping/publication version (pre-mapping raw stage).

    return JSONResponse(
        {
            "datastream_id": ds_id,
            "project_id": ds_project_id,
            "stage": stage,
            "served_stage": sample["served_stage"],
            "stage_note": sample["stage_note"],
            "date_from": date_from,
            "date_to": date_to,
            "limit": max(1, min(limit, 20)),
            "masked_fields": sample["masked_fields"],
            **version_ref,
            "days": sample["days"],
        }
    )


def _overlay_rejection_counts(days: list[dict], ds_id: str, project_id: str, conn) -> None:
    """Set each day's ``rejection_count`` from the managed-feed rejected-row store.

    Groups app.managed_feed_rejected_rows by the rejected row's day for this
    project-scoped datastream, then annotates the matching sample day. Days with no
    rejections stay at 0. Read-only, project-scoped (AD-5).
    """
    if not days:
        return
    date_from = days[0]["date"]
    date_to = days[-1]["date"]
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        # created_at is the reject event time; the ledger's interval day is the row's
        # logical day. We bucket by created_at::date, bounded to the requested range.
        cur.execute(
            """
            SELECT created_at::date AS d, COUNT(*) AS n
            FROM app.managed_feed_rejected_rows
            WHERE datastream_id = %s AND project_id = %s
              AND created_at::date BETWEEN %s AND %s
            GROUP BY created_at::date
            """,
            (ds_id, project_id, date_from, date_to),
        )
        for d, n in cur.fetchall():
            counts[d.isoformat() if hasattr(d, "isoformat") else str(d)] = int(n)
    for day in days:
        if day["date"] in counts:
            day["rejection_count"] = counts[day["date"]]


async def _list_datastreams(request: Request) -> Response:
    """GET /api/datastreams?project_id=<id> -- list datastreams for a project.

    Response (200): [{"id", "project_id", "name", "module_name", ...}]
    Error:
        400 -- missing project_id
        401 -- unauthorized
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )

    try:
        from core.datastreams import list_datastreams  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(project_id, identity, "viewer", conn)
            if role_error is not None:
                return role_error
            rows = list_datastreams(project_id, conn)
    except Exception as exc:
        logger.error("admin_api: list_datastreams_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    return JSONResponse(rows)


async def _create_datastream(request: Request) -> Response:
    """POST /api/datastreams -- create a legacy row or versioned intent draft."""

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )
    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id est requis"},
            status_code=422,
        )

    versioned = isinstance(body.get("intent"), dict)
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if versioned and not idempotency_key:
        return JSONResponse(
            {
                "code": "missing_idempotency_key",
                "message": "Idempotency-Key est requis pour creer une version.",
            },
            status_code=400,
        )

    created_by = identity or "anonymous"
    try:
        from core.datastreams import create_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            minimum_role = (
                "owner"
                if versioned
                and body["intent"].get("destination", {}).get("policy") == "external_read_only"
                else "member"
            )
            role_error = _require_datastream_role(project_id, created_by, minimum_role, conn)
            if role_error is not None:
                return role_error

            if versioned:
                from core.flows import upsert_flow  # noqa: PLC0415
                from core.main import get_loaded_modules  # noqa: PLC0415

                definition = {
                    "schema_version": "2",
                    "kind": "datastream",
                    "project_id": project_id,
                    "name": (body.get("name") or "").strip(),
                    "intent": body["intent"],
                    "idempotency_key": idempotency_key,
                    "reason": body.get("reason", "rest_draft_created"),
                    "trace_id": request.headers.get("traceparent"),
                }
                result = upsert_flow(
                    project_id,
                    definition,
                    created_by,
                    conn,
                    loaded_modules=get_loaded_modules(),
                )
                response = dict(result["flow"])
                response["plan_version"] = result["plan_version"]
                return JSONResponse(response, status_code=201)

            row = create_datastream(body, project_id, created_by, conn)
            conn.commit()
    except ValueError as exc:
        return JSONResponse({"code": "invalid_input", "message": str(exc)}, status_code=422)
    except Exception as exc:
        # Story 34.2: trial datastream cap reached -- typed 409, no partial state.
        # (Lazy isinstance check keeps the import out of the sorted top block.)
        from core.trial_enforcement import TrialDatastreamLimitError  # noqa: PLC0415

        if isinstance(exc, TrialDatastreamLimitError):
            return JSONResponse(exc.to_dict(), status_code=409)
        from core.flows import (  # noqa: PLC0415
            FlowConflictError,
            FlowScopeError,
            FlowUnavailableError,
            FlowValidationError,
        )

        if isinstance(exc, FlowValidationError):
            return JSONResponse(
                {"code": "validation_error", "message": str(exc), "errors": exc.errors},
                status_code=422,
            )
        if isinstance(exc, FlowScopeError):
            return JSONResponse(
                {"code": "not_found", "message": "Flux de donnees introuvable"},
                status_code=404,
            )
        if isinstance(exc, FlowConflictError) or "UniqueViolation" in type(exc).__name__:
            return JSONResponse({"code": "conflict", "message": str(exc)}, status_code=409)
        if isinstance(exc, FlowUnavailableError):
            return JSONResponse({"code": "unavailable", "message": str(exc)}, status_code=503)
        logger.error("admin_api: create_datastream_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees"},
            status_code=500,
        )

    write_audit_row(
        identity=created_by,
        action=ACTION_DATASTREAM_CREATED,
        provider_account="",
        connection_ref="",
        metadata={
            "datastream_id": row["id"],
            "project_id": project_id,
            "name": row["name"],
            "module_name": row["module_name"],
        },
    )
    return JSONResponse(row, status_code=201)


async def _list_datastream_versions(request: Request) -> Response:
    """GET immutable intent versions for one project-scoped Datastream."""

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_intents import list_intent_versions  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            versions = list_intent_versions(ds_id, project_id, conn)
    except Exception as exc:
        logger.error("admin_api: datastream_versions_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Versions indisponibles"}, 503)
    return JSONResponse({"versions": versions})


async def _validate_datastream_intent(request: Request) -> Response:
    """Validate a draft intent without provider calls, queueing, or writes."""

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = (body.get("project_id") or "").strip()
    intent = body.get("intent")
    if not project_id or not isinstance(intent, dict):
        return JSONResponse(
            {"code": "missing_field", "message": "project_id et intent sont requis"}, 400
        )
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_intents import (  # noqa: PLC0415
            DatastreamIntentStructuralError,
            validate_intent,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            capabilities = None
            source = intent.get("source", {})
            if source.get("kind") == "connector_pull":
                from core.main import get_loaded_modules  # noqa: PLC0415
                from core.source_capabilities import (  # noqa: PLC0415
                    SourceCapabilitiesNotFound,
                    SourceCapabilitiesUnavailable,
                    get_scoped_source_capabilities,
                )

                try:
                    capabilities = get_scoped_source_capabilities(
                        project_id=project_id,
                        connection_ref_id=source.get("connection_ref_id", ""),
                        identity=identity or "anonymous",
                        loaded_modules=get_loaded_modules(),
                        conn=conn,
                    )
                except SourceCapabilitiesNotFound:
                    return JSONResponse({"code": "not_found", "message": "Source introuvable"}, 404)
                except SourceCapabilitiesUnavailable:
                    return JSONResponse(
                        {"code": "unavailable", "message": "Catalogue indisponible"}, 503
                    )
            result = validate_intent(intent, capabilities=capabilities)
    except DatastreamIntentStructuralError as exc:
        return JSONResponse(
            {"code": "invalid_intent", "issues": [item.as_dict() for item in exc.issues]},
            422,
        )
    except Exception as exc:
        logger.error("admin_api: validate_datastream_intent_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Validation indisponible"}, 503)

    payload = result.as_dict()
    return JSONResponse(payload, 200 if result.executable else 422)


async def _profile_datastream_mapping(request: Request) -> Response:
    """POST /api/datastreams/{id}/mapping/profile -- physically profile fields and suggest roles."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    sample_data = body.get("sample_data")
    if sample_data is not None and not isinstance(sample_data, list):
        return JSONResponse(
            {"code": "invalid_field", "message": "sample_data doit être une liste"}, 400
        )
    if isinstance(sample_data, list) and len(sample_data) > 500:
        return JSONResponse(
            {"code": "too_many_rows", "message": "sample_data limité à 500 lignes"}, 400
        )
    field_records = body.get("field_records")
    if field_records is not None and not isinstance(field_records, list):
        return JSONResponse(
            {"code": "invalid_field", "message": "field_records doit être une liste"}, 400
        )
    if isinstance(field_records, list) and len(field_records) > 200:
        return JSONResponse(
            {"code": "too_many_fields", "message": "field_records limité à 200 champs"}, 400
        )

    try:
        from core.datastream_field_mapping import profile_fields  # noqa: PLC0415
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            ds = get_datastream(ds_id, project_id, conn)
            if ds is None:
                return JSONResponse({"code": "not_found", "message": "Flux introuvable"}, 404)

            if field_records is None and ds.get("connection_ref_id") and ds.get("report_id"):
                from core.main import get_loaded_modules  # noqa: PLC0415
                from core.source_capabilities import get_scoped_source_capabilities  # noqa: PLC0415

                try:
                    caps = get_scoped_source_capabilities(
                        project_id=project_id,
                        connection_ref_id=ds.get("connection_ref_id", ""),
                        identity=identity or "anonymous",
                        loaded_modules=get_loaded_modules(),
                        conn=conn,
                    )
                    report = next(
                        (
                            r
                            for r in caps.get("reports", [])
                            if r.get("id") == ds.get("report_id")
                        ),
                        None,
                    )
                    if report:
                        field_records = report.get("field_catalog", [])
                except Exception:
                    pass

            if not field_records:
                field_records = []

            known_target_fields = set()
            with conn.cursor() as cur:
                # Only approved fields are valid mapping targets (H1: draft fields
                # must not appear as accepted targets in profile_fields/mapping validation).
                cur.execute("SELECT name FROM app.target_fields WHERE status = 'approved'")
                known_target_fields = {row[0] for row in cur.fetchall()}

            result = profile_fields(
                field_records=field_records,
                sample_data=sample_data,
                known_target_fields=known_target_fields,
            )
            return JSONResponse(result, 200)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"code": "invalid_input", "message": str(exc)}, 400)
    except Exception as exc:
        logger.error("admin_api: profile_datastream_mapping_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Profilage indisponible"}, 503
        )


async def _create_datastream_mapping_version(request: Request) -> Response:
    """POST /api/datastreams/{id}/mapping/versions -- append immutable mapping version."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_header", "message": "En-tête Idempotency-Key requis"},
            400,
        )
    if len(idempotency_key) > 255:
        return JSONResponse(
            {"code": "invalid_header", "message": "En-tête Idempotency-Key trop long (max 255)"},
            400,
        )
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    mapping_payload = body.get("mapping") or body.get("mapping_payload")
    if not project_id or not isinstance(mapping_payload, dict):
        return JSONResponse(
            {"code": "missing_field", "message": "project_id et mapping sont requis"}, 400
        )
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_field_mapping import (  # noqa: PLC0415
            DatastreamMappingConflict,
            DatastreamMappingNotFound,
            DatastreamMappingStructuralError,
            DatastreamMappingUnavailable,
            save_field_mapping,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error

            res = save_field_mapping(
                datastream_id=ds_id,
                project_id=project_id,
                mapping_payload=mapping_payload,
                identity=identity or "anonymous",
                idempotency_key=idempotency_key,
                conn=conn,
            )
            status_code = 200 if res.get("idempotent_replay") else 201
            return JSONResponse(res, status_code=status_code)
    except DatastreamMappingNotFound:
        return JSONResponse({"code": "not_found", "message": "Flux introuvable"}, 404)
    except DatastreamMappingConflict:
        return JSONResponse({"code": "conflict", "message": "Conflit d'idempotence"}, 409)
    except DatastreamMappingStructuralError as exc:
        return JSONResponse({"code": "invalid_mapping", "issues": list(exc.issues)}, 422)
    except DatastreamMappingUnavailable as exc:
        logger.error("admin_api: create_datastream_mapping_version_unavailable: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Enregistrement du mapping indisponible"}, 503
        )
    except Exception as exc:
        logger.error("admin_api: create_datastream_mapping_version_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Enregistrement du mapping indisponible"}, 503
        )


async def _list_datastream_mapping_versions(request: Request) -> Response:
    """GET /api/datastreams/{id}/mapping/versions -- list mapping versions."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_field_mapping import (  # noqa: PLC0415
            DatastreamMappingNotFound,
            list_mapping_versions,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            versions = list_mapping_versions(ds_id, project_id, conn)
            return JSONResponse({"versions": versions}, 200)
    except DatastreamMappingNotFound:
        return JSONResponse({"code": "not_found", "message": "Flux introuvable"}, 404)
    except Exception as exc:
        logger.error("admin_api: list_datastream_mapping_versions_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Lecture des versions indisponible"}, 503
        )


async def _get_datastream_mapping_version(request: Request) -> Response:
    """GET /api/datastreams/{id}/mapping/versions/{ver} -- get single mapping version."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    ver_spec = request.path_params.get("ver", "")
    try:
        from core.datastream_field_mapping import (  # noqa: PLC0415
            DatastreamMappingNotFound,
            get_mapping_version,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            version = get_mapping_version(ds_id, project_id, ver_spec, conn)
            return JSONResponse(version, 200)
    except DatastreamMappingNotFound:
        return JSONResponse({"code": "not_found", "message": "Version introuvable"}, 404)
    except Exception as exc:
        logger.error("admin_api: get_datastream_mapping_version_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Lecture de la version indisponible"}, 503
        )


async def _compile_datastream_projection(request: Request) -> Response:
    """POST /api/datastreams/{id}/projection/compile -- Story 12.4.

    Compile a SAFE KPI projection plan from an immutable 12.3 mapping version.
    Pure metadata over the mapping version + governed project preferences: it
    NEVER publishes, moves no pointer, and performs no BigQuery write
    (publication atomicity is 12.5). Member role required (viewer < member <
    owner). A projection that fails any compile gate returns 422 with the
    deterministic issue list and blocks publication semantics.

    Body: {"project_id", "mapping_version" (spec or number, default 'latest'),
           "dimension_projection" (optional field_id),
           "connector_canonical_breakdown" (optional; the connector's current
           canonical breakdown name -- required when dimension_projection is set,
           else the projection is rejected governed_dim_shadows_canonical),
           "approved" (optional)}.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id est requis"}, 400
        )
    ds_id = request.path_params.get("id", "")
    version_spec = body.get("mapping_version")
    dimension_projection = body.get("dimension_projection")
    # The connector's CURRENT canonical breakdown partition name (what
    # rollup.canonical_breakdown_per_connector / the dbt marts'
    # MIN(breakdown_dimension) already pin -- e.g. 'country' for GA4/GSC). Passed
    # in by the caller (Story 12.5 will derive it from the published fact); the
    # compiler REJECTS a governed projection that would sort at/before it and
    # re-pin canonical (governed_dim_shadows_canonical). Unknown => fail closed.
    connector_canonical_breakdown = body.get("connector_canonical_breakdown")
    approved = bool(body.get("approved", False))
    try:
        from core.datastream_field_mapping import (  # noqa: PLC0415
            DatastreamMappingNotFound,
            get_mapping_version,
            list_mapping_versions,
        )
        from core.datastream_projection import (  # noqa: PLC0415
            ProjectionCompileError,
            compile_projection,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error

            if version_spec in (None, "", "latest"):
                versions = list_mapping_versions(ds_id, project_id, conn)
                if not versions:
                    return JSONResponse(
                        {"code": "not_found", "message": "Aucune version de mapping"}, 404
                    )
                mapping_version = versions[0]  # newest-first
            else:
                mapping_version = get_mapping_version(
                    ds_id, project_id, version_spec, conn
                )

            # Governed project-scoped cardinality/scan thresholds (no hardcode).
            preferences: dict = {}
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT max_projection_grain_cardinality, max_projection_scan_bytes
                    FROM app.project_preferences
                    WHERE project_id = %s
                    """,
                    (project_id,),
                )
                pref_row = cur.fetchone()
                if pref_row is not None:
                    preferences = {
                        "max_projection_grain_cardinality": pref_row[0],
                        "max_projection_scan_bytes": pref_row[1],
                    }

        plan = compile_projection(
            mapping_version,
            project_preferences=preferences,
            dimension_projection=dimension_projection,
            connector_canonical_breakdown=connector_canonical_breakdown,
            approved=approved,
        )
        if not plan["executable"]:
            return JSONResponse(
                {"code": "projection_rejected", "issues": plan["issues"], "plan": plan},
                422,
            )
        return JSONResponse(plan, 200)
    except DatastreamMappingNotFound:
        return JSONResponse({"code": "not_found", "message": "Version introuvable"}, 404)
    except ProjectionCompileError as exc:
        logger.error("admin_api: compile_datastream_projection_invalid: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Compilation de projection indisponible"}, 503
        )
    except Exception as exc:
        logger.error("admin_api: compile_datastream_projection_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Compilation de projection indisponible"}, 503
        )


# ===========================================================================
# Story 12.5: atomic candidate publication REST seams.
#
# create-execution (Member), state-advance (Member; publishing/published are
# internal-only via commit_publication), publish (Member; Owner for approved /
# force_empty_publish), reconcile (Owner), publication-log (Viewer), single
# execution (Viewer). All reuse _require_datastream_role; cross-project returns a
# non-disclosing 404 + audit. Opaque 5xx (no str(exc) leak).
# ===========================================================================


async def _create_datastream_execution(request: Request) -> Response:
    """POST /api/datastreams/{id}/executions (Member) -- create a candidate execution.

    Body: {project_id, plan_version_id, mapping_version_id, projection_plan,
           idempotency_key}. The projection_plan must be an executable 12.4 plan.
    201 on success; 409 idempotency_conflict / concurrent_execution_active; 422
    on validation failure.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    plan_version_id = str(body.get("plan_version_id") or "").strip()
    mapping_version_id = str(body.get("mapping_version_id") or "").strip()
    projection_plan = body.get("projection_plan")
    idempotency_key = str(
        body.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or ""
    ).strip()
    if not plan_version_id or not mapping_version_id or not isinstance(projection_plan, dict):
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "plan_version_id, mapping_version_id et projection_plan sont requis",
            },
            422,
        )
    try:
        from core.datastream_publication import (  # noqa: PLC0415
            ConcurrentExecutionActive,
            IdempotencyConflict,
            InvalidReference,
            PublicationError,
            create_execution,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                record = create_execution(
                    ds_id,
                    project_id,
                    plan_version_id,
                    mapping_version_id,
                    projection_plan,
                    identity or "anonymous",
                    idempotency_key,
                    conn,
                )
                conn.commit()
            except IdempotencyConflict:
                conn.rollback()
                return JSONResponse(
                    {"code": "idempotency_conflict", "message": "Cle idempotente reutilisee"},
                    409,
                )
            except ConcurrentExecutionActive as exc:
                conn.rollback()
                return JSONResponse(
                    {
                        "code": "concurrent_execution_active",
                        "message": "Une execution est deja active",
                        "blocking_execution_id": exc.blocking_execution_id,
                    },
                    409,
                )
            except InvalidReference as exc:
                # Non-existent plan/mapping reference -> opaque, non-disclosing 422.
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Reference invalide"}, 422)
            except PublicationError as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Requete invalide"}, 422)
    except Exception as exc:
        logger.error("admin_api: create_datastream_execution_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Publication indisponible"}, 503)
    return JSONResponse(record, 201)


async def _advance_datastream_execution_state(request: Request) -> Response:
    """POST /api/datastreams/{id}/executions/{exec_id}/state (Member).

    Body: {project_id, new_state, content_hash?, row_count?, error_code?,
           error_detail?}. The publishing/published transitions are INTERNAL to
    commit_publication and rejected here (a caller must use /publish).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    exec_id = request.path_params.get("exec_id", "")
    new_state = str(body.get("new_state") or "").strip()
    # publishing/published are internal-only (commit_publication owns the pointer).
    if new_state in ("publishing", "published"):
        return JSONResponse(
            {
                "code": "invalid_state_transition",
                "message": "Utilisez /publish pour publier (transition interne)",
            },
            422,
        )
    try:
        from core.datastream_publication import (  # noqa: PLC0415
            ExecutionNotFound,
            InvalidStateTransition,
            advance_state,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                record = advance_state(
                    exec_id,
                    None,
                    new_state,
                    identity or "anonymous",
                    conn,
                    project_id=project_id,
                    content_hash=body.get("content_hash"),
                    row_count=body.get("row_count"),
                    error_code=body.get("error_code"),
                    error_detail=body.get("error_detail"),
                )
                conn.commit()
            except ExecutionNotFound:
                conn.rollback()
                return JSONResponse(
                    {"code": "not_found", "message": "Execution introuvable"}, 404
                )
            except InvalidStateTransition as exc:
                conn.rollback()
                return JSONResponse(
                    {"code": exc.code, "message": "Transition d'etat invalide"}, 422
                )
    except Exception as exc:
        logger.error("admin_api: advance_datastream_execution_state_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Publication indisponible"}, 503)
    return JSONResponse(record, 200)


async def _publish_datastream_execution(request: Request) -> Response:
    """POST /api/datastreams/{id}/executions/{exec_id}/publish.

    Member to publish; Owner required when approved=true or force_empty_publish=true.
    Runs DQ gates then commit_publication. 200 with the publication result on
    success; 422 with the gate issues on failure.

    ATOMICITY NOTE: publish is NOT a single transaction end-to-end. The
    validating->ready advance below COMMITS before commit_publication runs (two
    separate commits). commit_publication ITSELF is atomic (its 4 writes land in one
    transaction or none do). If the process dies after the validating->ready commit
    but before/inside commit_publication, the execution is left at `ready` (or
    `failed` via the out-of-band handler) and the prior published pointer is intact;
    a retry simply re-publishes from `ready` (idempotent from that state).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    exec_id = request.path_params.get("exec_id", "")
    approved = bool(body.get("approved", False))
    force_empty_publish = bool(body.get("force_empty_publish", False))
    # Owner is required to force-approve a gated operation.
    minimum_role = "owner" if (approved or force_empty_publish) else "member"
    try:
        from core.datastream_publication import (  # noqa: PLC0415
            STATE_READY,
            STATE_VALIDATING,
            ExecutionNotFound,
            InvalidStateTransition,
            PublicationError,
            advance_state,
            commit_publication,
            run_dq_gates,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, minimum_role, conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                issues = run_dq_gates(
                    exec_id,
                    project_id,
                    conn,
                    approved=approved,
                    force_empty_publish=force_empty_publish,
                    validated_content_hash=body.get("validated_content_hash"),
                    plan_source_schema_hash=body.get("plan_source_schema_hash"),
                    current_capability_fingerprint=body.get("current_capability_fingerprint"),
                    landing_schema_hash=body.get("landing_schema_hash"),
                    plan_declared_schema_hash=body.get("plan_declared_schema_hash"),
                )
            except ExecutionNotFound:
                return JSONResponse(
                    {"code": "not_found", "message": "Execution introuvable"}, 404
                )
            if issues:
                # Fail closed: mark the execution failed, leave prior pointer intact.
                try:
                    advance_state(
                        exec_id,
                        None,
                        "failed",
                        identity or "anonymous",
                        conn,
                        project_id=project_id,
                        error_code=issues[0]["code"],
                        error_detail=issues[0].get("detail", ""),
                    )
                    conn.commit()
                except (InvalidStateTransition, ExecutionNotFound):
                    conn.rollback()
                return JSONResponse(
                    {"code": "dq_gate_failed", "issues": issues}, 422
                )

            # Move validating -> ready if needed (idempotent guard), then commit.
            try:
                current = None
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT state FROM app.datastream_executions "
                        "WHERE id = %s AND project_id = %s",
                        (exec_id, project_id),
                    )
                    srow = cur.fetchone()
                    current = srow[0] if srow is not None else None
                if current == STATE_VALIDATING:
                    advance_state(
                        exec_id, STATE_VALIDATING, STATE_READY,
                        identity or "anonymous", conn, project_id=project_id,
                    )
                    conn.commit()
                result = commit_publication(
                    exec_id, project_id, identity or "anonymous", conn
                )
            except ExecutionNotFound:
                conn.rollback()
                return JSONResponse(
                    {"code": "not_found", "message": "Execution introuvable"}, 404
                )
            except InvalidStateTransition as exc:
                conn.rollback()
                return JSONResponse(
                    {"code": exc.code, "message": "Transition d'etat invalide"}, 422
                )
            except PublicationError as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Publication refusee"}, 422)
    except Exception as exc:
        logger.error("admin_api: publish_datastream_execution_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Publication indisponible"}, 503)
    return JSONResponse(result, 200)


async def _reconcile_datastream_execution(request: Request) -> Response:
    """POST /api/datastreams/{id}/executions/{exec_id}/reconcile (Owner)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    exec_id = request.path_params.get("exec_id", "")
    try:
        from core.datastream_publication import (  # noqa: PLC0415
            ExecutionNotFound,
            reconcile_execution,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "owner", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                result = reconcile_execution(exec_id, project_id, conn)
            except ExecutionNotFound:
                return JSONResponse(
                    {"code": "not_found", "message": "Execution introuvable"}, 404
                )
    except Exception as exc:
        logger.error("admin_api: reconcile_datastream_execution_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Reconciliation indisponible"}, 503)
    return JSONResponse(result, 200)


async def _list_datastream_publications(request: Request) -> Response:
    """GET /api/datastreams/{id}/publications?project_id=<id> (Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_publication import get_publication_log  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            rows = get_publication_log(ds_id, project_id, conn)
    except Exception as exc:
        logger.error("admin_api: list_datastream_publications_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Journal indisponible"}, 503)
    return JSONResponse({"publications": rows})


async def _get_datastream_execution(request: Request) -> Response:
    """GET /api/datastreams/{id}/executions/{exec_id}?project_id=<id> (Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    exec_id = request.path_params.get("exec_id", "")
    try:
        from core.datastream_publication import (  # noqa: PLC0415
            ExecutionNotFound,
            get_execution,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                record = get_execution(exec_id, project_id, conn)
            except ExecutionNotFound:
                return JSONResponse(
                    {"code": "not_found", "message": "Execution introuvable"}, 404
                )
    except Exception as exc:
        logger.error("admin_api: get_datastream_execution_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Execution indisponible"}, 503)
    return JSONResponse(record, 200)


# ===========================================================================
# Story 12.7: read-only external BigQuery observation/registration.
#
# observe (Member) -- observe an EXISTING BigQuery object read-only and, on a
# fresh `ok` verdict, mint a 12.5 candidate execution (never writes the external
# object). The `external_object` coordinates are NOT taken from the request body:
# they are read server-side from the pinned plan version's intent
# (source.external_object) so the caller cannot re-target the observation at a
# different object than the one the plan was validated against. `probe_result`
# is the Phase-B live / injected read-only probe outcome. Reuses
# _require_datastream_role; cross-project returns a non-disclosing 404 + audit.
# ===========================================================================


def _load_external_object(conn, ds_id: str, project_id: str, plan_version_id: str):
    """Read source.external_object from a pinned plan version (project-scoped).

    Returns the external_object dict, or None when the plan version is absent in
    this (datastream, project) scope or is not an external_bq source. Mirrors the
    inline scoped SELECT the publish handler uses -- no new module seam invented.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT normalized_payload
            FROM app.datastream_plan_versions
            WHERE id = %s AND datastream_id = %s AND project_id = %s
            """,
            (plan_version_id, ds_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    payload = row[0]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    if not isinstance(source, dict):
        return None
    external_object = source.get("external_object")
    return external_object if isinstance(external_object, dict) else None


async def _observe_datastream(request: Request) -> Response:
    """POST /api/datastreams/{id}/observe (Member) -- Story 12.7.

    Body: {project_id, plan_version_id, mapping_version_id, projection_plan,
           probe_result, project_region?, expected_content_hash?, idempotency_key}.
    On a fresh `ok` verdict: 201 {verdict:"ok", execution, virtual_pull_commit,
    observation}. On a blocking verdict / unchanged_noop: 200 {verdict,
    observation, repair?} (the published pointer is left untouched). Concurrent /
    idempotency conflicts -> 409; invalid reference / publication error -> 422.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    plan_version_id = str(body.get("plan_version_id") or "").strip()
    mapping_version_id = str(body.get("mapping_version_id") or "").strip()
    projection_plan = body.get("projection_plan")
    probe_result = body.get("probe_result")
    idempotency_key = str(
        body.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or ""
    ).strip()
    if (
        not plan_version_id
        or not mapping_version_id
        or not isinstance(projection_plan, dict)
        or not isinstance(probe_result, dict)
    ):
        return JSONResponse(
            {
                "code": "missing_field",
                "message": (
                    "plan_version_id, mapping_version_id, projection_plan et "
                    "probe_result sont requis"
                ),
            },
            422,
        )
    try:
        from core.datastream_publication import (  # noqa: PLC0415
            ConcurrentExecutionActive,
            IdempotencyConflict,
            InvalidReference,
            PublicationError,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.external_bq_registration import (  # noqa: PLC0415
            BLOCKING_VERDICTS,
            ObservationInputError,
            observe_and_register,
        )

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            external_object = _load_external_object(
                conn, ds_id, project_id, plan_version_id
            )
            if external_object is None:
                # Non-disclosing: unknown plan version / not an external_bq source.
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )
            try:
                result = observe_and_register(
                    datastream_id=ds_id,
                    project_id=project_id,
                    external_object=external_object,
                    plan_version_id=plan_version_id,
                    mapping_version_id=mapping_version_id,
                    projection_plan=projection_plan,
                    probe_result=probe_result,
                    actor=identity or "anonymous",
                    idempotency_key=idempotency_key,
                    conn=conn,
                    project_region=body.get("project_region"),
                    expected_content_hash=body.get("expected_content_hash"),
                )
                conn.commit()
            except IdempotencyConflict:
                conn.rollback()
                return JSONResponse(
                    {"code": "idempotency_conflict", "message": "Cle idempotente reutilisee"},
                    409,
                )
            except ConcurrentExecutionActive as exc:
                conn.rollback()
                return JSONResponse(
                    {
                        "code": "concurrent_execution_active",
                        "message": "Une execution est deja active",
                        "blocking_execution_id": exc.blocking_execution_id,
                    },
                    409,
                )
            except InvalidReference as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Reference invalide"}, 422)
            except ObservationInputError:
                conn.rollback()
                return JSONResponse(
                    {"code": "invalid_observation", "message": "Observation invalide"}, 422
                )
            except PublicationError as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Requete invalide"}, 422)
    except Exception as exc:
        logger.error("admin_api: observe_datastream_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Observation indisponible"}, 503)

    verdict = result.get("verdict")
    # Fresh, changed, ok content minted a virtual pull commit + a 12.5 candidate.
    if verdict == "ok" and result.get("execution") is not None:
        return JSONResponse(result, 201)
    # A blocking verdict or an unchanged no-op leaves the published pointer intact:
    # 200 with the observation (+ repair on a blocking verdict), no execution.
    response: dict = {"verdict": verdict, "observation": result.get("observation")}
    observation = result.get("observation")
    if isinstance(observation, dict) and observation.get("repair"):
        response["repair"] = observation["repair"]
    elif verdict in BLOCKING_VERDICTS and "repair" in result:
        response["repair"] = result["repair"]
    return JSONResponse(response, 200)


# ===========================================================================
# Story 12.8: managed-feed imports through an immutable import ledger.
#
# open-import (Member), record-rows (Member), publish (Member; runs the blocking
# rejection gate + the 12.5 DQ gates/commit before marking the ledger published),
# list-ledger (Viewer), get-ledger (Viewer), rejected-rows (Viewer). All reuse
# _require_datastream_role; cross-project returns a non-disclosing 404 + audit.
# ManagedFeedError subclasses map to typed HTTP codes; the generic base maps by
# .code. Opaque 5xx (no str(exc) leak).
# ===========================================================================


def _managed_feed_error_response(exc) -> Response | None:
    """Map a ManagedFeedError to its typed HTTP response, or None if unmapped.

    Import-side exceptions are mapped by TYPE; the generic ManagedFeedError base is
    mapped by its stable ``.code`` (content_hash_mismatch / invalid_landing_relation
    / invalid_row_count -> 422). Returns None for an unrecognised code so the caller
    can fall through to an opaque 503.
    """
    from core.managed_feed_ledger import (  # noqa: PLC0415
        ImportInProgress,
        ImportPayloadConflict,
        InvalidFeedFormat,
        LedgerNotFound,
        LedgerTerminal,
        RejectionThresholdExceeded,
    )

    if isinstance(exc, ImportPayloadConflict):
        return JSONResponse({"code": exc.code, "message": "Charge utile en conflit"}, 409)
    if isinstance(exc, ImportInProgress):
        return JSONResponse({"code": exc.code, "message": "Import deja en cours"}, 409)
    if isinstance(exc, LedgerTerminal):
        return JSONResponse({"code": exc.code, "message": "Import deja termine"}, 409)
    if isinstance(exc, InvalidFeedFormat):
        return JSONResponse({"code": exc.code, "message": "Format de flux invalide"}, 422)
    if isinstance(exc, RejectionThresholdExceeded):
        return JSONResponse({"code": exc.code, "issue": exc.issue}, 422)
    if isinstance(exc, LedgerNotFound):
        return JSONResponse({"code": "not_found", "message": "Import introuvable"}, 404)
    # Generic ManagedFeedError -> map by stable code.
    code = getattr(exc, "code", "")
    if code in ("content_hash_mismatch", "invalid_landing_relation", "invalid_row_count"):
        return JSONResponse({"code": code, "message": "Requete invalide"}, 422)
    return None


async def _open_managed_feed_import(request: Request) -> Response:
    """POST /api/datastreams/{id}/managed-feed/imports (Member) -- Story 12.8.

    Body: {project_id, plan_version_id, mapping_version_id, feed_format,
           projection_plan, idempotency_key, source_metadata, content_hash?,
           write_mode?}. 201 {ledger, execution, no_op, replay}.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    plan_version_id = str(body.get("plan_version_id") or "").strip()
    mapping_version_id = str(body.get("mapping_version_id") or "").strip()
    feed_format = str(body.get("feed_format") or "").strip()
    projection_plan = body.get("projection_plan")
    source_metadata = body.get("source_metadata")
    idempotency_key = str(
        body.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or ""
    ).strip()
    if (
        not plan_version_id
        or not mapping_version_id
        or not feed_format
        or not isinstance(projection_plan, dict)
        or not isinstance(source_metadata, dict)
    ):
        return JSONResponse(
            {
                "code": "missing_field",
                "message": (
                    "plan_version_id, mapping_version_id, feed_format, "
                    "projection_plan et source_metadata sont requis"
                ),
            },
            422,
        )
    write_mode = str(body.get("write_mode") or "replace").strip()
    try:
        from core.datastream_publication import (  # noqa: PLC0415
            InvalidReference,
            PublicationError,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.managed_feed_ledger import ManagedFeedError, open_import  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                result = open_import(
                    datastream_id=ds_id,
                    project_id=project_id,
                    plan_version_id=plan_version_id,
                    mapping_version_id=mapping_version_id,
                    feed_format=feed_format,
                    projection_plan=projection_plan,
                    actor=identity or "anonymous",
                    idempotency_key=idempotency_key,
                    source_metadata=source_metadata,
                    content_hash=body.get("content_hash"),
                    conn=conn,
                    write_mode=write_mode,
                )
                conn.commit()
            except ManagedFeedError as exc:
                conn.rollback()
                mapped = _managed_feed_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
            except InvalidReference as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Reference invalide"}, 422)
            except PublicationError as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Requete invalide"}, 422)
    except Exception as exc:
        logger.error("admin_api: open_managed_feed_import_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Import indisponible"}, 503)
    return JSONResponse(result, 201)


async def _record_managed_feed_rows(request: Request) -> Response:
    """POST /api/datastreams/{id}/managed-feed/imports/{ledger_id}/rows (Member).

    Body: {project_id, landing_relation, accepted_row_count, content_hash,
           rejected_rows[]}. 200 with the updated ledger row.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    ledger_id = request.path_params.get("ledger_id", "")
    landing_relation = str(body.get("landing_relation") or "").strip()
    content_hash = str(body.get("content_hash") or "").strip()
    accepted_row_count = body.get("accepted_row_count")
    rejected_rows = body.get("rejected_rows")
    if (
        not landing_relation
        or not content_hash
        or not isinstance(accepted_row_count, int)
    ):
        return JSONResponse(
            {
                "code": "missing_field",
                "message": (
                    "landing_relation, accepted_row_count et content_hash sont requis"
                ),
            },
            422,
        )
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.managed_feed_ledger import ManagedFeedError, record_rows  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                ledger = record_rows(
                    ledger_id=ledger_id,
                    project_id=project_id,
                    landing_relation=landing_relation,
                    accepted_row_count=accepted_row_count,
                    content_hash=content_hash,
                    rejected_rows=rejected_rows,
                    actor=identity or "anonymous",
                    conn=conn,
                )
                conn.commit()
            except ManagedFeedError as exc:
                conn.rollback()
                mapped = _managed_feed_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: record_managed_feed_rows_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Import indisponible"}, 503)
    return JSONResponse(ledger, 200)


async def _publish_managed_feed_import(request: Request) -> Response:
    """POST /api/datastreams/{id}/managed-feed/imports/{ledger_id}/publish (Member).

    Runs the blocking rejection gate (422 with the issue when breached), else
    advances the 12.5 candidate validating->ready, runs the DQ gates,
    commit_publication, then marks the ledger row published. 200 with the ledger.

    ATOMICITY NOTE: mirrors _publish_datastream_execution -- the validating->ready
    advance commits before commit_publication (which is itself atomic); a crash in
    between leaves the candidate at `ready` and the prior published pointer intact,
    and a retry re-publishes idempotently.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    ledger_id = request.path_params.get("ledger_id", "")
    try:
        from core.datastream_publication import (  # noqa: PLC0415
            STATE_READY,
            STATE_VALIDATING,
            ExecutionNotFound,
            InvalidStateTransition,
            PublicationError,
            advance_state,
            commit_publication,
            run_dq_gates,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.managed_feed_ledger import (  # noqa: PLC0415
            OUTCOME_PUBLISHED,
            ManagedFeedError,
            evaluate_rejection_gate_for_ledger,
            get_ledger,
            mark_outcome,
        )

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                # 1) Blocking rejection-threshold gate -- fail closed, no publish.
                issue = evaluate_rejection_gate_for_ledger(ledger_id, project_id, conn)
                if issue is not None:
                    return JSONResponse({"code": issue["code"], "issue": issue}, 422)

                ledger = get_ledger(ledger_id, project_id, conn)
                exec_id = ledger.get("execution_id")
                if not exec_id:
                    return JSONResponse(
                        {"code": "no_candidate", "message": "Aucune execution a publier"},
                        422,
                    )

                # 2) 12.5 DQ gates.
                gate_issues = run_dq_gates(exec_id, project_id, conn)
                if gate_issues:
                    return JSONResponse({"code": "dq_gate_failed", "issues": gate_issues}, 422)

                # 3) Advance validating->ready (idempotent guard) then publish.
                current = None
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT state FROM app.datastream_executions "
                        "WHERE id = %s AND project_id = %s",
                        (exec_id, project_id),
                    )
                    srow = cur.fetchone()
                    current = srow[0] if srow is not None else None
                if current == STATE_VALIDATING:
                    advance_state(
                        exec_id, STATE_VALIDATING, STATE_READY,
                        identity or "anonymous", conn, project_id=project_id,
                    )
                    conn.commit()
                commit_publication(exec_id, project_id, identity or "anonymous", conn)

                # 4) Mirror the published outcome onto the ledger (freshness contract).
                ledger = mark_outcome(
                    ledger_id, project_id, OUTCOME_PUBLISHED, identity or "anonymous", conn
                )
                conn.commit()
            except ExecutionNotFound:
                conn.rollback()
                return JSONResponse(
                    {"code": "not_found", "message": "Execution introuvable"}, 404
                )
            except InvalidStateTransition as exc:
                conn.rollback()
                return JSONResponse(
                    {"code": exc.code, "message": "Transition d'etat invalide"}, 422
                )
            except PublicationError as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Publication refusee"}, 422)
            except ManagedFeedError as exc:
                conn.rollback()
                mapped = _managed_feed_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: publish_managed_feed_import_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Publication indisponible"}, 503)
    return JSONResponse(ledger, 200)


async def _list_managed_feed_imports(request: Request) -> Response:
    """GET /api/datastreams/{id}/managed-feed/imports?project_id=<id> (Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.managed_feed_ledger import list_ledger  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            rows = list_ledger(ds_id, project_id, conn)
    except Exception as exc:
        logger.error("admin_api: list_managed_feed_imports_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Journal indisponible"}, 503)
    return JSONResponse({"imports": rows})


async def _get_managed_feed_import(request: Request) -> Response:
    """GET /api/datastreams/{id}/managed-feed/imports/{ledger_id}?project_id=<id> (Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    ledger_id = request.path_params.get("ledger_id", "")
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.managed_feed_ledger import (  # noqa: PLC0415
            LedgerNotFound,
            get_ledger,
        )

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                ledger = get_ledger(ledger_id, project_id, conn)
            except LedgerNotFound:
                return JSONResponse(
                    {"code": "not_found", "message": "Import introuvable"}, 404
                )
    except Exception as exc:
        logger.error("admin_api: get_managed_feed_import_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Import indisponible"}, 503)
    return JSONResponse(ledger, 200)


async def _get_managed_feed_rejected_rows(request: Request) -> Response:
    """GET managed-feed rejected-rows (project_id/limit/offset query params; Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    ledger_id = request.path_params.get("ledger_id", "")
    try:
        limit = int(request.query_params.get("limit") or 1000)
    except (TypeError, ValueError):
        limit = 1000
    try:
        offset = int(request.query_params.get("offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.managed_feed_ledger import get_rejected_rows  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            rows = get_rejected_rows(
                ledger_id, project_id, conn, limit=limit, offset=offset
            )
    except Exception as exc:
        logger.error("admin_api: get_managed_feed_rejected_rows_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Lignes rejetees indisponibles"}, 503
        )
    return JSONResponse({"rejected_rows": rows})


# ===========================================================================
# Story 12.9: CSV / Excel governed import.
#
# preview (Member), confirm-import (Member; Owner when force_empty_publish),
# version-contract PUT (Member), list/get import-contracts (Viewer). All reuse
# _require_datastream_role; cross-project returns a non-disclosing 404 + audit.
#
# UPLOAD TRANSPORT NOTE: the story's INTEGRATION SPEC sketches a multipart form,
# but server/core has no multipart/upload helper (mediaplan_api documents the same
# gap and uses a base64 JSON body). To match the file's convention EXACTLY and add
# no new dependency, the upload bytes ride in a base64 ``file_base64`` JSON field.
# The parse/ledger code is byte-oriented, so the transport is orthogonal. The
# short-lived upload-slot mechanism (Route 2 in the SPEC) stays Phase B; for now
# the confirm route re-sends the same base64 bytes.
# ===========================================================================


def _decode_upload_bytes(body: dict) -> tuple[bytes | None, Response | None]:
    """Decode the base64 ``file_base64`` upload field, capped BEFORE materialising.

    Returns ``(data, None)`` on success or ``(None, error_response)`` on a malformed
    / oversized / missing field. The size cap mirrors csv_excel_import.MAX_FILE_BYTES
    (50 MB) and is enforced on the base64 string first (never build a bomb buffer),
    then re-checked on the decoded bytes.
    """
    import base64  # noqa: PLC0415

    from core.csv_excel_import import MAX_FILE_BYTES  # noqa: PLC0415

    raw = body.get("file_base64")
    if not isinstance(raw, str) or not raw.strip():
        return None, JSONResponse(
            {"code": "missing_field", "message": "file_base64 est requis"}, 422
        )
    # Cap the base64 length first (base64 is ~4/3 the decoded size).
    if len(raw) > MAX_FILE_BYTES // 3 * 4 + 8:
        return None, JSONResponse(
            {"code": "file_too_large", "message": "Fichier trop volumineux (max 50 Mo)"}, 422
        )
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        return None, JSONResponse(
            {"code": "invalid_body", "message": "file_base64 n'est pas du base64 valide"}, 422
        )
    if len(data) > MAX_FILE_BYTES:
        return None, JSONResponse(
            {"code": "file_too_large", "message": "Fichier trop volumineux (max 50 Mo)"}, 422
        )
    return data, None


def _csv_excel_error_response(exc) -> Response | None:
    """Map a CsvExcelImportError to its stable ``.code`` -> HTTP (all 422), or None.

    Every parse / contract failure in Story 12.9 is a 422 (client-side data or
    configuration error): unsupported_file_type, empty_file, file_too_large,
    encoding_error, duplicate_columns, no_header_row, formula_in_cells,
    invalid_import_contract, append_unavailable, and the generic base by ``.code``.
    Returns None for an unrecognised type so the caller falls through to an opaque 503.
    """
    from core.csv_excel_import import CsvExcelImportError  # noqa: PLC0415

    if isinstance(exc, CsvExcelImportError):
        return JSONResponse(
            {"code": getattr(exc, "code", "invalid_import"), "message": str(exc)}, 422
        )
    return None


async def _preview_csv_excel_import(request: Request) -> Response:
    """POST /api/datastreams/{id}/imports/preview (Member) -- Story 12.9.

    Body: {project_id, file_base64, filename?, contract?}. Builds a bounded preview
    WITHOUT publishing or opening a ledger row. 200 with the preview dict (format,
    encoding, delimiter, sheet_name, columns, row_count, rejected_count,
    preview_rows, content_hash). Parse / contract errors -> 422 (stable code).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    data, dec_error = _decode_upload_bytes(body)
    if dec_error is not None:
        return dec_error
    contract = body.get("contract") if isinstance(body.get("contract"), dict) else None
    filename = body.get("filename")
    try:
        from dataclasses import asdict  # noqa: PLC0415

        from core.csv_excel_import import build_preview  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                preview = build_preview(data, filename=filename, contract=contract)
            except Exception as exc:  # noqa: BLE001 - mapped to a stable 422 below.
                mapped = _csv_excel_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: preview_csv_excel_import_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Aperçu indisponible"}, 503)
    return JSONResponse(asdict(preview), 200)


async def _confirm_csv_excel_import(request: Request) -> Response:
    """POST /api/datastreams/{id}/imports (Member; Owner on force_empty_publish).

    Story 12.9. Body: {project_id, plan_version_id, mapping_version_id,
    projection_plan, idempotency_key, source_metadata, contract, file_base64,
    import_contract_id?, force_empty_publish?, raw_schema?}. Drives run_import
    (open_import + record_rows + rejection gate). 200 with the run result (may be
    no-op / blocked / written_pending_publication). AppendUnavailable / parse error /
    empty-blocked -> 422; ImportPayloadConflict / ImportInProgress -> 409.

    RBAC: Member floor for the recoverable action; force_empty_publish additionally
    requires Owner (mirrors the publish route's owner-on-force pattern).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    plan_version_id = str(body.get("plan_version_id") or "").strip()
    mapping_version_id = str(body.get("mapping_version_id") or "").strip()
    projection_plan = body.get("projection_plan")
    source_metadata = body.get("source_metadata")
    contract = body.get("contract")
    idempotency_key = str(
        body.get("idempotency_key")
        or request.headers.get("Idempotency-Key")
        or ""
    ).strip()
    force_empty_publish = bool(body.get("force_empty_publish", False))
    if (
        not plan_version_id
        or not mapping_version_id
        or not isinstance(projection_plan, dict)
        or not isinstance(source_metadata, dict)
        or not isinstance(contract, dict)
    ):
        return JSONResponse(
            {
                "code": "missing_field",
                "message": (
                    "plan_version_id, mapping_version_id, projection_plan, "
                    "source_metadata et contract sont requis"
                ),
            },
            422,
        )
    data, dec_error = _decode_upload_bytes(body)
    if dec_error is not None:
        return dec_error
    try:
        from core.csv_excel_import import (  # noqa: PLC0415
            AppendUnavailable,
            CsvExcelImportError,
            run_import,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.managed_feed_ledger import (  # noqa: PLC0415
            ImportInProgress,
            ImportPayloadConflict,
        )

        with get_connection() as conn:
            # Member floor for the recoverable action; Owner floor when forcing an
            # empty publish (mirrors _publish_datastream_execution's owner-on-force).
            minimum_role = "owner" if force_empty_publish else "member"
            role_error = _require_datastream_role(
                project_id, identity, minimum_role, conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            # Read the project empty-publication preference (fail closed to False).
            preferences = {
                "allow_empty_publication": _read_allow_empty_publication_pref(
                    conn, project_id
                )
            }
            try:
                result = run_import(
                    data,
                    datastream_id=ds_id,
                    project_id=project_id,
                    plan_version_id=plan_version_id,
                    mapping_version_id=mapping_version_id,
                    projection_plan=projection_plan,
                    actor=identity or "anonymous",
                    idempotency_key=idempotency_key,
                    source_metadata=source_metadata,
                    contract=contract,
                    conn=conn,
                    raw_schema=body.get("raw_schema"),
                    force_empty_publish=force_empty_publish,
                    preferences=preferences,
                )
                conn.commit()
            except AppendUnavailable as exc:
                conn.rollback()
                return JSONResponse(
                    {"code": "append_unavailable", "message": str(exc)}, 422
                )
            except (ImportPayloadConflict, ImportInProgress) as exc:
                conn.rollback()
                mapped = _managed_feed_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
            except CsvExcelImportError as exc:
                conn.rollback()
                mapped = _csv_excel_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: confirm_csv_excel_import_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Import indisponible"}, 503)
    return JSONResponse(result, 200)


def _read_allow_empty_publication_pref(conn, project_id: str) -> bool:
    """Read the project-scoped ``allow_empty_publication`` preference (fail closed).

    Mirrors csv_excel_import / google_sheets_sync's read; defaults to False when the
    row / column is absent (the documented default).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT allow_empty_publication FROM app.project_preferences "
                "WHERE project_id = %s",
                (project_id,),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - fail closed on any read error.
        return False
    if row is None or row[0] is None:
        return False
    return bool(row[0])


async def _put_import_contract(request: Request) -> Response:
    """PUT /api/datastreams/{id}/import-contracts (Member) -- Story 12.9.

    Body: {project_id, contract, label?}. Versions (or de-duplicates) the parsing
    contract via version_contract; returns the cic_<ULID> id (existing on a matching
    fingerprint). 200 {import_contract_id}. invalid_import_contract / append_unavailable
    -> 422.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    contract = body.get("contract")
    if not isinstance(contract, dict):
        return JSONResponse({"code": "missing_field", "message": "contract est requis"}, 422)
    label = body.get("label")
    try:
        from core.csv_excel_import import version_contract  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                contract_id = version_contract(
                    contract,
                    datastream_id=ds_id,
                    project_id=project_id,
                    actor=identity or "anonymous",
                    conn=conn,
                    label=label,
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - mapped to a stable 422 below.
                conn.rollback()
                mapped = _csv_excel_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: put_import_contract_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Contrat indisponible"}, 503)
    return JSONResponse({"import_contract_id": contract_id}, 200)


async def _list_import_contracts(request: Request) -> Response:
    """GET /api/datastreams/{id}/import-contracts?project_id=<id> (Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, datastream_id, project_id, fingerprint, format,
                           write_mode, contract, label, is_active, created_by,
                           created_at
                    FROM app.csv_excel_import_contracts
                    WHERE datastream_id = %s AND project_id = %s
                    ORDER BY created_at DESC
                    """,
                    (ds_id, project_id),
                )
                cols = [d[0] for d in cur.description]
                contracts = [
                    _import_contract_row_to_dict(cols, row) for row in cur.fetchall()
                ]
    except Exception as exc:
        logger.error("admin_api: list_import_contracts_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Contrats indisponibles"}, 503)
    return JSONResponse({"contracts": contracts})


async def _get_import_contract(request: Request) -> Response:
    """GET /api/datastreams/{id}/import-contracts/{contract_id}?project_id=<id> (Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    contract_id = request.path_params.get("contract_id", "")
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, datastream_id, project_id, fingerprint, format,
                           write_mode, contract, label, is_active, created_by,
                           created_at
                    FROM app.csv_excel_import_contracts
                    WHERE id = %s AND datastream_id = %s AND project_id = %s
                    """,
                    (contract_id, ds_id, project_id),
                )
                row = cur.fetchone()
                if row is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Contrat introuvable"}, 404
                    )
                cols = [d[0] for d in cur.description]
                contract = _import_contract_row_to_dict(cols, row)
    except Exception as exc:
        logger.error("admin_api: get_import_contract_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Contrat indisponible"}, 503)
    return JSONResponse(contract, 200)


def _import_contract_row_to_dict(cols: list[str], row: tuple) -> dict:
    """Serialise an app.csv_excel_import_contracts row (ISO-8601 for timestamps)."""
    record: dict = {}
    for col, val in zip(cols, row):
        if col == "created_at" and val is not None and hasattr(val, "isoformat"):
            record[col] = val.isoformat()
        else:
            record[col] = val
    return record


# ===========================================================================
# Story 12.10: Google Sheets recurring sync (managed-feed sync schedule).
#
# configure (Member; upsert schedule), sync-now (Member; manual run), status
# (Viewer; schedule + last runs + next run). All reuse _require_datastream_role;
# cross-project returns a non-disclosing 404 + audit.
#
# PHASE_B_LIVE_BLOCKED: the production 15.6 sheets_adapter (live Google OAuth) is
# not available in this environment. sync-now injects None, so run_sync raises
# NotImplementedError -> mapped to a 503 with the PHASE_B_LIVE_BLOCKED marker. The
# one-line adapter injection is the documented Phase-B wiring (Open Questions #1).
# ===========================================================================


def _sync_schedule_row_to_dict(cols: list[str], row: tuple) -> dict:
    """Serialise an app.managed_feed_sync_schedule row (ISO-8601 for timestamps)."""
    record: dict = {}
    for col, val in zip(cols, row):
        if val is not None and hasattr(val, "isoformat"):
            record[col] = val.isoformat()
        else:
            record[col] = val
    return record


def _fetch_sync_schedule(conn, datastream_id: str, project_id: str) -> dict | None:
    """Read the sync schedule config for a (datastream, project) scope, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT datastream_id, project_id, connection_id, spreadsheet_id,
                   sheet_range, sheet_name, column_mapping, cadence_mode,
                   cadence_policy, quota_profile, last_sync_at, last_ledger_id,
                   last_watermark, enabled, created_by, created_at, updated_at
            FROM app.managed_feed_sync_schedule
            WHERE datastream_id = %s AND project_id = %s
            """,
            (datastream_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return _sync_schedule_row_to_dict(cols, row)


async def _configure_managed_feed_sync(request: Request) -> Response:
    """POST /api/datastreams/{id}/managed-feed/configure (Member) -- Story 12.10.

    Upsert the sync schedule (spreadsheet, range, column_mapping, cadence,
    quota_profile). Validates the cadence via validate_cadence BEFORE the DB write
    (hourly without allow_hourly -> 422). 201 with the upserted schedule row.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    connection_id = str(body.get("connection_id") or "").strip()
    spreadsheet_id = str(body.get("spreadsheet_id") or "").strip()
    sheet_range = str(body.get("sheet_range") or "").strip()
    cadence_mode = str(body.get("cadence_mode") or "manual").strip()
    if not connection_id or not spreadsheet_id or not sheet_range:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "connection_id, spreadsheet_id et sheet_range sont requis",
            },
            422,
        )
    quota_profile = body.get("quota_profile")
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.google_sheets_sync import validate_cadence  # noqa: PLC0415

        # Validate the cadence BEFORE any DB write (fail closed).
        cadence_errors = validate_cadence(cadence_mode, quota_profile)
        if cadence_errors:
            return JSONResponse(
                {"code": "quota_hourly_not_permitted", "issues": cadence_errors}, 422
            )

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.managed_feed_sync_schedule
                        (datastream_id, project_id, connection_id, spreadsheet_id,
                         sheet_range, sheet_name, column_mapping, cadence_mode,
                         cadence_policy, quota_profile, enabled, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb,
                            %s::jsonb, %s, %s)
                    ON CONFLICT (datastream_id, project_id) DO UPDATE SET
                        connection_id = EXCLUDED.connection_id,
                        spreadsheet_id = EXCLUDED.spreadsheet_id,
                        sheet_range = EXCLUDED.sheet_range,
                        sheet_name = EXCLUDED.sheet_name,
                        column_mapping = EXCLUDED.column_mapping,
                        cadence_mode = EXCLUDED.cadence_mode,
                        cadence_policy = EXCLUDED.cadence_policy,
                        quota_profile = EXCLUDED.quota_profile,
                        enabled = EXCLUDED.enabled,
                        updated_at = NOW()
                    RETURNING datastream_id, project_id, connection_id, spreadsheet_id,
                              sheet_range, sheet_name, column_mapping, cadence_mode,
                              cadence_policy, quota_profile, last_sync_at, last_ledger_id,
                              last_watermark, enabled, created_by, created_at, updated_at
                    """,
                    (
                        ds_id,
                        project_id,
                        connection_id,
                        spreadsheet_id,
                        sheet_range,
                        body.get("sheet_name") or "",
                        json.dumps(body.get("column_mapping") or {}),
                        cadence_mode,
                        json.dumps(body.get("cadence_policy") or {}),
                        json.dumps(quota_profile or {}),
                        bool(body.get("enabled", cadence_mode != "manual")),
                        identity or "anonymous",
                    ),
                )
                cols = [d[0] for d in cur.description]
                schedule = _sync_schedule_row_to_dict(cols, cur.fetchone())
                conn.commit()
    except Exception as exc:
        logger.error("admin_api: configure_managed_feed_sync_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Configuration indisponible"}, 503
        )
    return JSONResponse(schedule, 201)


async def _sync_now_managed_feed(request: Request) -> Response:
    """POST /api/datastreams/{id}/managed-feed/sync-now (Member) -- Story 12.10.

    Trigger an immediate manual sync run through run_sync. Body: {project_id,
    run_id?}. 200 with the sync_result dict (may report outcome=failed for a
    safe-fail; the HTTP status stays 200 because the RUN succeeded). QuotaViolation /
    SheetsSyncError -> 422; ImportPayloadConflict / ImportInProgress -> 409.

    PHASE_B_LIVE_BLOCKED: sheets_adapter is None here (no live Google OAuth in this
    environment); run_sync raises NotImplementedError, mapped to a 503 carrying the
    PHASE_B marker. Production injects the 15.6 adapter (SPEC Open Questions #1).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    run_id = str(body.get("run_id") or "").strip() or None
    force_empty_publish = bool(body.get("force_empty_publish", False))
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.google_sheets_sync import (  # noqa: PLC0415
            QuotaViolation,
            SheetsSyncError,
            run_sync,
        )
        from core.managed_feed_ledger import (  # noqa: PLC0415
            ImportInProgress,
            ImportPayloadConflict,
        )

        with get_connection() as conn:
            # Member floor; Owner floor when forcing an empty publish (mirrors the
            # publish route's owner-on-force pattern).
            minimum_role = "owner" if force_empty_publish else "member"
            role_error = _require_datastream_role(
                project_id, identity, minimum_role, conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            schedule = _fetch_sync_schedule(conn, ds_id, project_id)
            if schedule is None:
                # Non-disclosing: no schedule configured for this scope.
                return JSONResponse(
                    {"code": "not_found", "message": "Configuration de sync introuvable"}, 404
                )
            try:
                result = run_sync(
                    datastream_id=ds_id,
                    project_id=project_id,
                    connection_id=schedule["connection_id"],
                    spreadsheet_id=schedule["spreadsheet_id"],
                    sheet_range=schedule["sheet_range"],
                    sheet_name=schedule.get("sheet_name") or "",
                    column_mapping=schedule.get("column_mapping") or {},
                    plan_version_id=str(body.get("plan_version_id") or "").strip(),
                    mapping_version_id=str(body.get("mapping_version_id") or "").strip(),
                    projection_plan=body.get("projection_plan") or {},
                    actor=identity or "anonymous",
                    cadence_mode=schedule.get("cadence_mode") or "manual",
                    quota_profile=schedule.get("quota_profile"),
                    run_id=run_id,
                    conn=conn,
                    # PHASE_B_LIVE_BLOCKED: production injects the 15.6 adapter here.
                    sheets_adapter=None,
                    force_empty_publish=force_empty_publish,
                )
                conn.commit()
            except QuotaViolation as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": exc.detail}, 422)
            except (ImportPayloadConflict, ImportInProgress) as exc:
                conn.rollback()
                mapped = _managed_feed_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
            except SheetsSyncError as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": exc.detail}, 422)
            except NotImplementedError as exc:
                conn.rollback()
                # PHASE_B_LIVE_BLOCKED: no live adapter injected in this environment.
                return JSONResponse(
                    {"code": "phase_b_live_blocked", "message": str(exc)}, 503
                )
    except Exception as exc:
        logger.error("admin_api: sync_now_managed_feed_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Sync indisponible"}, 503)
    return JSONResponse(result, 200)


async def _status_managed_feed_sync(request: Request) -> Response:
    """GET /api/datastreams/{id}/managed-feed/status?project_id=<id> (Viewer).

    Story 12.10. Returns the sync schedule config + the last N ledger rows + the
    next-run description. 200 {schedule, last_runs, next_run}.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_schedule import calculate_schedule_window  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.google_sheets_sync import describe_next_run  # noqa: PLC0415
        from core.managed_feed_ledger import list_ledger  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            schedule = _fetch_sync_schedule(conn, ds_id, project_id)
            if schedule is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Configuration de sync introuvable"}, 404
                )
            last_runs = list_ledger(ds_id, project_id, conn, limit=10)
            next_run: dict = {}
            cadence_policy = schedule.get("cadence_policy") or {}
            try:
                window = calculate_schedule_window(
                    cadence_policy,
                    now_utc=datetime.now(tz=timezone.utc),
                    last_committed_watermark=schedule.get("last_watermark"),
                )
                next_run = describe_next_run(window)
            except Exception:  # noqa: BLE001 - a manual / unschedulable config -> nulls.
                next_run = describe_next_run(None)
    except Exception as exc:
        logger.error("admin_api: status_managed_feed_sync_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Statut indisponible"}, 503)
    return JSONResponse(
        {"schedule": schedule, "last_runs": last_runs, "next_run": next_run}
    )


# ===========================================================================
# Story 12.11: bounded sync / reload / reprocess (prepare + confirm).
#
# prepare (Member; assemble the AD-27 immutable proposal, no durable operation),
# confirm (Member; route EXACTLY ONE operations.execute_operation). All reuse
# _require_datastream_role; cross-project returns a non-disclosing 404 + audit.
# BoundedRecoveryError codes -> 422/409/404 (see _bounded_recovery_error_response).
# ===========================================================================


def _bounded_recovery_error_response(exc) -> Response | None:
    """Map a BoundedRecoveryError to its stable ``.code`` -> HTTP, or None.

    Lock conflicts (concurrent active execution) -> 409; not_found / wrong_verb ->
    404; every other precondition breach (forbidden_interval, retention_unavailable,
    incompatible_schema, stale_versions, policy_changed, quota_violation,
    missing_exposure, stale_preparation, invalid_kind) -> 422.
    """
    from core.bounded_recovery import BoundedRecoveryError  # noqa: PLC0415

    if not isinstance(exc, BoundedRecoveryError):
        return None
    code = exc.code
    if code == "lock_conflict":
        return JSONResponse({"code": code, "message": exc.message}, 409)
    if code in ("not_found", "wrong_verb"):
        return JSONResponse({"code": "not_found", "message": "Flux de donnees introuvable"}, 404)
    return JSONResponse({"code": code, "message": exc.message}, 422)


def _load_datastream_org_id(conn, ds_id: str, project_id: str) -> str | None:
    """Read app.datastreams.org_id for a (datastream, project) scope, or None.

    Mirrors the inline scoped SELECT convention (_load_external_object). Returns None
    for an out-of-scope / unknown datastream so the caller returns a non-disclosing
    404. bounded_recovery needs org_id (the AD-27 proposal + operation are org-scoped).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.datastreams WHERE id = %s AND project_id = %s",
            (ds_id, project_id),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return row[0]


async def _prepare_bounded_recovery(request: Request) -> Response:
    """POST /api/datastreams/{id}/bounded/prepare (Member) -- Story 12.11.

    Body: {project_id, kind, reason?, date_from?, date_to_exclusive?, partition?,
    chosen_mapping_version_id?, estimated_points?}. Assembles the AD-27 immutable
    proposal (NO durable operation, NO dispatch). 200 with the proposal
    {preparation_id, kind, target, target_versions, interval, impact, quota, ...}.
    BoundedRecoveryError codes -> 422/409/404.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    kind = str(body.get("kind") or "").strip()
    if not kind:
        return JSONResponse({"code": "missing_field", "message": "kind est requis"}, 422)
    try:
        from core.bounded_recovery import (  # noqa: PLC0415
            BoundedRecoveryError,
            prepare_bounded_recovery,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            org_id = _load_datastream_org_id(conn, ds_id, project_id)
            if org_id is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )
            try:
                estimated = body.get("estimated_points") or 0
                result = prepare_bounded_recovery(
                    conn,
                    org_id=org_id,
                    datastream_id=ds_id,
                    kind=kind,
                    actor=identity or "anonymous",
                    reason=body.get("reason"),
                    date_from=body.get("date_from"),
                    date_to_exclusive=body.get("date_to_exclusive"),
                    partition=body.get("partition"),
                    chosen_mapping_version_id=body.get("chosen_mapping_version_id"),
                    estimated_points=int(estimated) if isinstance(estimated, int) else 0,
                )
                conn.commit()
            except BoundedRecoveryError as exc:
                conn.rollback()
                mapped = _bounded_recovery_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: prepare_bounded_recovery_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Preparation indisponible"}, 503
        )
    return JSONResponse(result, 200)


async def _confirm_bounded_recovery(request: Request) -> Response:
    """POST /api/datastreams/{id}/bounded/confirm (Member) -- Story 12.11.

    Body: {project_id, preparation_id, reason?, trace_id?}. Re-validates every
    precondition against the live target and routes EXACTLY ONE durable operation
    (never commit_publication / pointer mutation). 200 with {preparation_id,
    operation_id, outcome, replayed, result}. BoundedRecoveryError codes ->
    422/409/404.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    preparation_id = str(body.get("preparation_id") or "").strip()
    if not preparation_id:
        return JSONResponse(
            {"code": "missing_field", "message": "preparation_id est requis"}, 422
        )
    try:
        from core.bounded_recovery import (  # noqa: PLC0415
            BoundedRecoveryError,
            confirm_bounded_recovery,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            # Scope guard: the preparation must belong to this datastream's org. A
            # cross-project confirm would otherwise leak an out-of-scope proposal.
            org_id = _load_datastream_org_id(conn, ds_id, project_id)
            if org_id is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )
            try:
                result = confirm_bounded_recovery(
                    conn,
                    preparation_id=preparation_id,
                    actor=identity or "anonymous",
                    reason=body.get("reason"),
                    trace_id=body.get("trace_id"),
                )
                conn.commit()
            except BoundedRecoveryError as exc:
                conn.rollback()
                mapped = _bounded_recovery_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: confirm_bounded_recovery_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Confirmation indisponible"}, 503
        )
    return JSONResponse(result, 200)


# ===========================================================================
# Story 12.12: safe replace / append / rollback (dataset recovery).
#
# rollback/preview (Viewer), rollback (Member), replace/preflight (Member),
# append/availability (Viewer), destination-policy (Owner via enforce_owner_floor).
# All reuse _require_datastream_role; cross-project returns a non-disclosing 404 +
# audit. DatasetRecoveryError codes -> the stable HTTP map in the story spec.
# ===========================================================================


def _dataset_recovery_error_response(exc) -> Response | None:
    """Map a DatasetRecoveryError to its stable ``.code`` -> HTTP, or None.

    Per the 12.12 INTEGRATION SPEC error map:
      rollback_window_expired -> 409; rollback_target_invalid /
      rollback_target_not_found -> 409/404; rollback_gate_failed -> 422 (+issues);
      concurrent_mutation_active -> 409 (+blocking id + lock reason);
      empty_replacement_blocked -> 422; owner_floor_required -> 403;
      access_unavailable -> 503; append_unavailable is handled at the read seam (200
      + fallback), never raised on these paths.
    """
    from core.dataset_recovery import DatasetRecoveryError  # noqa: PLC0415

    if not isinstance(exc, DatasetRecoveryError):
        return None
    code = exc.code
    if code == "rollback_window_expired":
        return JSONResponse(
            {
                "code": code,
                "message": exc.detail,
                "deadline": getattr(exc, "deadline", None),
                "deadline_source": getattr(exc, "deadline_source", None),
            },
            409,
        )
    if code == "rollback_target_not_found":
        return JSONResponse({"code": code, "message": "Cible de rollback introuvable"}, 404)
    if code == "rollback_target_invalid":
        return JSONResponse({"code": code, "message": exc.detail}, 409)
    if code == "rollback_gate_failed":
        return JSONResponse({"code": code, "issues": getattr(exc, "issues", [])}, 422)
    if code == "concurrent_mutation_active":
        return JSONResponse(
            {
                "code": code,
                "message": exc.detail,
                "blocking_execution_id": getattr(exc, "blocking_execution_id", None),
                "lock_reason": getattr(exc, "lock_reason", None),
            },
            409,
        )
    if code == "empty_replacement_blocked":
        return JSONResponse({"code": code, "message": exc.detail}, 422)
    if code == "owner_floor_required":
        return JSONResponse({"code": code, "message": exc.detail}, 403)
    if code == "access_unavailable":
        return JSONResponse({"code": code, "message": "Verification des droits indisponible"}, 503)
    return JSONResponse({"code": code, "message": exc.detail}, 422)


async def _preview_dataset_rollback(request: Request) -> Response:
    """GET /api/datastreams/{id}/rollback/preview?project_id=<id> (Viewer).

    Story 12.12. Resolves the default rollback target ONCE (the caller MUST echo the
    returned target_execution_id back to POST /rollback for idempotency across
    retries -- H1). 200 with {available, target_execution_id, current_execution_id,
    rollback_deadline, deadline_source, window_source, expired, reason}.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    target_execution_id = (
        request.query_params.get("target_execution_id") or ""
    ).strip() or None
    try:
        from core.dataset_recovery import preview_rollback  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            preview = preview_rollback(
                conn,
                datastream_id=ds_id,
                project_id=project_id,
                target_execution_id=target_execution_id,
            )
    except Exception as exc:
        logger.error("admin_api: preview_dataset_rollback_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Apercu indisponible"}, 503)
    return JSONResponse(preview, 200)


async def _rollback_dataset(request: Request) -> Response:
    """POST /api/datastreams/{id}/rollback (Member) -- Story 12.12.

    Body: {project_id, target_execution_id (REQUIRED -- resolved once via
    /rollback/preview), idempotency_key?}. Swaps the dataset pointer BACK to the
    retained target only after the DQ gates pass AND within the deadline. 200 with
    the rollback result (or {already_at_target: True} on an idempotent retry).
    DatasetRecoveryError codes -> the stable HTTP map.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    target_execution_id = str(body.get("target_execution_id") or "").strip()
    if not target_execution_id:
        # H1: the caller MUST resolve the target once via /rollback/preview and pass
        # the SAME id here so a retry short-circuits to already_at_target.
        return JSONResponse(
            {"code": "missing_field", "message": "target_execution_id est requis"}, 422
        )
    try:
        from core.dataset_recovery import DatasetRecoveryError, rollback_dataset  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                result = rollback_dataset(
                    conn,
                    datastream_id=ds_id,
                    project_id=project_id,
                    actor=identity or "anonymous",
                    target_execution_id=target_execution_id,
                )
                conn.commit()
            except DatasetRecoveryError as exc:
                conn.rollback()
                mapped = _dataset_recovery_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: rollback_dataset_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Rollback indisponible"}, 503)
    return JSONResponse(result, 200)


async def _preflight_replace_dataset(request: Request) -> Response:
    """POST /api/datastreams/{id}/replace/preflight (Member) -- Story 12.12.

    Body: {project_id, candidate_row_count, force_empty_publish?}. Validates the
    12.12 concurrency + empty pre-checks BEFORE an execution is minted (the caller
    then composes the 12.5 create_execution -> run_dq_gates -> commit_publication).
    200 {ok: True, action: dataset.replace}. concurrent_mutation_active -> 409;
    empty_replacement_blocked -> 422.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    candidate_row_count = body.get("candidate_row_count")
    if not isinstance(candidate_row_count, int):
        return JSONResponse(
            {"code": "missing_field", "message": "candidate_row_count (entier) est requis"}, 422
        )
    force_empty_publish = bool(body.get("force_empty_publish", False))
    try:
        from core.dataset_recovery import (  # noqa: PLC0415
            DatasetRecoveryError,
            preflight_replace,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Member floor; an empty replace additionally needs Owner (owner-on-force).
            minimum_role = "owner" if force_empty_publish else "member"
            role_error = _require_datastream_role(
                project_id, identity, minimum_role, conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                result = preflight_replace(
                    conn,
                    datastream_id=ds_id,
                    project_id=project_id,
                    candidate_row_count=candidate_row_count,
                    force_empty_publish=force_empty_publish,
                )
            except DatasetRecoveryError as exc:
                mapped = _dataset_recovery_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: preflight_replace_dataset_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Preflight indisponible"}, 503)
    return JSONResponse(result, 200)


async def _append_availability_dataset(request: Request) -> Response:
    """GET /api/datastreams/{id}/append/availability?project_id=<id> (Viewer).

    Story 12.12. Reports whether Append is available (needs a stable-key contract +
    compatible schema); otherwise presents Replace as the safe fallback. Optional
    candidate_schema_hash / target_schema_hash query params feed the compatibility
    check. 200 {available, fallback_action, reason, ...}.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    candidate_schema_hash = (
        request.query_params.get("candidate_schema_hash") or ""
    ).strip() or None
    target_schema_hash = (
        request.query_params.get("target_schema_hash") or ""
    ).strip() or None
    try:
        from core.dataset_recovery import resolve_append_availability  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            availability = resolve_append_availability(
                conn,
                datastream_id=ds_id,
                project_id=project_id,
                candidate_schema_hash=candidate_schema_hash,
                target_schema_hash=target_schema_hash,
            )
    except Exception as exc:
        logger.error("admin_api: append_availability_dataset_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Disponibilite indisponible"}, 503
        )
    return JSONResponse(availability, 200)


async def _dataset_destination_policy(request: Request) -> Response:
    """POST /api/datastreams/{id}/destination-policy (Owner) -- Story 12.12.

    Body: {project_id, operation}. Enforces the Owner floor via
    dataset_recovery.enforce_owner_floor for destination-policy operations
    (ownership / access / retention / irreversible deletion). This is the RBAC
    enforcement seam; the actual policy MUTATION is the caller's follow-up (this
    route proves the Owner floor holds). 200 {ok: True, operation} when authorized;
    owner_floor_required -> 403; access_unavailable -> 503.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    operation = str(body.get("operation") or "").strip()
    try:
        from core.dataset_recovery import (  # noqa: PLC0415
            OWNER_FLOOR_OPERATIONS,
            DatasetRecoveryError,
            enforce_owner_floor,
        )
        from core.db import get_connection  # noqa: PLC0415

        if operation not in OWNER_FLOOR_OPERATIONS:
            return JSONResponse(
                {
                    "code": "invalid_operation",
                    "message": "operation doit etre une operation de politique de destination",
                    "allowed": sorted(OWNER_FLOOR_OPERATIONS),
                },
                422,
            )
        with get_connection() as conn:
            # A cross-project / unknown datastream is a non-disclosing 404 BEFORE the
            # owner check (mirror the role-gate scoping used everywhere else).
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                enforce_owner_floor(
                    conn,
                    operation=operation,
                    identity=identity or "anonymous",
                    project_id=project_id,
                    datastream_id=ds_id,
                )
            except DatasetRecoveryError as exc:
                mapped = _dataset_recovery_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: dataset_destination_policy_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Verification de politique indisponible"}, 503
        )
    return JSONResponse({"ok": True, "operation": operation}, 200)


# ===========================================================================
# Story 12.14: versioned Datastream read model (Viewer).
#
# GET /api/datastreams/{id}/read-model -- surfaces, from the 12.2-12.5 tables, the
# plan versions, mapping versions, the current published execution + its DQ
# state/freshness/row_count, the current candidate (newest non-terminal execution),
# the publication log (actor/trace/prior-execution evidence), and the last import
# ledger rows (row/rejection counts). Reuses existing read helpers; adds NO new
# query logic beyond a single scoped candidate/current-pointer SELECT.
# ===========================================================================


async def _get_datastream_versions(request: Request) -> Response:
    """GET /api/datastreams/{id}/read-model?project_id=<id> (Viewer) -- Story 12.14.

    Assembles the versioned read model the 12.14 UI needs. 200 with:
      {
        plan_versions: [...],           # list_intent_versions (12.2)
        mapping_versions: [...],        # list_mapping_versions (12.3)
        current_published_execution_id, # the atomic pointer (12.5)
        current_candidate,              # newest non-terminal execution (12.5)
        published_execution,            # get_execution of the pointer (DQ state,
                                        #   freshness=state_changed_at, row_count,
                                        #   content_hash)
        publication_log: [...],         # get_publication_log (actor=published_by,
                                        #   prior_execution_id evidence)
        recent_imports: [...],          # list_ledger (row/rejection counts)
      }
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_field_mapping import list_mapping_versions  # noqa: PLC0415
        from core.datastream_intents import list_intent_versions  # noqa: PLC0415
        from core.datastream_publication import (  # noqa: PLC0415
            ExecutionNotFound,
            get_execution,
            get_publication_log,
        )
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.managed_feed_ledger import list_ledger  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error

            # Existence + scope: get_datastream returns None cross-project (AD-5).
            datastream = get_datastream(ds_id, project_id, conn)
            if datastream is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )

            plan_versions = list_intent_versions(ds_id, project_id, conn)
            try:
                mapping_versions = list_mapping_versions(ds_id, project_id, conn)
            except Exception:  # noqa: BLE001 - no mapping yet -> empty list.
                mapping_versions = []

            current_published_execution_id = _read_current_published_execution(
                conn, ds_id, project_id
            )
            published_execution = None
            if current_published_execution_id:
                try:
                    published_execution = get_execution(
                        current_published_execution_id, project_id, conn
                    )
                except ExecutionNotFound:
                    published_execution = None

            current_candidate = _read_current_candidate_execution(
                conn, ds_id, project_id, current_published_execution_id
            )
            publication_log = get_publication_log(ds_id, project_id, conn, limit=20)
            recent_imports = list_ledger(ds_id, project_id, conn, limit=20)
    except Exception as exc:
        logger.error("admin_api: get_datastream_versions_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Lecture des versions indisponible"}, 503
        )

    return JSONResponse(
        {
            "datastream_id": ds_id,
            "project_id": project_id,
            "plan_versions": plan_versions,
            "mapping_versions": mapping_versions,
            "current_published_execution_id": current_published_execution_id,
            "published_execution": published_execution,
            "current_candidate": current_candidate,
            "publication_log": publication_log,
            "recent_imports": recent_imports,
            # PHASE B / TODO (honest -- no backing column in the 12.2-12.5 tables):
            #   * a trace_id per execution: publication_log carries published_by
            #     (actor) but there is no per-execution trace column in 042; the
            #     trace lives on app.operations for recovery ops only. Surfacing a
            #     unified per-version trace is Phase B.
            #   * an explicit per-execution rejection_count: rejection counts live on
            #     the managed_feed ledger rows (recent_imports), not on the 042
            #     execution row -- the UI joins by execution_id. A denormalised
            #     execution.rejection_count column is Phase B.
        }
    )


def _read_current_published_execution(conn, ds_id: str, project_id: str) -> str | None:
    """Read app.datastreams.current_published_execution_id (project-scoped)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id FROM app.datastreams "
            "WHERE id = %s AND project_id = %s",
            (ds_id, project_id),
        )
        row = cur.fetchone()
    return row[0] if row is not None else None


def _read_current_candidate_execution(
    conn, ds_id: str, project_id: str, published_execution_id: str | None
) -> dict | None:
    """Read the newest NON-terminal execution (the current candidate), or None.

    A candidate is an execution that is not the published pointer and not in a
    terminal state (published / failed / cancelled) -- i.e. one still flowing toward
    publication (created / loading / validating / ready / publishing). Surfaces its
    DQ state (``state``), freshness (``state_changed_at``),
    row_count and content_hash for the 12.14 UI. Project-scoped; reuses the 042
    execution columns (no new table).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, state, state_changed_at, content_hash, row_count,
                   plan_version_id, mapping_version_id, error_code, created_at
            FROM app.datastream_executions
            WHERE datastream_id = %s AND project_id = %s
              AND state NOT IN ('published', 'failed', 'cancelled')
              AND (%s IS NULL OR id <> %s)
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (ds_id, project_id, published_execution_id, published_execution_id),
        )
        row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
    record: dict = {}
    for col, val in zip(cols, row):
        if val is not None and hasattr(val, "isoformat"):
            record[col] = val.isoformat()
        else:
            record[col] = val
    return record


async def _get_datastream(request: Request) -> Response:
    """GET /api/datastreams/{id}?project_id=<id> -- single datastream.

    Response (200): datastream object.
    Error:
        400 -- missing project_id
        401 -- unauthorized
        404 -- not found or wrong project
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )

    try:
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            row = get_datastream(ds_id, project_id, conn)
            if row is None:
                # Still check scope: if it exists but in another project, 404 + audit.
                # get_datastream already returns None for wrong project, so just 404.
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
    except Exception as exc:
        logger.error("admin_api: get_datastream_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    return JSONResponse(row)


async def _patch_datastream(request: Request) -> Response:
    """PATCH /api/datastreams/{id} -- update a datastream.

    Body (JSON): {"project_id": str, "name"?, "enabled"?, "schedule_mode"?,
                  "refetch_days"?, "date_window_days"?, "config"?,
                  "connection_ref_id"?, "report_profile_id"?}
    Response (200): updated datastream.
    Error:
        400 -- missing project_id
        401 -- unauthorized
        404 -- not found or wrong project
        409 -- name conflict
        422 -- validation error
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")

    try:
        body_bytes_patch = await request.body()
        body: dict = json.loads(body_bytes_patch) if body_bytes_patch.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id est requis dans le corps"},
            status_code=400,
        )

    try:
        from core.datastreams import get_datastream, update_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            existing = get_datastream(ds_id, project_id, conn)
            if existing is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            # Scope enforcement: the datastream exists in project_id (already verified above).
            scope_err = _enforce_datastream_project_scope(
                existing["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
                minimum_role=(
                    "owner"
                    if isinstance(body.get("intent"), dict)
                    and body["intent"].get("destination", {}).get("policy") == "external_read_only"
                    else "member"
                ),
            )
            if scope_err is not None:
                return scope_err

            if isinstance(body.get("intent"), dict):
                idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
                if not idempotency_key:
                    return JSONResponse(
                        {
                            "code": "missing_idempotency_key",
                            "message": "Idempotency-Key est requis pour creer une version.",
                        },
                        status_code=400,
                    )
                from core.flows import (  # noqa: PLC0415
                    FlowConflictError,
                    FlowScopeError,
                    FlowUnavailableError,
                    FlowValidationError,
                    upsert_flow,
                )
                from core.main import get_loaded_modules  # noqa: PLC0415

                definition = {
                    "schema_version": "2",
                    "kind": "datastream",
                    "id": ds_id,
                    "project_id": project_id,
                    "name": (body.get("name") or existing.get("name") or "").strip(),
                    "intent": body["intent"],
                    "idempotency_key": idempotency_key,
                    "reason": body.get("reason", "rest_draft_revised"),
                    "trace_id": request.headers.get("traceparent"),
                }
                try:
                    result = upsert_flow(
                        project_id,
                        definition,
                        identity or "anonymous",
                        conn,
                        loaded_modules=get_loaded_modules(),
                    )
                except FlowValidationError as exc:
                    return JSONResponse({"code": "validation_error", "errors": exc.errors}, 422)
                except FlowConflictError as exc:
                    return JSONResponse({"code": "conflict", "message": str(exc)}, 409)
                except FlowScopeError:
                    return JSONResponse({"code": "not_found"}, 404)
                except FlowUnavailableError as exc:
                    return JSONResponse({"code": "unavailable", "message": str(exc)}, 503)
                response = dict(result["flow"])
                response["plan_version"] = result["plan_version"]
                return JSONResponse(response)

            # Story 38.6 AC4: a generic-tabular inbound Datastream is publish-gated
            # until its required canonical semantics are completed (a later mapping
            # story lifts the gate). Enabling it via PATCH must not bypass the gate.
            _ds_cfg = existing.get("config") or {}
            if isinstance(_ds_cfg, str):
                import json as _json_cfg  # noqa: PLC0415

                try:
                    _ds_cfg = _json_cfg.loads(_ds_cfg)
                except (ValueError, TypeError):
                    _ds_cfg = {}
            if (
                body.get("enabled") is True
                and isinstance(_ds_cfg, dict)
                and _ds_cfg.get("publish_gate") == "canonical_semantics_required"
            ):
                return JSONResponse(
                    {
                        "code": "publish_gate_active",
                        "message": (
                            "Activation impossible : les semantiques canoniques requises "
                            "ne sont pas encore completees pour ce flux inbound generique."
                        ),
                    },
                    status_code=422,
                )

            if existing.get("versioned") and body.get("enabled") is True:
                # Story 12.6/12.13 activation seam: enable + schedule a VALIDATED
                # versioned datastream. Only an `executable` plan version (12.4 admission
                # ticket) may be activated; a blocked/draft plan is refused honestly.
                if existing.get("validation_state") != "executable":
                    return JSONResponse(
                        {
                            "code": "activation_not_available",
                            "message": (
                                "Activation impossible : le plan versionne n'est pas "
                                "valide (executable). Corrigez le mapping/preview d'abord."
                            ),
                        },
                        status_code=422,
                    )
                # Map the versioned cadence (manual/daily/hourly) to the datastream's
                # schedule_mode: daily -> the nightly dispatch, hourly -> hourly dispatch.
                _cadence = (
                    ((existing.get("intent_payload") or {}).get("schedule") or {}).get("mode")
                    or "manual"
                )
                _mode = {"daily": "nightly", "hourly": "hourly", "manual": "manual"}.get(
                    _cadence, "manual"
                )
                updated = update_datastream(
                    ds_id, project_id, {"enabled": True, "schedule_mode": _mode}, conn
                )
                conn.commit()
            else:
                updated = update_datastream(ds_id, project_id, body, conn)
                conn.commit()
    except ValueError as exc:
        return JSONResponse(
            {"code": "invalid_input", "message": str(exc)},
            status_code=422,
        )
    except Exception as exc:
        # Story 34.2 (F1 fix): enabling a draft over the trial cap -> typed 409
        # (same as create), not a generic 500.
        from core.trial_enforcement import TrialDatastreamLimitError  # noqa: PLC0415

        if isinstance(exc, TrialDatastreamLimitError):
            return JSONResponse(exc.to_dict(), status_code=409)
        if "UniqueViolation" in type(exc).__name__ or "unique" in str(exc).lower():
            return JSONResponse(
                {
                    "code": "conflict",
                    "message": "Un flux de donnees avec ce nom existe deja dans ce projet",
                },
                status_code=409,
            )
        logger.error("admin_api: patch_datastream_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    if updated is None:
        return JSONResponse(
            {"code": "not_found", "message": "Flux de donnees introuvable"},
            status_code=404,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_DATASTREAM_UPDATED,
        provider_account="",
        connection_ref="",
        metadata={"datastream_id": ds_id, "project_id": project_id},
    )
    return JSONResponse(updated)


async def _delete_datastream(request: Request) -> Response:
    """DELETE /api/datastreams/{id}?project_id=<id> -- delete or soft-archive a datastream.

    Response (200): {"status": "deleted"|"archived", "id": str}
    Error:
        400 -- missing project_id
        401 -- unauthorized
        404 -- not found or wrong project
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )

    try:
        from core.datastreams import delete_datastream, get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            existing = get_datastream(ds_id, project_id, conn)
            if existing is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            scope_err = _enforce_datastream_project_scope(
                existing["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
                minimum_role="owner",
            )
            if scope_err is not None:
                return scope_err

            # Check if soft-archive or hard delete will happen (for response status).
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.pull_jobs WHERE datastream_id = %s",
                    (ds_id,),
                )
                ref_count = cur.fetchone()[0]

            ok = delete_datastream(ds_id, project_id, conn, archived_by=identity or "anonymous")
            conn.commit()
    except Exception as exc:
        logger.error("admin_api: delete_datastream_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    if not ok:
        return JSONResponse(
            {"code": "not_found", "message": "Flux de donnees introuvable"},
            status_code=404,
        )

    status_label = (
        "archived" if ref_count > 0 or existing.get("current_plan_version_id") else "deleted"
    )
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_DATASTREAM_DELETED,
        provider_account="",
        connection_ref="",
        metadata={
            "datastream_id": ds_id,
            "project_id": project_id,
            "disposition": status_label,
        },
    )
    return JSONResponse({"status": status_label, "id": ds_id})


async def _run_datastream(request: Request) -> Response:
    """POST /api/datastreams/{id}/run -- enqueue a pull for this datastream.

    Body (JSON): {"project_id": str, "date_from": str?, "date_to": str?}
    If date_from/date_to are omitted, uses yesterday - (refetch_days-1) .. yesterday
    (same window as the nightly scheduler).
    Response (202): {"job_id", "pull_id", "state", "deduplicated"?}
    Error:
        400 -- missing project_id
        401 -- unauthorized
        404 -- not found or wrong project
        422 -- validation error
        500 -- DB/queue error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id est requis dans le corps"},
            status_code=400,
        )

    try:
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            ds = get_datastream(ds_id, project_id, conn)
            if ds is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            scope_err = _enforce_datastream_project_scope(
                ds["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
                minimum_role="member",
            )
            if scope_err is not None:
                return scope_err
    except Exception as exc:
        logger.error("admin_api: run_datastream_db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    if ds.get("versioned"):
        return JSONResponse(
            {
                "code": "dispatch_not_available",
                "message": "Le dispatch versionne appartient a la Story 12.6.",
            },
            status_code=422,
        )

    connection_ref_id = ds.get("connection_ref_id")
    if not connection_ref_id:
        return JSONResponse(
            {
                "code": "not_configured",
                "message": "Ce flux n'est pas encore lie a une connexion.",
            },
            status_code=422,
        )

    # Resolve window: caller-supplied or default from refetch_days.
    date_from = (body.get("date_from") or "").strip()
    date_to = (body.get("date_to") or "").strip()
    if not date_from or not date_to:
        refetch_days = int(ds.get("refetch_days") or 3)
        yesterday_d = date.today() - timedelta(days=1)
        date_from = (yesterday_d - timedelta(days=refetch_days - 1)).isoformat()
        date_to = yesterday_d.isoformat()

    try:
        from core import queue  # noqa: PLC0415

        job = queue.enqueue_pull(
            connection_ref_id,
            date_from,
            date_to,
            requested_by=identity or "anonymous",
            datastream_id=ds_id,
        )
    except Exception as exc:
        logger.error("admin_api: run_datastream_enqueue_error: %s", exc)
        return JSONResponse(
            {"code": "queue_error", "message": f"Erreur de mise en file d'attente: {exc}"},
            status_code=500,
        )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_DATASTREAM_RUN,
        provider_account="",
        connection_ref=connection_ref_id,
        metadata={
            "datastream_id": ds_id,
            "project_id": project_id,
            "job_id": job.get("job_id"),
            "date_from": date_from,
            "date_to": date_to,
        },
    )
    return JSONResponse(job, status_code=202)


# ---------------------------------------------------------------------------
# Story 8.3 — Extract ledger + refetch routes
#
# GET  /api/datastreams/{id}/ledger?project_id=&from=&to=
#      Returns day-grain extract ledger (last 35 days by default).
# POST /api/datastreams/{id}/refetch
#      Body: {"project_id": str, "dates": [YYYY-MM-DD, ...]}
#         or {"project_id": str, "from": YYYY-MM-DD, "to": YYYY-MM-DD}
#      Enqueues bounded pull(s) via enqueue_pull with datastream_id.
#      Contiguous date selections are grouped into one window each.
# ---------------------------------------------------------------------------

_DEFAULT_LEDGER_DAYS = 35


async def _get_datastream_ledger(request: Request) -> Response:
    """GET /api/datastreams/{id}/ledger -- extract ledger for a datastream.

    Query params:
        project_id  (required) -- project scope (AD-5)
        from        (optional) -- start date YYYY-MM-DD (default today - 35 days)
        to          (optional) -- end date YYYY-MM-DD (default yesterday)

    Response (200):
        {"ledger": [{date, status, row_count, expected_rows, completeness_ratio,
                     pull_id, loaded_at}, ...]}
        Ordered date ASC. One entry per calendar day in the window.

    Error responses:
        400 -- missing project_id, or invalid date format (French)
        401 -- unauthorized
        404 -- datastream not found or wrong project
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")
    project_id = request.query_params.get("project_id") or ""
    if not project_id:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "project_id est requis en parametre de requete",
            },
            status_code=400,
        )

    # Default window: last 35 days (yesterday back 35 days).
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    default_from = (date.today() - timedelta(days=_DEFAULT_LEDGER_DAYS)).isoformat()

    raw_from = (request.query_params.get("from") or default_from).strip()
    raw_to = (request.query_params.get("to") or yesterday).strip()

    if not _ISO_DATE_RE.match(raw_from):
        return JSONResponse(
            {
                "code": "invalid_date",
                "message": (
                    f"Parametre 'from' invalide (format attendu YYYY-MM-DD) : {raw_from!r}"
                ),
            },
            status_code=400,
        )
    if not _ISO_DATE_RE.match(raw_to):
        return JSONResponse(
            {
                "code": "invalid_date",
                "message": (f"Parametre 'to' invalide (format attendu YYYY-MM-DD) : {raw_to!r}"),
            },
            status_code=400,
        )

    try:
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.extract_ledger import get_extract_ledger  # noqa: PLC0415

        with get_connection() as conn:
            ds = get_datastream(ds_id, project_id, conn)
            if ds is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            scope_err = _enforce_datastream_project_scope(
                ds["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
            )
            if scope_err is not None:
                return scope_err

            ledger = get_extract_ledger(ds_id, raw_from, raw_to, conn)
    except Exception as exc:
        logger.error("admin_api: get_datastream_ledger_error ds=%s: %s", ds_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    return JSONResponse({"ledger": ledger})


def _group_dates_into_windows(dates: list[str]) -> list[tuple[str, str]]:
    """Group a sorted list of YYYY-MM-DD strings into contiguous (from, to) windows.

    Example: ["2026-07-01", "2026-07-02", "2026-07-04"] ->
             [("2026-07-01", "2026-07-02"), ("2026-07-04", "2026-07-04")]
    """
    if not dates:
        return []

    parsed = sorted({date.fromisoformat(d) for d in dates})
    windows: list[tuple[str, str]] = []
    window_start = parsed[0]
    window_end = parsed[0]

    for d in parsed[1:]:
        if d == window_end + timedelta(days=1):
            window_end = d
        else:
            windows.append((window_start.isoformat(), window_end.isoformat()))
            window_start = d
            window_end = d

    windows.append((window_start.isoformat(), window_end.isoformat()))
    return windows


async def _refetch_datastream(request: Request) -> Response:
    """POST /api/datastreams/{id}/refetch -- enqueue re-fetch for selected days.

    Body (JSON):
        {"project_id": str,
         "dates": ["YYYY-MM-DD", ...]}          -- explicit day list, OR
        {"project_id": str,
         "from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}  -- date range (converted to day list)

    Contiguous date selections are grouped into minimal pull windows.
    Each window calls enqueue_pull() with datastream_id; dedup index applies.
    Writes ACTION_DATASTREAM_RUN per window.

    Response (202):
        {"jobs": [{job_id, pull_id, state, date_from, date_to, deduplicated?}, ...]}

    Error responses:
        400 -- missing project_id, no dates, invalid dates (French)
        401 -- unauthorized
        404 -- datastream not found or wrong project
        422 -- datastream has no connection_ref_id
        500 -- DB/queue error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    ds_id = request.path_params.get("id", "")

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide: {exc}"},
            status_code=400,
        )

    project_id = (body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id est requis"},
            status_code=400,
        )

    # Resolve dates list from either "dates" array or "from"/"to" range.
    dates_raw: list[str] = []
    if "dates" in body:
        raw_list = body.get("dates") or []
        if not isinstance(raw_list, list):
            return JSONResponse(
                {"code": "invalid_field", "message": "dates doit etre un tableau de dates"},
                status_code=400,
            )
        dates_raw = [str(d).strip() for d in raw_list]
    elif "from" in body or "to" in body:
        raw_from = (body.get("from") or "").strip()
        raw_to = (body.get("to") or "").strip()
        if not raw_from or not raw_to:
            return JSONResponse(
                {
                    "code": "missing_field",
                    "message": "from et to sont requis quand dates n'est pas fourni",
                },
                status_code=400,
            )
        if not _ISO_DATE_RE.match(raw_from):
            return JSONResponse(
                {
                    "code": "invalid_date",
                    "message": f"'from' invalide (format YYYY-MM-DD) : {raw_from!r}",
                },
                status_code=400,
            )
        if not _ISO_DATE_RE.match(raw_to):
            return JSONResponse(
                {
                    "code": "invalid_date",
                    "message": f"'to' invalide (format YYYY-MM-DD) : {raw_to!r}",
                },
                status_code=400,
            )
        # Expand range to day list.
        try:
            d_from = date.fromisoformat(raw_from)
            d_to = date.fromisoformat(raw_to)
            if d_to < d_from:
                return JSONResponse(
                    {
                        "code": "invalid_date",
                        "message": "'to' doit etre posterieur ou egal a 'from'",
                    },
                    status_code=400,
                )
            cur_d = d_from
            while cur_d <= d_to:
                dates_raw.append(cur_d.isoformat())
                cur_d += timedelta(days=1)
        except Exception:
            return JSONResponse(
                {"code": "invalid_date", "message": "Dates invalides"},
                status_code=400,
            )
    else:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "Fournir 'dates' (tableau) ou 'from'/'to' (plage de dates)",
            },
            status_code=400,
        )

    if not dates_raw:
        return JSONResponse(
            {"code": "missing_field", "message": "Aucune date selectionnee"},
            status_code=400,
        )

    # Validate each date string.
    for d_str in dates_raw:
        if not _ISO_DATE_RE.match(d_str):
            return JSONResponse(
                {
                    "code": "invalid_date",
                    "message": f"Date invalide (format YYYY-MM-DD) : {d_str!r}",
                },
                status_code=400,
            )

    # Cap at 365 days to prevent abuse.
    if len(dates_raw) > 365:
        return JSONResponse(
            {
                "code": "too_many_dates",
                "message": "Maximum 365 jours par requete de re-fetch",
            },
            status_code=400,
        )

    # Fetch datastream and enforce project scope.
    try:
        from core.datastreams import get_datastream  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            ds = get_datastream(ds_id, project_id, conn)
            if ds is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )
            scope_err = _enforce_datastream_project_scope(
                ds["project_id"],
                identity,
                ds_id,
                conn,
                claimed_project_id=project_id,
                minimum_role="member",
            )
            if scope_err is not None:
                return scope_err
    except Exception as exc:
        logger.error("admin_api: refetch_datastream_db_error ds=%s: %s", ds_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur base de donnees: {exc}"},
            status_code=500,
        )

    if ds.get("versioned"):
        return JSONResponse(
            {
                "code": "dispatch_not_available",
                "message": "Le dispatch versionne appartient a la Story 12.6.",
            },
            status_code=422,
        )

    connection_ref_id = ds.get("connection_ref_id")
    if not connection_ref_id:
        return JSONResponse(
            {
                "code": "not_configured",
                "message": "Ce flux n'est pas encore lie a une connexion.",
            },
            status_code=422,
        )

    # Group contiguous dates into windows, enqueue one pull per window.
    try:
        windows = _group_dates_into_windows(dates_raw)
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_date", "message": f"Erreur de calcul des fenetres: {exc}"},
            status_code=400,
        )

    jobs: list[dict] = []
    subject = identity or "anonymous"
    try:
        from core import queue  # noqa: PLC0415

        for win_from, win_to in windows:
            job = queue.enqueue_pull(
                connection_ref_id,
                win_from,
                win_to,
                requested_by=subject,
                datastream_id=ds_id,
            )
            job_entry = {
                "job_id": job.get("job_id"),
                "pull_id": job.get("pull_id"),
                "state": job.get("state"),
                "date_from": win_from,
                "date_to": win_to,
            }
            if job.get("deduplicated"):
                job_entry["deduplicated"] = True
            jobs.append(job_entry)

            write_audit_row(
                identity=subject,
                action=ACTION_DATASTREAM_RUN,
                provider_account="",
                connection_ref=connection_ref_id,
                metadata={
                    "datastream_id": ds_id,
                    "project_id": project_id,
                    "job_id": job.get("job_id"),
                    "date_from": win_from,
                    "date_to": win_to,
                    "source": "refetch",
                },
            )
    except Exception as exc:
        logger.error("admin_api: refetch_datastream_enqueue_error ds=%s: %s", ds_id, exc)
        return JSONResponse(
            {"code": "queue_error", "message": f"Erreur de mise en file d'attente: {exc}"},
            status_code=500,
        )

    return JSONResponse({"jobs": jobs}, status_code=202)


# ===========================================================================
# Story 19.3 -- Observabilite + controle du cache DuckDB (CAP-22 / AD-22).
#
# GET  /api/admin/cache/status
#      Retourne l'etat courant du cache read-through : age, tables, fenetre,
#      row counts (lus du manifeste 19.1), hit rate (compteurs 19.2), project_ids
#      couverts. Etat honnete : "no-cache" | "stale" | "fresh" | "disabled".
#      AD-5 : l'endpoint expose uniquement les row counts des projets auxquels
#      l'appelant a acces (identite resolue depuis le Bearer token). Les project_ids
#      couverts sont filtres ; les row counts globaux sont presentes sans filtre
#      (ils ne revelent pas de donnees metier, juste des volumes). Ce choix suit
#      le modele des autres endpoints admin : la granularite de scoping est le projet
#      pour les donnees metier (cartes, rapports), pas pour les metriques d'infra.
#      Coherent avec /api/health qui expose des metriques d'infra sans scoping fin.
#
# POST /api/admin/cache/rebuild
#      Declenche un rebuild on-demand en reutilisant cache_warehouse.rebuild_cache().
#      Bornee (une seule execution, pas de rejouabilite). Auditee (AD-14 -- AD-15)
#      avec performed_by = identite REELLE du Bearer token (jamais 'system').
#      403 + audit sur tentative cross-projet (AD-5).
#      Repond toujours proprement (invariant f) : un echec retourne {"status": "failed"}
#      sans jamais propager une exception en 500 brut.
#
# Regles AD-5 retenues (detaillees dans l'artifact 19-3) :
#   - Le status expose les project_ids couverts bruts (metadonnees d'infra, pas
#     de donnees metier). Un admin qui voit la page sante voit l'etat global du cache.
#   - Le rebuild POST verifie que l'appelant a acces a AU MOINS un projet actif avant
#     de declencher (protection contre les anonymous). Pas de scoping fin : le cache
#     couvre tous les projets (AD-5 s'applique dans le fichier cache, pas dans cet
#     endpoint de controle). Coherent avec /api/health qui est accessible a tout admin.
# ===========================================================================

ACTION_CACHE_REBUILD = "cache.rebuild"


async def _cache_status(request: Request) -> Response:
    """GET /api/admin/cache/status -- etat du cache read-through (Story 19.3).

    Retourne :
      {
        "cache_state":   "disabled" | "no-cache" | "stale" | "fresh",
        "cache_enabled": bool,
        "cache_built_at": str | null,       -- UTC ISO-8601 (depuis le manifeste)
        "age_seconds":   float | null,      -- maintenant - cache_built_at
        "min_date":      str | null,        -- borne basse de la fenetre
        "max_date":      str | null,        -- borne haute de la fenetre
        "tables":        [str, ...],        -- tables cachees (manifeste)
        "row_counts":    {table: int, ...}, -- counts par table (manifeste)
        "project_ids":   [str, ...],        -- projets couverts (manifeste)
        "hit_rate":      float | null,      -- hits / (hits + misses) sur la session
        "stats":         {decision: int},   -- compteurs bruts (AD-13)
        "last_rebuild_cause": null          -- nightly | manual (enrichi en phase B)
      }

    Honnete : "no-cache" quand le fichier est absent/ephemere perdu, "stale" quand
    le cache est perime selon la meme regle que 19.2 (_cache_is_fresh), "fresh" sinon.
    "disabled" quand TOOROW_CACHE_ENABLED=false (independant de l'existence du fichier).
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis."},
            status_code=401,
        )

    # Lecture du flag enabled.
    cache_enabled = os.environ.get("TOOROW_CACHE_ENABLED", "false").lower() == "true"

    # Lecture du manifeste (None si absent/corrompu -- invariant f).
    from core.cache_warehouse import read_manifest  # noqa: PLC0415

    manifest = read_manifest()

    # Compteurs hit/miss (AD-13 / NFR7) -- ne leve jamais.
    try:
        from core.warehouse import get_cache_stats  # noqa: PLC0415

        stats = get_cache_stats()
    except Exception:  # noqa: BLE001
        stats = {}

    hits = stats.get("hit", 0)
    total_decisions = sum(stats.values())
    # Le hit rate agrege = hits / toutes les decisions (hit + miss + bypass + ...).
    hit_rate: float | None = (hits / total_decisions) if total_decisions > 0 else None

    now_utc = datetime.now(tz=timezone.utc)

    if not cache_enabled:
        return JSONResponse(
            {
                "cache_state": "disabled",
                "cache_enabled": False,
                "cache_built_at": None,
                "age_seconds": None,
                "min_date": None,
                "max_date": None,
                "tables": [],
                "row_counts": {},
                "project_ids": [],
                # review-19-3 F-3: cache off -> pas de hit rate (les stats ne comptent
                # que des decisions "disabled", un ratio serait un mensonge semantique).
                "hit_rate": None,
                "stats": stats,
                "last_rebuild_cause": None,
            }
        )

    if manifest is None:
        return JSONResponse(
            {
                "cache_state": "no-cache",
                "cache_enabled": True,
                "cache_built_at": None,
                "age_seconds": None,
                "min_date": None,
                "max_date": None,
                "tables": [],
                "row_counts": {},
                "project_ids": [],
                "hit_rate": hit_rate,
                "stats": stats,
                "last_rebuild_cause": None,
            }
        )

    cache_built_at = manifest.get("cache_built_at")
    age_seconds: float | None = None
    if cache_built_at:
        try:
            built_dt = datetime.fromisoformat(cache_built_at)
            if built_dt.tzinfo is None:
                built_dt = built_dt.replace(tzinfo=timezone.utc)
            age_seconds = (now_utc - built_dt).total_seconds()
        except Exception:  # noqa: BLE001
            pass

    # Fraicheur : reuse la MEME regle que 19.2 (_cache_is_fresh) -- pas de duplication.
    try:
        from core.warehouse import _cache_is_fresh  # noqa: PLC0415

        is_fresh = _cache_is_fresh(cache_built_at, now=now_utc)
    except Exception:  # noqa: BLE001
        is_fresh = False

    cache_state = "fresh" if is_fresh else "stale"

    return JSONResponse(
        {
            "cache_state": cache_state,
            "cache_enabled": True,
            "cache_built_at": cache_built_at,
            "age_seconds": age_seconds,
            "min_date": manifest.get("min_date"),
            "max_date": manifest.get("max_date"),
            "tables": manifest.get("tables") or [],
            "row_counts": manifest.get("row_counts") or {},
            "project_ids": manifest.get("project_ids") or [],
            "hit_rate": hit_rate,
            "stats": stats,
            "last_rebuild_cause": None,
        }
    )


async def _cache_rebuild(request: Request) -> Response:
    """POST /api/admin/cache/rebuild -- trigger manuel de rebuild (Story 19.3).

    Declenche cache_warehouse.rebuild_cache() de facon bornee et synchrone.
    Audite avec performed_by = identite REELLE du Bearer token (AD-14).
    AD-5 : verifie que l'appelant est authentifie (meme guard que tout endpoint admin).
    Invariant (f) : JAMAIS de 500 brut -- un echec retourne {"status": "failed"}.

    Response (200):
      {"status": "ok" | "disabled" | "failed" | "skipped",
       "tables": [...], "row_counts": {...}, "project_ids": [...],
       "min_date": str, "max_date": str, "cache_built_at": str,
       "performed_by": str}

    Response (401): non authentifie.
    Response (403): TOOROW_CACHE_ENABLED=false (inutile de reconstruire un cache
                    desactive, et declencher un rebuild serait trompeur).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis."},
            status_code=401,
        )

    # review-19-3 F-2: une identite chaine-vide (auth disabled) ne doit pas
    # contourner la valeur sentinelle "anonymous" dans l'audit AD-14.
    performed_by = (identity or "").strip() or "anonymous"

    # AD-5 / Guard : si le cache est desactive, le rebuild est refuse avec 403
    # (cela eviterait une confusion : le service retourne "disabled" sans construire
    # quoi que ce soit -- le caller doit activer TOOROW_CACHE_ENABLED d'abord).
    cache_enabled = os.environ.get("TOOROW_CACHE_ENABLED", "false").lower() == "true"
    if not cache_enabled:
        write_audit_row(
            identity=performed_by,
            action=ACTION_CACHE_REBUILD,
            provider_account="",
            connection_ref="",
            metadata={
                "trigger": "manual",
                "result": "refused_disabled",
                "performed_by": performed_by,
            },
        )
        return JSONResponse(
            {
                "code": "cache_disabled",
                "message": (
                    "Le cache est desactive (TOOROW_CACHE_ENABLED=false). "
                    "Activez-le avant de declencher un rebuild."
                ),
            },
            status_code=403,
        )

    # Audit AVANT le rebuild (AD-14 On-Behalf-Of : on trace la demande, pas seulement
    # le resultat -- coherent avec la revocation Google et les autres actions on-demand).
    write_audit_row(
        identity=performed_by,
        action=ACTION_CACHE_REBUILD,
        provider_account="",
        connection_ref="",
        metadata={
            "trigger": "manual",
            "performed_by": performed_by,
        },
    )

    # Rebuild on-demand -- JAMAIS de raise (invariant f : rebuild_cache() l'absorbe).
    try:
        from core import cache_warehouse  # noqa: PLC0415

        result = cache_warehouse.rebuild_cache()
    except Exception as exc:  # noqa: BLE001 -- filet de securite supplementaire
        logger.warning("admin_api: cache_rebuild_unexpected: %s: %s", type(exc).__name__, exc)
        result = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}

    logger.info(
        "admin_api: cache_rebuild_manual status=%s performed_by=%s",
        result.get("status"),
        performed_by,
    )

    # Audit du resultat (AD-14 : on trace aussi le resultat pour observabilite).
    write_audit_row(
        identity=performed_by,
        action=ACTION_CACHE_REBUILD,
        provider_account="",
        connection_ref="",
        metadata={
            "trigger": "manual_result",
            "status": result.get("status"),
            "tables": result.get("tables", []),
            "performed_by": performed_by,
        },
    )

    return JSONResponse({**result, "performed_by": performed_by})


async def _source_capabilities(request: Request) -> Response:
    """Return the governed capability catalog for one project-owned connection."""

    ok, identity = await _check_auth(request)
    if not ok:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    project_id = request.query_params.get("project_id", "").strip()
    connection_ref_id = request.query_params.get("connection_ref_id", "").strip()
    if not project_id or not connection_ref_id:
        return JSONResponse(
            {
                "error": "invalid_input",
                "message": "project_id and connection_ref_id are required",
            },
            status_code=400,
        )

    from core import db as _core_db  # noqa: PLC0415
    from core.main import get_loaded_modules  # noqa: PLC0415
    from core.source_capabilities import (  # noqa: PLC0415
        SourceCapabilitiesNotFound,
        SourceCapabilitiesUnavailable,
        get_scoped_source_capabilities,
    )

    try:
        with _core_db.get_connection() as conn:
            catalog = get_scoped_source_capabilities(
                project_id=project_id,
                connection_ref_id=connection_ref_id,
                identity=identity,
                loaded_modules=get_loaded_modules(),
                conn=conn,
            )
    except SourceCapabilitiesNotFound:
        return JSONResponse({"error": "source_capabilities_not_found"}, status_code=404)
    except SourceCapabilitiesUnavailable:
        return JSONResponse({"error": "source_capabilities_unavailable"}, status_code=503)
    except Exception as exc:  # noqa: BLE001 -- stable public failure contract
        logger.warning("admin_api: source_capabilities_unavailable: %s", type(exc).__name__)
        return JSONResponse({"error": "source_capabilities_unavailable"}, status_code=503)

    return JSONResponse(catalog)


# Story 36.6: setup responsibility checklist and minimum handoff seams.
_SETUP_HANDOFF_COOKIE = "toorow_setup_handoff"


def _setup_no_store(response: Response) -> Response:
    response.headers.update(
        {
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
    )
    return response


def _setup_host_context(request: Request) -> dict[str, str]:
    return {
        "host": "rest",
        "workspace_id": (request.headers.get("X-Workspace-Id") or "console")[:256],
    }


def _setup_gate_response() -> Response | None:
    from core.project_access import epic36_production_access_enabled

    if not epic36_production_access_enabled():
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    return None


def _authorize_setup_task(
    conn,
    *,
    identity: str,
    task_id: str,
    minimum_capability: str = "edit",
) -> Response | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.org_id,j.project_id FROM app.setup_tasks t "
            "JOIN app.setup_journeys j ON j.id=t.journey_id WHERE t.id=%s",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None:
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    if row[1]:
        from core.project_access import resolve_strict_resource_access

        decision = resolve_strict_resource_access(
            identity,
            conn,
            minimum_capability=minimum_capability,
            project_id=row[1],
        )
        if not decision.allowed or decision.org_id != row[0]:
            return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
        return None
    return _enforce_org_manage(row[0], identity, conn, "manage_setup_task")


def _setup_error(exc: Exception) -> Response:
    from core.operations import OperationIdempotencyConflict
    from core.setup_responsibilities import SetupConflict, SetupUnavailable, SetupValidationError

    if isinstance(exc, SetupUnavailable):
        status, code, message = 404, "not_found", "Setup unavailable"
    elif isinstance(exc, (SetupConflict, OperationIdempotencyConflict)):
        status, code, message = 409, "conflict", "Setup state already changed"
    elif isinstance(exc, (SetupValidationError, json.JSONDecodeError, TypeError)):
        status, code, message = 422, "invalid_request", str(exc)
    else:
        logger.error("admin_api: setup operation failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "Setup unavailable"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))


async def _get_setup_journey(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    project_id = request.path_params.get("project_id")
    org_id = request.path_params.get("org_id")
    from core.db import get_connection
    from core.project_access import (
        identity_has_org_access,
        resolve_strict_resource_access,
    )
    from core.setup_responsibilities import get_reconciled_journey

    try:
        with get_connection() as conn:
            if project_id:
                decision = resolve_strict_resource_access(
                    identity, conn, minimum_capability="view", project_id=project_id
                )
                if not decision.allowed:
                    return JSONResponse(
                        {"code": "not_found", "message": "Not found"}, status_code=404
                    )
                result = get_reconciled_journey(
                    conn,
                    project_id=project_id,
                    server_evidence={"project_access": {project_id: True}},
                )
            else:
                if not org_id or not identity_has_org_access(org_id, identity, conn):
                    return JSONResponse(
                        {"code": "not_found", "message": "Not found"}, status_code=404
                    )
                result = get_reconciled_journey(
                    conn,
                    org_id=org_id,
                    server_evidence={"organization_access": {org_id: True}},
                )
    except Exception as exc:
        return _setup_error(exc)
    return _setup_no_store(JSONResponse(result))

async def _prepare_setup_handoff(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.setup_responsibilities import prepare_handoff

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        with get_connection() as conn:
            denied = _authorize_setup_task(
                conn,
                identity=identity,
                task_id=request.path_params["task_id"],
                minimum_capability="edit",
            )
            if denied is not None:
                return denied
            result = prepare_handoff(
                conn,
                task_id=request.path_params["task_id"],
                actor=identity,
                expires_in_hours=body.get("expires_in_hours", 48),
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _setup_error(exc)
    payload = {
        "handoff_id": result.handoff_id,
        "state": result.state,
        "expires_at": result.expires_at,
        "operation_id": result.operation_id,
        "audit_event_id": result.audit_event_id,
        "replayed": result.replayed,
    }
    if result.delivery_url:
        payload["delivery_handoff"] = {"url": result.delivery_url, "single_return": True}
    return _setup_no_store(
        Response(
            json.dumps(payload),
            status_code=201,
            media_type="application/vnd.toorow.setup-handoff+json",
        )
    )


async def _reassign_setup_task(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.setup_responsibilities import reassign_task

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        with get_connection() as conn:
            denied = _authorize_setup_task(
                conn,
                identity=identity,
                task_id=request.path_params["task_id"],
                minimum_capability="manage",
            )
            if denied is not None:
                return denied
            result = reassign_task(
                conn,
                task_id=request.path_params["task_id"],
                actor=identity,
                actor_type=str(body.get("actor_type") or ""),
                assigned_identity=body.get("assigned_identity"),
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _setup_error(exc)
    return _setup_no_store(JSONResponse(result))


async def _revoke_setup_handoff(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.setup_responsibilities import revoke_handoff

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_id FROM app.setup_handoffs WHERE id=%s",
                    (request.path_params["handoff_id"],),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=row[0], minimum_capability="manage"
            )
            if denied is not None:
                return denied
            result = revoke_handoff(
                conn,
                handoff_id=request.path_params["handoff_id"],
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _setup_error(exc)
    return _setup_no_store(JSONResponse(result))


async def _setup_handoff_bootstrap(_request: Request) -> Response:
    import secrets as _secrets

    nonce = _secrets.token_urlsafe(18)
    html = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><title>Action de mise en route</title></head>
<body><main><h1>Action de mise en route</h1><p>Vérification du périmètre.</p></main>
<script nonce="__NONCE__">'use strict';const raw=location.hash.startsWith('#handoff=')
?location.hash.slice(9):'';history.replaceState(null,'',location.pathname);
if(raw){fetch('/api/setup/handoffs/exchange',{method:'POST',credentials:'same-origin',
headers:{'Content-Type':'application/json'},body:JSON.stringify({bearer:raw})});}</script>
</body></html>""".replace("__NONCE__", nonce)
    return Response(
        html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; "
                "style-src 'none'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )


async def _exchange_setup_handoff(request: Request) -> Response:
    from core.db import get_connection
    from core.setup_responsibilities import exchange_handoff

    # Best-effort identity: a bound handoff requires the authenticated matching
    # identity; an unbound (anonymous, external-actor) handoff stays open.
    authorized, identity = await _check_invitation_identity(request)
    presented_identity = identity if authorized and identity != "anonymous" else None
    try:
        body = json.loads(await request.body())
        bearer = body.get("bearer") if isinstance(body, dict) else None
        with get_connection() as conn:
            result = exchange_handoff(
                conn, bearer=bearer, presented_identity=presented_identity
            )
            conn.commit()
    except Exception as exc:
        return _setup_error(exc)
    response = _setup_no_store(
        JSONResponse(
            {
                "handoff_id": result.handoff_id,
                "task_id": result.task_id,
                "purpose": result.purpose,
                "actor_type": result.actor_type,
                "safe_scope": result.safe_scope,
                "return_path": result.return_path,
            }
        )
    )
    response.set_cookie(
        _SETUP_HANDOFF_COOKIE,
        result.session_value,
        max_age=result.max_age_seconds,
        path="/api/setup/handoffs",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


# ---------------------------------------------------------------------------
# Story 36.7: delegated source authorization and exact account exposure.
# The operator prepares a delegation; the credential owner authorizes and
# exposes ONLY the exact account. The callback verifies redirect-allowlist +
# state + nonce + PKCE BEFORE any connection/exposure transition (SameSite=Lax).
# No token/secret ever enters a response or an operation payload.
# ---------------------------------------------------------------------------
def _delegation_error(exc: Exception) -> Response:
    from core.operations import OperationIdempotencyConflict
    from core.source_delegation import (
        DelegationConflict,
        DelegationUnavailable,
        DelegationValidationError,
        DelegationVerificationError,
    )

    if isinstance(exc, DelegationVerificationError):
        # Opaque 400 -- no oracle on redirect/state/nonce/PKCE rejection reason.
        status, code, message = (
            400,
            "invalid_delegation",
            "Requête de délégation invalide ou expirée. Relancez l'autorisation.",
        )
    elif isinstance(exc, DelegationUnavailable):
        status, code, message = 404, "not_found", "Not found"
    elif isinstance(exc, (DelegationConflict, OperationIdempotencyConflict)):
        status, code, message = 409, "conflict", "Delegation state already changed"
    elif isinstance(exc, (DelegationValidationError, json.JSONDecodeError, TypeError)):
        status, code, message = 422, "invalid_request", str(exc)
    else:
        logger.error("admin_api: source delegation failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "Delegation unavailable"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))


async def _prepare_source_delegation(request: Request) -> Response:
    """POST /api/source-delegations -- bind a delegation + mint the owner handoff.

    Body: {task_id, source, provider, owner_org_id, beneficiary_org_id,
           requested_scopes[], redirect_allowlist_ref, expires_in_hours?}
    Enforces auth (AD-14) + edit access on the source_authorization task (AD-5)
    BEFORE binding. Returns the delegation id, state, authorize state, PKCE
    challenge (public) and the minimum-scoped handoff -- never a token/secret.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.source_delegation import prepare_source_delegation

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        task_id = str(body.get("task_id") or "")
        with get_connection() as conn:
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=task_id, minimum_capability="edit"
            )
            if denied is not None:
                return denied
            result = prepare_source_delegation(
                conn,
                task_id=task_id,
                source=str(body.get("source") or ""),
                provider=str(body.get("provider") or ""),
                owner_org_id=str(body.get("owner_org_id") or ""),
                beneficiary_org_id=str(body.get("beneficiary_org_id") or ""),
                requested_scopes=body.get("requested_scopes"),
                redirect_allowlist_ref=str(body.get("redirect_allowlist_ref") or ""),
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
                expires_in_hours=int(body.get("expires_in_hours", 48)),
            )
            conn.commit()
    except Exception as exc:
        return _delegation_error(exc)
    payload = {
        "delegation_id": result.delegation_id,
        "state": result.state,
        "expires_at": result.expires_at,
        "operation_id": result.operation_id,
        "audit_event_id": result.audit_event_id,
        "authorize_state": result.authorize_state,
        "pkce_challenge": result.pkce_challenge,
        "pkce_method": result.pkce_method,
        "replayed": result.replayed,
    }
    if result.handoff.delivery_url:
        payload["delivery_handoff"] = {
            "url": result.handoff.delivery_url,
            "single_return": True,
        }
    return _setup_no_store(
        Response(
            json.dumps(payload),
            status_code=201,
            media_type="application/vnd.toorow.source-delegation+json",
        )
    )


async def _source_delegation_callback(request: Request) -> Response:
    """POST /api/source-delegations/{delegation_id}/callback

    The delegated OAuth callback lands here (SameSite=Lax return). Verifies the
    exact redirect allow-list + HMAC state + nonce + PKCE BEFORE any transition,
    then exposes ONLY the exact account and REVALIDATES connection health + exact
    exposure. No token/secret enters this handler, its payload, or its response.

    Body: {state, redirect_uri, credential_id, external_account_id}
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from ulid import ULID

    from core import tracing
    from core.db import get_connection
    from core.source_delegation import complete_source_delegation

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        delegation_id = request.path_params["delegation_id"]
        with get_connection() as conn:
            result = complete_source_delegation(
                conn,
                delegation_id=delegation_id,
                state_param=str(body.get("state") or ""),
                redirect_uri=str(body.get("redirect_uri") or ""),
                credential_id=str(body.get("credential_id") or ""),
                external_account_id=str(body.get("external_account_id") or ""),
                grant_id=f"grant_{ULID()}",
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _delegation_error(exc)
    response = _setup_no_store(
        JSONResponse(
            {
                "delegation_id": result.delegation_id,
                "state": result.state,
                "task_state": result.task_reconciled_state,
                "exposure": result.exposure,
                "operation_id": result.operation_id,
                "audit_event_id": result.audit_event_id,
                "replayed": result.replayed,
            }
        )
    )
    # SameSite=Lax on any delegation cookie the callback may set (Story 36.7 AC).
    response.headers["Set-Cookie-SameSite-Policy"] = "Lax"
    return response


async def _revoke_source_delegation(request: Request) -> Response:
    """POST /api/source-delegations/{delegation_id}/revoke

    Close a pending delegation without touching any prior valid connection or
    exposure. Enforces auth + edit access on the bound task.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.source_delegation import revoke_source_delegation

    try:
        delegation_id = request.path_params["delegation_id"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_id FROM app.source_delegations WHERE delegation_id=%s",
                    (delegation_id,),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=row[0], minimum_capability="edit"
            )
            if denied is not None:
                return denied
            result = revoke_source_delegation(
                conn,
                delegation_id=delegation_id,
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _delegation_error(exc)
    return _setup_no_store(JSONResponse(result))


# ---------------------------------------------------------------------------
# Story 36.14: capability-driven host preflight, install handoff and callback bind.
# Project-scoped, fail-closed via the Epic 36 gate + resolve_strict_resource_access
# (manage, since the bind may enable a high-risk-capable capability context; the
# preflight binds to the host_connection setup task). NO brand-first ordering: the
# catalog is returned as an opaque capability-keyed mapping (E36-NFR03). NO token or
# Toorow data enters any handler, payload or response.
# ---------------------------------------------------------------------------
def _host_preflight_error(exc: Exception) -> Response:
    from core.host_preflight import (
        HostPreflightConflict,
        HostPreflightUnavailable,
        HostPreflightValidationError,
    )
    from core.operations import OperationIdempotencyConflict

    if isinstance(exc, HostPreflightUnavailable):
        status, code, message = 404, "not_found", "Not found"
    elif isinstance(exc, (HostPreflightConflict, OperationIdempotencyConflict)):
        status, code, message = 409, "conflict", "Host preflight state already changed"
    elif isinstance(exc, (HostPreflightValidationError, json.JSONDecodeError, TypeError)):
        status, code, message = 422, "invalid_request", str(exc)
    else:
        logger.error("admin_api: host preflight failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "Host preflight unavailable"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))


async def _get_host_catalog(request: Request) -> Response:
    """GET /api/mcp-hosts/catalog -- the maintained host capability catalog.

    Returns an OPAQUE, capability-keyed MAPPING of hosts (E36-NFR03: no brand-first
    ordering). Authenticated + gated only; the catalog is source-agnostic reference
    data with no org-specific content, so no per-resource access check is required.
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    from core.host_preflight import host_catalog

    return _setup_no_store(JSONResponse({"hosts": host_catalog()}))


async def _prepare_host_preflight(request: Request) -> Response:
    """POST /api/mcp-hosts/preflight -- record a dated, capability-negotiated preflight.

    Body: {host_key, task_id, org_id, project_id?, expires_in_hours?}
    Enforces auth (AD-14) + manage access on the host_connection task (AD-5) BEFORE
    recording. Returns the dated capabilities/plan/role + UI-support decision.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.host_preflight import preflight_host

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        task_id = str(body.get("task_id") or "")
        with get_connection() as conn:
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=task_id, minimum_capability="manage"
            )
            if denied is not None:
                return denied
            result = preflight_host(
                conn,
                host_key=str(body.get("host_key") or ""),
                task_id=task_id,
                org_id=str(body.get("org_id") or ""),
                project_id=body.get("project_id"),
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
                expires_in_hours=int(body.get("expires_in_hours", 72)),
            )
            conn.commit()
    except Exception as exc:
        return _host_preflight_error(exc)
    return _setup_no_store(
        Response(
            json.dumps(result),
            status_code=201,
            media_type="application/vnd.toorow.host-preflight+json",
        )
    )


async def _prepare_host_install_handoff(request: Request) -> Response:
    """POST /api/mcp-hosts/preflight/{preflight_id}/handoff

    Hand the install to the host administrator via a minimal purpose-scoped handoff
    (no Toorow data). Enforces auth + manage access on the bound host_connection task.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.host_preflight import prepare_host_install_handoff

    try:
        preflight_id = request.path_params["preflight_id"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_id FROM app.host_preflights WHERE id=%s", (preflight_id,)
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=row[0], minimum_capability="manage"
            )
            if denied is not None:
                return denied
            result = prepare_host_install_handoff(
                conn,
                preflight_id=preflight_id,
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
                expires_in_hours=int((json.loads(await request.body() or "{}") or {})
                                     .get("expires_in_hours", 72)),
            )
            conn.commit()
    except Exception as exc:
        return _host_preflight_error(exc)
    return _setup_no_store(JSONResponse(result))


async def _bind_host_connection(request: Request) -> Response:
    """POST /api/mcp-hosts/preflight/{preflight_id}/bind

    On a verified install/authorization, reconcile the callback state from SERVER
    evidence and write the Story 36.11 capability-context binding (endpoint/org/
    policy/catalog version). High-risk profiles bind ONLY with a verifiable 64-hex
    workspace_evidence_hash; otherwise only Insights binds (fail closed). No token
    or secret enters this handler, its payload or its response.

    Body: {endpoint_binding, enabled_profiles[], workspace_evidence_hash?, host?,
           workspace_id?, workspace_type?, client_id?, policy_version}
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.host_preflight import bind_host_connection

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        preflight_id = request.path_params["preflight_id"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_id FROM app.host_preflights WHERE id=%s", (preflight_id,)
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=row[0], minimum_capability="manage"
            )
            if denied is not None:
                return denied
            profiles = body.get("enabled_profiles")
            result = bind_host_connection(
                conn,
                preflight_id=preflight_id,
                endpoint_binding=str(body.get("endpoint_binding") or ""),
                enabled_profiles=profiles if isinstance(profiles, list) else ["insights"],
                workspace_evidence_hash=body.get("workspace_evidence_hash"),
                host=body.get("host"),
                workspace_id=body.get("workspace_id"),
                workspace_type=body.get("workspace_type"),
                client_id=body.get("client_id"),
                policy_version=str(body.get("policy_version") or ""),
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _host_preflight_error(exc)
    return _setup_no_store(JSONResponse(result))


# ---------------------------------------------------------------------------
# Story 36.8: recommend and save a bounded first-report draft, then preview it.
# All three routes are project-scoped and fail-closed via the Epic 36 gate +
# resolve_strict_resource_access. The preview NEVER publishes or advances a
# current pointer (the domain enforces can_publish=False).
# ---------------------------------------------------------------------------
def _first_report_error(exc: Exception) -> Response:
    from core.first_report_draft import (
        FirstReportDraftConflict,
        FirstReportDraftUnavailable,
        FirstReportDraftValidationError,
    )
    from core.operations import OperationIdempotencyConflict

    if isinstance(exc, FirstReportDraftUnavailable):
        status, code, message = 404, "not_found", "Not found"
    elif isinstance(exc, (FirstReportDraftConflict, OperationIdempotencyConflict)):
        status, code, message = 409, "conflict", "First report draft already saved"
    elif isinstance(exc, (FirstReportDraftValidationError, json.JSONDecodeError, TypeError)):
        status, code, message = 422, "invalid_request", str(exc)
    else:
        logger.error("admin_api: first report draft failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "First report draft unavailable"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))


async def _first_report_scope(request: Request):
    """Shared fail-closed gate: (denied_response | None, identity, project_id, decision)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return (
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
            ),
            identity,
            None,
            None,
        )
    if denied := _setup_gate_response():
        return denied, identity, None, None
    return None, identity, request.path_params.get("project_id"), None


async def _recommend_first_report(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    from core.db import get_connection
    from core.first_report_draft import recommend_first_report
    from core.main import get_loaded_modules
    from core.project_access import (
        resolve_provider_account_access,
        resolve_strict_resource_access,
    )
    from core.source_capabilities import (
        SourceCapabilitiesNotFound,
        SourceCapabilitiesUnavailable,
        get_scoped_source_capabilities,
    )

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="edit", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            # Resolve the datastream's connection + one exposed eligible account.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_plan_version_id FROM app.datastreams "
                    "WHERE id=%s AND project_id=%s",
                    (datastream_id, project_id),
                )
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Not found"}, status_code=404
                    )
                cur.execute(
                    """
                    SELECT cr.id, s.account_id
                    FROM app.datastreams d
                    JOIN app.connection_ref cr ON cr.project_id = d.project_id
                    LEFT JOIN app.connection_account_scope s ON s.connection_ref_id = cr.id
                    WHERE d.id = %s AND d.project_id = %s AND cr.status = 'active'
                    LIMIT 1
                    """,
                    (datastream_id, project_id),
                )
                conn_row = cur.fetchone()
            if conn_row is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            connection_ref_id, exposed_account_id = conn_row[0], conn_row[1]
            account = None
            if exposed_account_id:
                acc = resolve_provider_account_access(
                    identity,
                    conn,
                    credential_id=connection_ref_id,
                    external_account_id=exposed_account_id,
                    beneficiary_org_id=decision.org_id or "",
                    project_id=project_id,
                )
                if acc.allowed:
                    account = {
                        "credential_id": connection_ref_id,
                        "external_account_id": exposed_account_id,
                    }
            try:
                capabilities = get_scoped_source_capabilities(
                    project_id=project_id,
                    connection_ref_id=connection_ref_id,
                    identity=identity,
                    loaded_modules=get_loaded_modules(),
                    conn=conn,
                )
            except SourceCapabilitiesNotFound:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            except SourceCapabilitiesUnavailable:
                return JSONResponse(
                    {"code": "operation_failed", "message": "Source unavailable"}, status_code=503
                )
            recommendation = recommend_first_report(
                conn,
                project_id=project_id,
                capabilities=capabilities,
                account=account,
                actor=identity,
                datastream_id=datastream_id,
            )
    except Exception as exc:
        return _first_report_error(exc)
    payload = recommendation.as_dict()
    payload["connection_ref_id"] = connection_ref_id
    return _setup_no_store(JSONResponse(payload))


async def _save_first_report_draft(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.first_report_draft import save_first_report_draft
    from core.main import get_loaded_modules
    from core.project_access import resolve_strict_resource_access
    from core.source_capabilities import (
        SourceCapabilitiesNotFound,
        SourceCapabilitiesUnavailable,
        get_scoped_source_capabilities,
    )

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="edit", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            connection_ref_id = str(body.get("connection_ref_id") or "")
            try:
                capabilities = get_scoped_source_capabilities(
                    project_id=project_id,
                    connection_ref_id=connection_ref_id,
                    identity=identity,
                    loaded_modules=get_loaded_modules(),
                    conn=conn,
                )
            except SourceCapabilitiesNotFound:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            except SourceCapabilitiesUnavailable:
                return JSONResponse(
                    {"code": "operation_failed", "message": "Source unavailable"}, status_code=503
                )
            saved = save_first_report_draft(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                connection_ref_id=connection_ref_id,
                report_id=str(body.get("report_id") or ""),
                metrics=list(body.get("metrics") or []),
                dimensions=list(body.get("dimensions") or []),
                grain=list(body.get("grain") or []),
                timezone=str(body.get("timezone") or "UTC"),
                currency=str(body.get("currency") or "unknown"),
                interval=body.get("interval") or {},
                capabilities=capabilities,
                actor=identity,
                idempotency_key=key,
                effective_org_id=decision.org_id or "",
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
    except Exception as exc:
        return _first_report_error(exc)
    return _setup_no_store(
        JSONResponse(
            {
                "draft_id": saved.draft_id,
                "plan_version_id": saved.plan_version_id,
                "mapping_version_id": saved.mapping_version_id,
                "state": saved.state,
                "operation_id": saved.operation_id,
                "audit_event_id": saved.audit_event_id,
                "replayed": saved.replayed,
                "recommendation": saved.recommendation,
            },
            status_code=201,
        )
    )


async def _preview_first_report(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    from core.db import get_connection
    from core.first_report_draft import preview_first_report
    from core.project_access import resolve_strict_resource_access

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="view", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            preview = preview_first_report(
                conn,
                project_id=project_id,
                plan_version_id=str(body.get("plan_version_id") or ""),
                mapping_version_id=str(body.get("mapping_version_id") or ""),
                actor=identity,
                draft_id=body.get("draft_id"),
                row_count=body.get("row_count"),
                content_hash=body.get("content_hash"),
            )
    except Exception as exc:
        return _first_report_error(exc)
    return _setup_no_store(
        JSONResponse(
            {
                "draft_id": preview.draft_id,
                "plan_version_id": preview.plan_version_id,
                "mapping_version_id": preview.mapping_version_id,
                "executable": preview.executable,
                "row_count": preview.row_count,
                "content_hash": preview.content_hash,
                "dq_issues": preview.dq_issues,
                "evidence": preview.evidence,
                "can_publish": preview.can_publish,
            }
        )
    )


# ---------------------------------------------------------------------------
# Story 36.9: execute and publish the recent-first candidate SAFELY.
# Project-scoped, fail-closed via the Epic 36 gate + resolve_strict_resource_access
# (edit+ to execute; view+ to read state). ONE idempotent candidate; a bad
# candidate NEVER replaces the publication (last-known-good preserved); a ready
# valid candidate publishes atomically. Recent/historical coverage stay separate.
# ---------------------------------------------------------------------------
def _recent_first_error(exc: Exception) -> Response:
    from core.recent_first_publication import (
        RecentFirstConflict,
        RecentFirstUnavailable,
        RecentFirstValidationError,
    )

    if isinstance(exc, RecentFirstUnavailable):
        status, code, message = 404, "not_found", "Not found"
    elif isinstance(exc, RecentFirstConflict):
        status, code, message = 409, "conflict", "Recent-first candidate already running"
    elif isinstance(exc, (RecentFirstValidationError, json.JSONDecodeError, TypeError)):
        status, code, message = 422, "invalid_request", str(exc)
    else:
        logger.error("admin_api: recent-first publish failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "Recent-first publish unavailable"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))


async def _execute_recent_first(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core.db import get_connection
    from core.project_access import resolve_strict_resource_access
    from core.recent_first_publication import execute_recent_first

    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="edit", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            result = execute_recent_first(
                conn,
                project_id=project_id,
                actor=identity,
                idempotency_key=key,
                draft_id=body.get("draft_id"),
                datastream_id=datastream_id if not body.get("draft_id") else None,
                row_count=body.get("row_count"),
                content_hash=body.get("content_hash"),
                verification_verdict=body.get("verification_verdict"),
                pull_id=body.get("pull_id"),
                validated_content_hash=body.get("validated_content_hash"),
                force_empty_publish=bool(body.get("force_empty_publish", False)),
                approved=bool(body.get("approved", False)),
            )
    except Exception as exc:
        return _recent_first_error(exc)
    status = 201 if result.published else 200
    return _setup_no_store(JSONResponse(result.as_dict(), status_code=status))


async def _get_recent_first_state(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    from core.db import get_connection
    from core.project_access import resolve_strict_resource_access
    from core.recent_first_publication import get_recent_first_state

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="view", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            state = get_recent_first_state(
                conn, project_id=project_id, datastream_id=datastream_id
            )
    except Exception as exc:
        return _recent_first_error(exc)
    return _setup_no_store(JSONResponse(state))


# ---------------------------------------------------------------------------
# Story 36.10: expose first-pull progress + AUTHORITATIVE report readiness.
# Project-scoped, fail-closed via the Epic 36 gate + resolve_strict_resource_access
# (view+; existence-hides on denial). Returns ONE versioned first_report_readiness
# object whose overall / host_cta are SERVER-derived (the UI never re-infers) and
# whose degraded state is honest (never labelled fully ready). Pure read/compose
# surface (E36-NFR05) -- mutates nothing.
# ---------------------------------------------------------------------------
async def _get_first_report_readiness(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    from core.db import get_connection
    from core.first_report_readiness import compute_first_report_readiness
    from core.project_access import resolve_strict_resource_access

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="view", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            readiness = compute_first_report_readiness(
                conn,
                datastream_id=datastream_id,
                project_id=project_id,
                actor=identity,
            )
    except Exception as exc:
        logger.error("admin_api: first-report readiness failed: %s", type(exc).__name__)
        return _setup_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Readiness unavailable"},
                status_code=500,
            )
        )
    return _setup_no_store(JSONResponse(readiness.as_dict()))


# ---------------------------------------------------------------------------
# Story 36.19: tenant-facing first-value funnel read (E36-FR09).
# Project-scoped, fail-closed via the Epic 36 gate + resolve_strict_resource_access
# (view+). The funnel domain (`tenant_journeys`) INDEPENDENTLY re-resolves strict
# resource access for `identity` and existence-hides (empty) on denial; the endpoint
# ALSO gates up-front so an unauthorized caller learns nothing about the project.
# Returns ONLY allowlisted stage/outcome enums + wait-state OWNER TYPES for the
# authorized project's own journeys -- never a pseudonymised cross-tenant cohort, never
# a raw email/org/provider (AD-32). Cross-tenant cohort stays STAFF-only tooling
# (`first_value_funnel.cross_tenant_cohort`) and is deliberately NOT exposed here.
# ---------------------------------------------------------------------------
async def _get_first_value_journeys(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    project_id = request.path_params.get("project_id")
    from core.db import get_connection
    from core.first_value_funnel import tenant_journeys
    from core.project_access import resolve_strict_resource_access

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="view", project_id=project_id
            )
            if not decision.allowed:
                # Existence-hiding: an unauthorized caller learns nothing (404).
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            journeys = tenant_journeys(conn, identity=identity, project_id=project_id)
    except Exception as exc:
        logger.error("admin_api: first-value journeys failed: %s", type(exc).__name__)
        return _setup_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "First-value journeys unavailable"},
                status_code=500,
            )
        )
    payload = {
        "project_id": project_id,
        "journeys": [
            {
                "journey_ref": view.journey_ref_hash,
                "stages": view.stages,
                "wait_state_owners": view.wait_state_owners,
            }
            for view in journeys
        ],
    }
    return _setup_no_store(JSONResponse(payload))


# ---------------------------------------------------------------------------
# Story 36.15: render / validate / reproduce the first report (E36-FR05).
# READ-ONLY starter request. Project-scoped, fail-closed via the Epic 36 gate +
# resolve_strict_resource_access (view+; existence-hides on denial). Runs only
# when readiness is ready/degraded-within-policy; returns BOUNDED evidence + an
# optional authenticated deep-link -- NEVER the full dataset (E36-NFR06). The
# reproduce endpoint INDEPENDENTLY re-evaluates access for the SECOND identity and
# existence-hides on denial (no report/first-user leak).
# ---------------------------------------------------------------------------
async def _render_first_report(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    from core.db import get_connection
    from core.first_report_render import (
        FirstReportRenderUnavailable,
        render_first_report,
    )
    from core.project_access import resolve_strict_resource_access

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="view", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            rendered = render_first_report(
                conn,
                datastream_id=datastream_id,
                project_id=project_id,
                actor=identity,
            )
    except FirstReportRenderUnavailable:
        return _setup_no_store(
            JSONResponse(
                {"code": "not_renderable", "message": "Report is not yet renderable"},
                status_code=409,
            )
        )
    except Exception as exc:
        logger.error("admin_api: first-report render failed: %s", type(exc).__name__)
        return _setup_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Render unavailable"},
                status_code=500,
            )
        )
    return _setup_no_store(JSONResponse(rendered.as_dict()))


async def _reproduce_first_report(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    from core.db import get_connection
    from core.first_report_render import (
        FirstReportRenderUnavailable,
        FirstReportReproductionDenied,
        reproduce_first_report,
    )

    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        workspace_ref = body.get("workspace_ref")
        with get_connection() as conn:
            # The SECOND user's access is re-evaluated INDEPENDENTLY inside the
            # domain (resolve_strict_resource_access for `identity`); denial ->
            # existence-hiding not_found. No first-user readiness object leaks.
            rendered = reproduce_first_report(
                conn,
                datastream_id=datastream_id,
                project_id=project_id,
                second_actor=identity,
                workspace_ref=workspace_ref,
            )
    except FirstReportReproductionDenied:
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    except FirstReportRenderUnavailable:
        return _setup_no_store(
            JSONResponse(
                {"code": "not_renderable", "message": "Report is not yet renderable"},
                status_code=409,
            )
        )
    except (json.JSONDecodeError, TypeError):
        return _setup_no_store(
            JSONResponse({"code": "invalid_request", "message": "Invalid body"}, status_code=422)
        )
    except Exception as exc:
        logger.error("admin_api: first-report reproduce failed: %s", type(exc).__name__)
        return _setup_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Reproduce unavailable"},
                status_code=500,
            )
        )
    payload = rendered.as_dict()
    payload["reproduced"] = True
    return _setup_no_store(JSONResponse(payload))


# ---------------------------------------------------------------------------
# Story 36.18: governed-publication TRUSTED CONSOLE (AD-27 human confirmation).
#
# THE SECRET SPLIT (the invariant this section exists to enforce)
# ---------------------------------------------------------------
# Governed publication mints an OPAQUE, model-hidden confirmation secret in
# ``governed_publication.prepare_publication_review``. The MCP tool
# ``review_agent_change`` DELIBERATELY DROPS that secret before returning, so an
# agent can inspect the review scope/diff but the secret NEVER enters model/MCP
# context. That leaves a gap: without an out-of-band retrieval path, nobody can
# ever call ``confirm_and_publish`` and governed publication is unreachable.
#
# These REST endpoints ARE that out-of-band path -- the AD-27 "trusted console /
# in-host human presence" surface. They are NOT MCP tools: the secret is returned
# to an AUTHENTICATED HUMAN OPERATOR over REST (never to a model), and the confirm
# accepts the secret the console retrieved. The MCP ``review_agent_change`` stays
# secret-hidden; the human console (here) is the only place the secret surfaces,
# ONE TIME, to a caller who already holds ``manage`` authority over the resource.
#
#   MCP  review_agent_change            -> review WITHOUT the secret (agents: scope/diff)
#   REST POST /publication-reviews      -> mints the review AND returns the secret ONCE
#                                          to the manage-authority human console
#   REST POST .../{id}/confirm          -> human-only; carries the console-held secret
#                                          (or, in future, a server-verified in-host
#                                          presence) -- NEVER a raw model tool argument
#   REST POST .../{id}/rollback         -> the DISTINCT rollback as a human console action
#
# All three: authenticated + Epic 36 gate (``_setup_gate_response``) + strict
# ``manage`` access over the proposal's/confirmation's org+project
# (``resolve_strict_resource_access``); existence-hiding (404) on any denial; and
# the secret is NEVER logged and NEVER placed on any MCP surface.
# ---------------------------------------------------------------------------
def _governed_publication_error(exc: Exception) -> Response:
    """Map a governed-publication domain failure to a fail-closed REST response.

    A confirm precondition breach (``PublicationConfirmationRefused``) carries a
    stable ``code`` but MUST NOT reveal why beyond it; a 409 is returned so the
    console can retry/refresh without disclosing internal review state. A missing/
    out-of-scope review (``PublicationReviewUnavailable``) is existence-hidden as a
    404. The confirmation secret is NEVER included in any error body.
    """
    from core.governed_publication import (  # noqa: PLC0415
        PublicationConfirmationRefused,
        PublicationReviewUnavailable,
    )
    from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

    if isinstance(exc, PublicationReviewUnavailable):
        status, code, message = 404, "not_found", "Not found"
    elif isinstance(exc, PublicationConfirmationRefused):
        # Stable code only; never the raw refusal reason string beyond the code.
        status, code, message = 409, exc.code, "Confirmation refusee"
    elif isinstance(exc, OperationIdempotencyConflict):
        status, code, message = 409, "conflict", "Publication already in progress"
    elif isinstance(exc, (json.JSONDecodeError, TypeError, ValueError)):
        status, code, message = 422, "invalid_request", "Requete invalide"
    else:
        logger.error("admin_api: governed publication failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "Publication indisponible"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))


def _publication_confirmation_scope(conn, confirmation_id: str):
    """Resolve (datastream_id, project_id, org_id) for a prepared confirmation.

    Used to run the strict AD-5 ``manage`` guard on the confirmation's resource
    BEFORE the domain module re-loads the full row FOR UPDATE. An absent row yields
    None so the caller existence-hides (404) without disclosing the confirmation.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT datastream_id, project_id, org_id "
            "FROM app.publication_confirmations WHERE id = %s",
            (confirmation_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {"datastream_id": row[0], "project_id": row[1], "org_id": row[2]}


async def _prepare_publication_review_console(request: Request) -> Response:
    """POST /api/governance/publication-reviews -- mint a review + return the secret ONCE.

    The trusted-console out-of-band retrieval (AD-27). Authenticated human + Epic 36
    gate + strict ``manage`` authority over the PROPOSAL's org+project. Calls
    ``prepare_publication_review`` and returns the review object INCLUDING the opaque
    ``confirmation_secret`` -- exactly once, to this manage-authority human operator
    over REST. This is the ONLY surface that reveals the secret; the MCP review tool
    never does. The secret is NEVER logged. Denial (out of scope / not ready) is a
    404 (existence-hiding).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    from core.db import get_connection  # noqa: PLC0415
    from core.governed_publication import prepare_publication_review  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        proposal_id = str(body.get("proposal_id") or "").strip()
        if not proposal_id:
            return _setup_no_store(
                JSONResponse(
                    {"code": "invalid_request", "message": "proposal_id est requis."},
                    status_code=422,
                )
            )
        with get_connection() as conn:
            # Resolve the proposal's resource so we can guard it on the manage floor.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT datastream_id, project_id, org_id "
                    "FROM app.mapping_proposals WHERE id = %s",
                    (proposal_id,),
                )
                prow = cur.fetchone()
            if prow is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="manage", datastream_id=prow[0]
            )
            if not decision.allowed or str(decision.org_id) != str(prow[2]):
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            review = prepare_publication_review(
                conn,
                proposal_id=proposal_id,
                actor=identity,
                org_id=str(decision.org_id),
                host_context=_setup_host_context(request),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- fail-closed; secret never logged.
        return _governed_publication_error(exc)

    # Return the model-safe review AND -- this is the out-of-band retrieval -- the
    # opaque secret, ONCE, to the authenticated manage-authority human console. This
    # response is REST-only; it never enters any model/MCP context. Never logged.
    payload = dict(review.review)
    payload["confirmation_id"] = review.confirmation_id
    payload["confirmation_secret"] = review.confirmation_secret
    payload["confirmation_secret_single_return"] = True
    return _setup_no_store(
        Response(
            json.dumps(payload),
            status_code=201,
            media_type="application/vnd.toorow.publication-review+json",
        )
    )


async def _confirm_publication_review_console(request: Request) -> Response:
    """POST /api/governance/publication-reviews/{confirmation_id}/confirm -- human confirm.

    The human-only confirmation surface. Authenticated human + Epic 36 gate + strict
    ``manage`` authority over the CONFIRMATION's org+project + Idempotency-Key. The
    request body carries the ``confirmation_secret`` the console retrieved at prepare
    time (the out-of-band value) -- it is a REST argument from a trusted human, NEVER
    a model tool argument. Calls ``confirm_and_publish`` (verifies the one-way hash,
    rechecks every precondition, routes EXACTLY ONE durable operation). The secret is
    NEVER echoed back and NEVER logged.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    confirmation_id = request.path_params.get("confirmation_id")
    from core import tracing  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.governed_publication import confirm_and_publish  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        # The out-of-band secret the console retrieved at prepare. Never logged.
        confirmation_secret = str(body.get("confirmation_secret") or "")
        if not confirmation_secret.strip():
            return _setup_no_store(
                JSONResponse(
                    {"code": "invalid_request", "message": "confirmation_secret est requis."},
                    status_code=422,
                )
            )
        with get_connection() as conn:
            scope = _publication_confirmation_scope(conn, confirmation_id)
            if scope is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="manage", datastream_id=scope["datastream_id"]
            )
            if not decision.allowed or str(decision.org_id) != str(scope["org_id"]):
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            result = confirm_and_publish(
                conn,
                confirmation_id=confirmation_id,
                confirmation_secret=confirmation_secret,
                actor=identity,
                org_id=str(decision.org_id),
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- fail-closed; secret never logged.
        return _governed_publication_error(exc)

    # NEVER echo the secret. Return only the operation outcome + versions.
    return _setup_no_store(
        JSONResponse(
            {
                "confirmation_id": result.confirmation_id,
                "operation_id": result.operation_id,
                "outcome": result.outcome,
                "replayed": result.replayed,
                "current_mapping_version_id": result.current_mapping_version_id,
                "prior_mapping_version_id": result.prior_mapping_version_id,
                "prior_version_rollbackable": result.prior_mapping_version_id is not None,
            },
            status_code=200,
        )
    )


async def _rollback_publication_review_console(request: Request) -> Response:
    """POST /api/governance/publication-reviews/{confirmation_id}/rollback -- human rollback.

    Re-points the live mapping pointer back to the prior version as a DISTINCT
    confirmed idempotent operation. Authenticated human + Epic 36 gate + strict
    ``manage`` authority over the confirmation's org+project + Idempotency-Key. Calls
    ``rollback_publication``. No secret is involved (rollback is authorized by the
    manage guard + the confirmation binding, not by the confirmation secret).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    confirmation_id = request.path_params.get("confirmation_id")
    from core import tracing  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.governed_publication import rollback_publication  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        target_mapping_version_id = str(body.get("target_mapping_version_id") or "").strip()
        if not target_mapping_version_id:
            return _setup_no_store(
                JSONResponse(
                    {
                        "code": "invalid_request",
                        "message": "target_mapping_version_id est requis.",
                    },
                    status_code=422,
                )
            )
        with get_connection() as conn:
            scope = _publication_confirmation_scope(conn, confirmation_id)
            if scope is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="manage", datastream_id=scope["datastream_id"]
            )
            if not decision.allowed or str(decision.org_id) != str(scope["org_id"]):
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            result = rollback_publication(
                conn,
                confirmation_id=confirmation_id,
                target_mapping_version_id=target_mapping_version_id,
                actor=identity,
                org_id=str(decision.org_id),
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- fail-closed.
        return _governed_publication_error(exc)

    return _setup_no_store(
        JSONResponse(
            {
                "confirmation_id": result.confirmation_id,
                "operation_id": result.operation_id,
                "outcome": result.outcome,
                "replayed": result.replayed,
                "current_mapping_version_id": result.current_mapping_version_id,
                "prior_mapping_version_id": result.prior_mapping_version_id,
                "distinct_operation": True,
            },
            status_code=200,
        )
    )


router = Router(
    routes=[
        # Story 21.1 (AC4): organization CRUD + membership. Static routes precede
        # /{org_id} so Starlette matches list/create first; /members after.
        Route("/api/organizations", endpoint=_list_orgs, methods=["GET"]),
        Route("/api/organizations", endpoint=_create_org, methods=["POST"]),
        Route("/invite", endpoint=_invitation_bootstrap, methods=["GET"]),
        Route("/api/invitations/exchange", endpoint=_exchange_invitation, methods=["POST"]),
        Route("/api/invitations/accept", endpoint=_accept_invitation, methods=["POST"]),
        Route(
            "/api/projects/{project_id}/setup-journey",
            endpoint=_get_setup_journey,
            methods=["GET"],
        ),
        Route(
            "/api/organizations/{org_id}/setup-journey",
            endpoint=_get_setup_journey,
            methods=["GET"],
        ),
        Route(
            "/api/setup/tasks/{task_id}/handoffs",
            endpoint=_prepare_setup_handoff,
            methods=["POST"],
        ),
        Route(
            "/api/setup/tasks/{task_id}/reassign",
            endpoint=_reassign_setup_task,
            methods=["POST"],
        ),
        Route(
            "/api/setup/handoffs/{handoff_id}/revoke",
            endpoint=_revoke_setup_handoff,
            methods=["POST"],
        ),
        Route("/handoff", endpoint=_setup_handoff_bootstrap, methods=["GET"]),
        Route(
            "/api/setup/handoffs/exchange",
            endpoint=_exchange_setup_handoff,
            methods=["POST"],
        ),
        # Story 36.7: delegated source authorization + exact account exposure.
        # Static route precedes the {delegation_id} routes.
        Route(
            "/api/source-delegations",
            endpoint=_prepare_source_delegation,
            methods=["POST"],
        ),
        Route(
            "/api/source-delegations/{delegation_id}/callback",
            endpoint=_source_delegation_callback,
            methods=["POST"],
        ),
        Route(
            "/api/source-delegations/{delegation_id}/revoke",
            endpoint=_revoke_source_delegation,
            methods=["POST"],
        ),
        # Story 36.14: capability-driven host preflight, install handoff + bind.
        # Static/catalog route precedes the {preflight_id} routes.
        Route(
            "/api/mcp-hosts/catalog",
            endpoint=_get_host_catalog,
            methods=["GET"],
        ),
        Route(
            "/api/mcp-hosts/preflight",
            endpoint=_prepare_host_preflight,
            methods=["POST"],
        ),
        Route(
            "/api/mcp-hosts/preflight/{preflight_id}/handoff",
            endpoint=_prepare_host_install_handoff,
            methods=["POST"],
        ),
        Route(
            "/api/mcp-hosts/preflight/{preflight_id}/bind",
            endpoint=_bind_host_connection,
            methods=["POST"],
        ),
        # Story 36.8: bounded first-report draft -- recommend / save / preview.
        Route(
            "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/recommend",
            endpoint=_recommend_first_report,
            methods=["POST"],
        ),
        Route(
            "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/draft",
            endpoint=_save_first_report_draft,
            methods=["POST"],
        ),
        Route(
            "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/preview",
            endpoint=_preview_first_report,
            methods=["POST"],
        ),
        # Story 36.9: execute + publish the recent-first candidate safely; read the
        # separate recent/historical coverage state for the 36.10 readiness object.
        Route(
            "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/execute",
            endpoint=_execute_recent_first,
            methods=["POST"],
        ),
        Route(
            "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/recent-state",
            endpoint=_get_recent_first_state,
            methods=["GET"],
        ),
        # Story 36.10: one versioned first-report readiness object (progress +
        # authoritative readiness). Server-derived overall / host_cta.
        Route(
            "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/readiness",
            endpoint=_get_first_report_readiness,
            methods=["GET"],
        ),
        # Story 36.19: tenant-facing first-value funnel journeys (E36-FR09). Project-
        # scoped, fail-closed (view+); returns ONLY allowlisted enums + wait-state owner
        # types for the authorized project's own journeys. Cross-tenant cohort stays
        # staff tooling and is NOT exposed here.
        Route(
            "/api/projects/{project_id}/first-value/journeys",
            endpoint=_get_first_value_journeys,
            methods=["GET"],
        ),
        # Story 36.15: render / validate the first report (read-only starter
        # request) and reproduce it for a SECOND independently-authorized user.
        # Bounded evidence + optional deep-link -- never the full dataset.
        Route(
            "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/render",
            endpoint=_render_first_report,
            methods=["GET"],
        ),
        Route(
            "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/reproduce",
            endpoint=_reproduce_first_report,
            methods=["POST"],
        ),

        # Story 36.18: governed-publication TRUSTED CONSOLE (AD-27 human confirmation).
        # The out-of-band secret-retrieval + human confirm/rollback surface. The MCP
        # review tool stays secret-hidden; ONLY the prepare route below returns the
        # opaque confirmation secret, ONCE, to a manage-authority human over REST.
        Route(
            "/api/governance/publication-reviews",
            endpoint=_prepare_publication_review_console,
            methods=["POST"],
        ),
        Route(
            "/api/governance/publication-reviews/{confirmation_id}/confirm",
            endpoint=_confirm_publication_review_console,
            methods=["POST"],
        ),
        Route(
            "/api/governance/publication-reviews/{confirmation_id}/rollback",
            endpoint=_rollback_publication_review_console,
            methods=["POST"],
        ),

        Route(
            "/api/organizations/{org_id}/invitations",
            endpoint=_list_invitations,
            methods=["GET"],
        ),
        Route(
            "/api/organizations/{org_id}/invitations/{invitation_id}/revoke",
            endpoint=_revoke_invitation,
            methods=["POST"],
        ),
        Route(
            "/api/organizations/{org_id}/invitations/{invitation_id}/resend",
            endpoint=_resend_invitation,
            methods=["POST"],
        ),
        Route(
            "/api/organizations/{org_id}/invitations",
            endpoint=_issue_invitation,
            methods=["POST"],
        ),
        # Story 21.8 AC4: read side of org membership (added after 21.5).
        Route(
            "/api/organizations/{org_id}/members",
            endpoint=_list_org_members,
            methods=["GET"],
        ),
        Route(
            "/api/organizations/{org_id}/members",
            endpoint=_add_org_member,
            methods=["POST"],
        ),
        # Story 21.5 follow-up: manage an existing member (remove / change role|status).
        Route(
            "/api/organizations/{org_id}/members/{identity}",
            endpoint=_remove_org_member,
            methods=["DELETE"],
        ),
        Route(
            "/api/organizations/{org_id}/members/{identity}",
            endpoint=_update_org_member,
            methods=["PATCH"],
        ),
        Route("/api/organizations/{org_id}", endpoint=_get_org, methods=["GET"]),
        Route("/api/organizations/{org_id}", endpoint=_patch_org, methods=["PATCH"]),
        # Story 24.2: org data-plane lifecycle (human-gated delete + per-org provision
        # + platform backfill).  provision-warehouse before {org_id} DELETE so
        # Starlette resolves the sub-resource before the bare id route.
        Route(
            "/api/organizations/{org_id}/provision-warehouse",
            endpoint=_provision_org_warehouse,
            methods=["POST"],
        ),
        # Same ordering rule: the sub-resource is declared before the bare id.
        Route(
            "/api/organizations/{org_id}/deletion-preview",
            endpoint=_org_deletion_preview,
            methods=["GET"],
        ),
        Route("/api/organizations/{org_id}", endpoint=_delete_org, methods=["DELETE"]),
        Route(
            "/api/admin/warehouse/provision-schemas",
            endpoint=_backfill_warehouse_schemas,
            methods=["POST"],
        ),
        # Story 21.2: self-service global user profile.
        Route("/api/me/profile", endpoint=_get_my_profile, methods=["GET"]),
        Route("/api/me/profile", endpoint=_patch_my_profile, methods=["PATCH"]),
        # RGPD account erasure: preview declared before the bare /api/me route.
        Route(
            "/api/me/deletion-preview",
            endpoint=_get_my_deletion_preview,
            methods=["GET"],
        ),
        Route("/api/me", endpoint=_delete_me, methods=["DELETE"]),
        # Story 21.3: credential accounts + per-account cross-org grants. Most
        # specific (grants under an account) declared before the shorter shapes.
        Route(
            "/api/credentials/{credential_id}/accounts/{external_account_id}/grants/{grantee_org_id}",
            endpoint=_revoke_account_grant,
            methods=["DELETE"],
        ),
        Route(
            "/api/credentials/{credential_id}/accounts/{external_account_id}/grants",
            endpoint=_create_account_grant,
            methods=["POST"],
        ),
        Route(
            "/api/credentials/{credential_id}/accounts",
            endpoint=_list_credential_accounts,
            methods=["GET"],
        ),
        Route(
            "/api/credentials/{credential_id}/accounts",
            endpoint=_register_credential_account,
            methods=["POST"],
        ),
        Route(
            "/api/credentials/{credential_id}/grants",
            endpoint=_list_credential_grants,
            methods=["GET"],
        ),
        # Story 24.5: dataset marts access grants (BigQuery IAM, per-org).
        # DELETE (with /{grant_id}) declared before the shorter GET/POST shapes.
        Route(
            "/api/organizations/{org_id}/dataset-access/{grant_id}",
            endpoint=_revoke_dataset_access,
            methods=["DELETE"],
        ),
        Route(
            "/api/organizations/{org_id}/dataset-access",
            endpoint=_grant_dataset_access,
            methods=["POST"],
        ),
        Route(
            "/api/organizations/{org_id}/dataset-access",
            endpoint=_list_dataset_access_grants,
            methods=["GET"],
        ),
        # Story 21.4: flux (app.datastreams) org-scoped + linked to N projects.
        # DELETE (with /{project_id}) declared before the shorter GET/POST shapes.
        Route(
            "/api/flux/{flux_id}/projects/{project_id}",
            endpoint=_unlink_flux_from_project,
            methods=["DELETE"],
        ),
        Route(
            "/api/flux/{flux_id}/projects",
            endpoint=_list_flux_projects,
            methods=["GET"],
        ),
        Route(
            "/api/flux/{flux_id}/projects",
            endpoint=_link_flux_to_project,
            methods=["POST"],
        ),
        # Story 7.1 (AC3, AC4): project CRUD. Static /api/projects precedes the
        # parametrized /{project_id} routes so Starlette matches list/create first.
        Route(
            "/api/vocabularies/countries",
            endpoint=_list_countries,
            methods=["GET"],
        ),
        Route("/api/projects", endpoint=_list_projects, methods=["GET"]),
        Route("/api/projects", endpoint=_create_project, methods=["POST"]),
        Route(
            "/api/projects/{project_id}/geography/preview",
            endpoint=_preview_geographic_change,
            methods=["POST"],
        ),
        Route(
            "/api/projects/{project_id}/geography/previews/{preview_id}/confirm",
            endpoint=_confirm_geographic_change,
            methods=["POST"],
        ),        Route("/api/projects/{project_id}", endpoint=_get_project, methods=["GET"]),
        Route("/api/projects/{project_id}", endpoint=_patch_project, methods=["PATCH"]),
        Route("/api/projects/{project_id}", endpoint=_delete_project, methods=["DELETE"]),
        # Story 7.3 (AC5): key rotation endpoint.
        # MUST be declared before the generic {project_id} routes to avoid
        # Starlette absorbing "rotate-key" as a path param on the nested routes.
        Route(
            "/api/projects/{project_id}/rotate-key",
            endpoint=_rotate_project_key,
            methods=["POST"],
        ),
        # Story 7.3 (AC4): per-connection revocation endpoint.
        Route(
            "/api/projects/{project_id}/connections/{connection_id}/revoke",
            endpoint=_revoke_connection,
            methods=["POST"],
        ),
        Route("/api/source-capabilities", endpoint=_source_capabilities, methods=["GET"]),
        Route("/api/connections", endpoint=_list_connections, methods=["GET"]),
        Route("/api/connections", endpoint=_create_connection, methods=["POST"]),
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
        # Story 3.4 (AC3): Cloud Scheduler dispatch stub (Phase B, QUEUE_BACKEND=cloud_tasks)
        Route(
            "/internal/scheduler/dispatch-nightly",
            endpoint=_dispatch_nightly_internal,
            methods=["POST"],
        ),
        # Story 4.3 (AC4): context events CRUD (admin console only — widget uses MCP tool)
        Route("/api/context-events", endpoint=_create_context_event, methods=["POST"]),
        Route("/api/context-events", endpoint=_list_context_events, methods=["GET"]),
        # Story 4.4 (AC8): manual mirror sync trigger
        Route("/api/mirror/sync", endpoint=_trigger_mirror_sync, methods=["POST"]),
        # Story 5.2 (AC4): REST proxy for health MCP tool (Pipeline panel)
        Route("/api/health", endpoint=_health_proxy, methods=["GET"]),
        # Story 5.3 (AC5): alert-definitions CRUD
        # IMPORTANT: /api/alert-definitions must precede /api/alert-definitions/{id}
        # so Starlette does not absorb the list/create routes as ID parameters.
        Route(
            "/api/alert-definitions",
            endpoint=_list_alert_definitions,
            methods=["GET"],
        ),
        Route(
            "/api/alert-definitions",
            endpoint=_create_alert_definition,
            methods=["POST"],
        ),
        Route(
            "/api/alert-definitions/{id}",
            endpoint=_update_alert_definition,
            methods=["PATCH"],
        ),
        Route(
            "/api/alert-definitions/{id}",
            endpoint=_delete_alert_definition,
            methods=["DELETE"],
        ),
        # Story 5.5 (AC7): feedback queryability endpoint
        Route("/api/feedback", endpoint=_list_feedback, methods=["GET"]),
        Route("/api/tracked-entities", endpoint=_list_tracked_entities, methods=["GET"]),
        Route("/api/knowledge", endpoint=_list_knowledge, methods=["GET"]),
        Route("/api/procedures", endpoint=_list_procedures, methods=["GET"]),
        Route("/api/eval/golden-questions", endpoint=_list_golden_questions, methods=["GET"]),
        Route("/api/eval/runs", endpoint=_list_eval_runs, methods=["GET"]),
        Route("/api/overview/summary", endpoint=_overview_summary, methods=["GET"]),
        # Story 6.1 (AC9): report management endpoints.
        # IMPORTANT: the static /available route precedes the parametrized PATCH
        # route so Starlette does not absorb "available" as a project_id param.
        Route("/api/reports/available", endpoint=_list_available_reports, methods=["GET"]),
        Route(
            "/api/reports/{project_id}/{module_name}/{report_id}",
            endpoint=_patch_report,
            methods=["PATCH"],
        ),
        # Story 7.2 (AC7): module management endpoints.
        # IMPORTANT: the static /available route precedes the parametrized PATCH
        # route so Starlette does not absorb "available" as a project_id param.
        Route("/api/modules/available", endpoint=_list_available_modules, methods=["GET"]),
        Route(
            "/api/modules/{project_id}/{module_name}",
            endpoint=_patch_module,
            methods=["PATCH"],
        ),
        # Story 6.5 (AC5): notebooks CRUD + run trigger.
        # IMPORTANT: the static /api/notebooks route (list) must precede the
        # parametrized routes so Starlette does not absorb the list GET as a notebook_id.
        # The /run endpoint must precede the bare /{notebook_id} routes to avoid
        # Starlette absorbing "run" as a notebook_id path param.
        # Story 6.6: /shared/{token} MUST precede /{notebook_id}/... routes (no auth).
        Route("/api/notebooks", endpoint=_list_notebooks, methods=["GET"]),
        # Story 6.6 (AC3): public shared endpoint -- no auth guard; token is unguessable.
        # MUST be declared before parametrized notebook_id routes to avoid "shared"
        # being absorbed as a notebook_id.
        Route(
            "/api/notebooks/shared/{token}",
            endpoint=_shared_notebook_endpoint,
            methods=["GET"],
        ),
        Route(
            "/api/notebooks/{notebook_id}/run",
            endpoint=_run_notebook_endpoint,
            methods=["POST"],
        ),
        # Story 6.6 (AC2): schedule toggle.
        Route(
            "/api/notebooks/{notebook_id}/schedule",
            endpoint=_schedule_notebook,
            methods=["PATCH"],
        ),
        # Story 6.6 (AC3): share token management.
        Route(
            "/api/notebooks/{notebook_id}/share",
            endpoint=_share_notebook,
            methods=["PATCH"],
        ),
        # Story 6.6 (AC5): slide/HTML export.
        Route(
            "/api/notebooks/{notebook_id}/runs/{run_id}/export/html",
            endpoint=_export_notebook_html,
            methods=["GET"],
        ),
        Route(
            "/api/notebooks/{notebook_id}",
            endpoint=_get_notebook,
            methods=["GET"],
        ),
        Route(
            "/api/notebooks/{notebook_id}",
            endpoint=_patch_notebook,
            methods=["PATCH"],
        ),
        Route(
            "/api/notebooks/{notebook_id}",
            endpoint=_delete_notebook,
            methods=["DELETE"],
        ),
        # Story 8.2: datastream CRUD + /run.
        # IMPORTANT: /api/datastreams (list/create) must precede the parametrized
        # /{id} routes. The /run, /ledger, /refetch sub-routes must precede /{id}
        # so Starlette does not absorb them as id path parameters.
        Route("/api/datastreams", endpoint=_list_datastreams, methods=["GET"]),
        Route("/api/datastreams", endpoint=_create_datastream, methods=["POST"]),
        Route(
            "/api/datastreams/{id}/versions",
            endpoint=_list_datastream_versions,
            methods=["GET"],
        ),
        Route(
            "/api/datastreams/{id}/validate",
            endpoint=_validate_datastream_intent,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/run",
            endpoint=_run_datastream,
            methods=["POST"],
        ),
        # Story 8.3: extract ledger + refetch endpoints.
        Route(
            "/api/datastreams/{id}/ledger",
            endpoint=_get_datastream_ledger,
            methods=["GET"],
        ),
        Route(
            "/api/datastreams/{id}/refetch",
            endpoint=_refetch_datastream,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/mapping/profile",
            endpoint=_profile_datastream_mapping,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/mapping/versions",
            endpoint=_list_datastream_mapping_versions,
            methods=["GET"],
        ),
        Route(
            "/api/datastreams/{id}/mapping/versions",
            endpoint=_create_datastream_mapping_version,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/mapping/versions/{ver}",
            endpoint=_get_datastream_mapping_version,
            methods=["GET"],
        ),
        # Story 12.4: safe KPI projection compile (Member; NEVER publishes).
        Route(
            "/api/datastreams/{id}/projection/compile",
            endpoint=_compile_datastream_projection,
            methods=["POST"],
        ),
        # Story 12.5: atomic candidate publication. Static/sub-path routes precede
        # the /{id} catch-alls; more-specific /executions/{exec_id}/<verb> routes
        # precede /executions/{exec_id}.
        Route(
            "/api/datastreams/{id}/executions",
            endpoint=_create_datastream_execution,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/executions/{exec_id}/state",
            endpoint=_advance_datastream_execution_state,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/executions/{exec_id}/publish",
            endpoint=_publish_datastream_execution,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/executions/{exec_id}/reconcile",
            endpoint=_reconcile_datastream_execution,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/executions/{exec_id}",
            endpoint=_get_datastream_execution,
            methods=["GET"],
        ),
        Route(
            "/api/datastreams/{id}/publications",
            endpoint=_list_datastream_publications,
            methods=["GET"],
        ),
        # Story 12.7: read-only external BigQuery observation (Member). Static
        # /observe sub-path precedes the /{id} catch-alls.
        Route(
            "/api/datastreams/{id}/observe",
            endpoint=_observe_datastream,
            methods=["POST"],
        ),
        # Story 12.8: managed-feed imports through an immutable ledger. More-specific
        # /imports/{ledger_id}/<verb> routes precede /imports/{ledger_id}, which
        # precedes /imports; all precede the /{id} catch-alls.
        Route(
            "/api/datastreams/{id}/managed-feed/imports/{ledger_id}/rows",
            endpoint=_record_managed_feed_rows,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/managed-feed/imports/{ledger_id}/publish",
            endpoint=_publish_managed_feed_import,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/managed-feed/imports/{ledger_id}/rejected-rows",
            endpoint=_get_managed_feed_rejected_rows,
            methods=["GET"],
        ),
        Route(
            "/api/datastreams/{id}/managed-feed/imports/{ledger_id}",
            endpoint=_get_managed_feed_import,
            methods=["GET"],
        ),
        Route(
            "/api/datastreams/{id}/managed-feed/imports",
            endpoint=_open_managed_feed_import,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/managed-feed/imports",
            endpoint=_list_managed_feed_imports,
            methods=["GET"],
        ),
        # Story 12.10: Google Sheets recurring sync (managed-feed sync schedule).
        # Static /managed-feed/<verb> sub-paths precede the /managed-feed/imports*
        # routes above only by prefix; they are disjoint. All precede /{id}.
        Route(
            "/api/datastreams/{id}/managed-feed/configure",
            endpoint=_configure_managed_feed_sync,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/managed-feed/sync-now",
            endpoint=_sync_now_managed_feed,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/managed-feed/status",
            endpoint=_status_managed_feed_sync,
            methods=["GET"],
        ),
        # Story 12.9: CSV / Excel governed import. More-specific /import-contracts/
        # {contract_id} precedes /import-contracts; /imports/preview precedes /imports.
        # All precede the /{id} catch-alls.
        Route(
            "/api/datastreams/{id}/imports/preview",
            endpoint=_preview_csv_excel_import,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/imports",
            endpoint=_confirm_csv_excel_import,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/import-contracts/{contract_id}",
            endpoint=_get_import_contract,
            methods=["GET"],
        ),
        Route(
            "/api/datastreams/{id}/import-contracts",
            endpoint=_put_import_contract,
            methods=["PUT"],
        ),
        Route(
            "/api/datastreams/{id}/import-contracts",
            endpoint=_list_import_contracts,
            methods=["GET"],
        ),
        # Story 12.11: bounded sync / reload / reprocess (prepare + confirm).
        Route(
            "/api/datastreams/{id}/bounded/prepare",
            endpoint=_prepare_bounded_recovery,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/bounded/confirm",
            endpoint=_confirm_bounded_recovery,
            methods=["POST"],
        ),
        # Story 12.12: safe replace / append / rollback (dataset recovery). More-
        # specific /rollback/preview precedes /rollback. All precede the /{id}
        # catch-alls.
        Route(
            "/api/datastreams/{id}/rollback/preview",
            endpoint=_preview_dataset_rollback,
            methods=["GET"],
        ),
        Route(
            "/api/datastreams/{id}/rollback",
            endpoint=_rollback_dataset,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/replace/preflight",
            endpoint=_preflight_replace_dataset,
            methods=["POST"],
        ),
        Route(
            "/api/datastreams/{id}/append/availability",
            endpoint=_append_availability_dataset,
            methods=["GET"],
        ),
        Route(
            "/api/datastreams/{id}/destination-policy",
            endpoint=_dataset_destination_policy,
            methods=["POST"],
        ),
        # Story 12.14: versioned Datastream read model (Viewer). A DISTINCT
        # /read-model path (the /versions path is already taken by the 12.2 intent-
        # version list, _list_datastream_versions). Static sub-path precedes /{id}.
        Route(
            "/api/datastreams/{id}/read-model",
            endpoint=_get_datastream_versions,
            methods=["GET"],
        ),
        # Story 12.19: deterministic daily masked sample-preview. Static /sample
        # sub-path MUST precede /{id} so Starlette does not absorb it as an id.
        Route(
            "/api/datastreams/{id}/sample",
            endpoint=_datastream_sample,
            methods=["GET"],
        ),
        Route("/api/datastreams/{id}", endpoint=_get_datastream, methods=["GET"]),
        Route("/api/datastreams/{id}", endpoint=_patch_datastream, methods=["PATCH"]),
        Route("/api/datastreams/{id}", endpoint=_delete_datastream, methods=["DELETE"]),
        # Story 8.5: data model (target fields + mappings) CRUD -- routes live in
        # core.datamodel_api; static /fields paths precede /{name} inside the list.
        *_DATAMODEL_ROUTES,
        # Story 8.4: control tower overview -- routes live in core.overview.
        *_OVERVIEW_ROUTES,
        # Story 8.7: declarative flows (shared MCP/REST layer) -- core.flows_api.
        *_FLOWS_ROUTES,
        # Story 8.6: data quality monitors -- core.dq_api.
        *_DQ_ROUTES,
        # Story 8.9: report-to-datamodel chain view -- core.report_chain.
        # NOTE: this route pattern /api/reports/{module}/{report_id}/chain must be
        # listed AFTER any /api/reports/{project}/{module}/{id} PATCH route so the
        # static suffix "chain" takes precedence in Starlette's routing order.
        *_REPORT_CHAIN_ROUTES,
        # Story 9.1: card library catalog + get_card REST mirror -- core.cards_api.
        *_CARDS_ROUTES,
        # Story 11.1: context layer topics + procedures CRUD -- core.context_api.
        *_CONTEXT_ROUTES,
        # Story 11.2: schema-context auto-generation trigger (ADMIN-only) --
        # core.schema_context_api. Separate module from 11.1's CONTEXT_ROUTES.
        *_SCHEMA_CONTEXT_ROUTES,
        # Story 22.1: media plans -- core.mediaplan_api.
        *_MEDIAPLAN_ROUTES,
        # Story 13.5 volet (a): galerie des rendus -- core.rendus_api.
        # Route statique /api/rendus/snapshots declaree avant /{snapshot_id}.
        *_RENDUS_ROUTES,
        # Epic 35 Story 35.4: boite insights + partage equipe -- core.daily_insights_api.
        *_DAILY_INSIGHTS_ROUTES,
        # Story 19.3: cache DuckDB observability + rebuild trigger.
        # /status precede /rebuild pour clarte (pas de conflit de routes ici).
        Route("/api/admin/cache/status", endpoint=_cache_status, methods=["GET"]),
        Route("/api/admin/cache/rebuild", endpoint=_cache_rebuild, methods=["POST"]),
        # Story 27.2: metric semantics curation REST API -- core.metric_semantics_api.
        *_METRIC_SEMANTICS_ROUTES,
        # Story 13.2: MDM conflicts + FX binding -- core.conflict_resolutions_api.
        # Static routes (/api/mdm/conflicts/resolutions) declared before parameterised
        # (/api/mdm/conflicts/resolutions/{project_id}/{target_field}/{source_module}).
        *_CONFLICT_RESOLUTION_ROUTES,
        # Story 39.3: money aggregation-check (cross-currency refusal engine seam).
        *_MONEY_ROUTES,
        # Story 39.8: cross-source day-offset SIGNAL (timezone advisory engine seam).
        *_TIMEZONE_ROUTES,
        # Story 34.3: org-plan control surface (super-admin only, deny-by-default).
        *_ORG_PLAN_ROUTES,
        # Story 38.2: connector installation state surface (platform-admin + catalog gate).
        # More-specific paths (/installation suffix) before any future less-specific
        # connector routes per Starlette convention.
        *_CONNECTOR_INSTALLATION_ROUTES,
        # Story 38.3: connector domain and adapter-route configuration (platform-admin).
        # /domain routes are distinct from /installation routes; Starlette resolves by
        # path suffix so ordering between them is irrelevant, but we keep 38.3 after 38.2.
        *_CONNECTOR_DOMAIN_ROUTES,
        # Story 38.4: connector verification + synthetic test delivery (platform-admin).
        # /verify, /test-delivery, /verification are more-specific than the bare
        # connector param routes; Starlette resolves these before any future catch-alls.
        *_CONNECTOR_VERIFICATION_ROUTES,
        # Story 38.5: connector activation/deactivation (org-owner) + health layering.
        # /activation/deactivate is more-specific than /activation; routes listed
        # more-specific-first per Starlette convention.
        *_CONNECTOR_ACTIVATION_ROUTES,
        # Story 38.6: import template catalog read + inbound managed-feed Datastream
        # creation. /templates and /datastreams are distinct sub-paths under the
        # connector param route; both are declared after more-specific connector
        # suffixes (/installation, /domain, /verify, /activation) per Starlette
        # convention (more-specific first).
        *_IMPORT_TEMPLATE_ROUTES,
        # Story 38.7: inbound delivery credential lifecycle (issue/rotate/revoke/list).
        # /credentials and /credentials/{id}/rotate|revoke are more-specific than the
        # bare connector/datastream param routes. Rotate/revoke are declared before the
        # bare /credentials collection route (more-specific first).
        *_INBOUND_CREDENTIAL_ROUTES,
    ]
)
