"""toorow -- ASGI routing / dispatch (extracted 5.1/AI-27).

Extracted verbatim from ``core.main`` in Story 5.1 (AI-27 decomposition). Holds the
HTTP surface plumbing that wraps the FastMCP app:

  * ``HostHeaderValidationMiddleware`` -- Story 1.1 421 Host guard.
  * ``audit_endpoint`` -- Story 2.6 GET /api/audit.
  * ``build_asgi_app(mcp)`` -- assembles the /admin + /api + MCP dispatcher and
    starts the background threads (health poller, queue worker, scheduler).

``main.py`` re-exports ``HostHeaderValidationMiddleware``, ``_audit_endpoint`` and
``build_asgi_app`` for backward compatibility with existing imports/tests. The MCP
instance is passed in (not imported) so this module has no import cycle with main.

AD-2: source-agnostic -- no module-specific strings.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, Router
from starlette.staticfiles import StaticFiles

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Host-header / 421 handling (Story 1.1 T3.2).
# ---------------------------------------------------------------------------
class HostHeaderValidationMiddleware:
    """ASGI middleware returning 421 when the Host header is not allowed.

    Enabled only when ``HOST_HEADER_VALIDATION=strict``. ``ALLOWED_HOST`` may be
    a comma-separated list of allowed host[:port] values. When strict mode is on
    but ``ALLOWED_HOST`` is unset, all hosts are allowed (fail-open on config)
    and a note is left for operators — Cloud Run injects the real hostname.
    """

    def __init__(self, app):
        self.app = app
        self.strict = os.environ.get("HOST_HEADER_VALIDATION", "").lower() == "strict"
        raw = os.environ.get("ALLOWED_HOST", "")
        self.allowed = {h.strip().lower() for h in raw.split(",") if h.strip()}
        # Hardening: strict mode with no ALLOWED_HOST is a misconfiguration.
        # Log at ERROR at init so operators are alerted (previously silently fail-open).
        if self.strict and not self.allowed:
            logger.error(
                "routing: HOST_HEADER_VALIDATION=strict but ALLOWED_HOST is empty -- "
                "all requests will be rejected (403). Set ALLOWED_HOST to fix."
            )

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.strict:
            if not self.allowed:
                # Strict mode with no allowed hosts: fail-closed, reject all (403).
                await self._reject_forbidden(send)
                return
            host = ""
            for name, value in scope.get("headers", []):
                if name == b"host":
                    host = value.decode("latin-1").lower()
                    break
            # Review 1.1 M1: a missing Host header must also be rejected in
            # strict mode — an empty host is not an allowed host.
            if not host or host not in self.allowed:
                await self._reject(send)
                return
        await self.app(scope, receive, send)

    async def _reject_forbidden(self, send):
        body = (
            b'{"code":"forbidden",'
            b'"message":"ALLOWED_HOST not configured","provenance":null}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _reject(self, send):
        body = (
            b'{"code":"misdirected_request",'
            b'"message":"Host header not allowed","provenance":null}'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 421,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# Story 2.6 — GET /api/audit endpoint (Option B: standalone Starlette Route).
# ---------------------------------------------------------------------------


async def audit_endpoint(request: Request) -> Response:
    """GET /api/audit -- return audit rows as JSON or CSV.

    Query params (all optional):
        start          ISO-8601 date/datetime (inclusive lower bound on created_at)
        end            ISO-8601 date/datetime (inclusive upper bound)
        action         Exact match on action code (e.g. connection.created)
        connection_ref Exact match on conn_ ULID
        format         "json" (default) or "csv"

    Auth:
        Reuses auth mode from TOOROW_AUTH_MODE. In "oauth" mode a valid Bearer
        token is required (enforced by FastMCP's auth middleware on the MCP paths;
        for this plain HTTP path we check the mode and reject unauthenticated
        requests when auth is not disabled).
        In "disabled" mode (default / CI) no auth is required.

    Responses:
        200 JSON:  {"rows": [...], "count": N}
        200 CSV:   text/csv with header row + data rows
        401 JSON:  {"code": "unauthorized", "message": "..."}
        500 JSON:  {"code": "audit_query_error", "message": "..."}
    """
    # Auth gate (review-2-6 F-01): REAL token verification via the shared
    # api_auth layer — same verifier as the MCP boundary, not a presence check.
    from core.api_auth import authenticate_api_request
    from core.audit import query_audit_log, rows_to_csv

    authorized, identity = await authenticate_api_request(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "valid Bearer token required"},
            status_code=401,
        )

    params = request.query_params
    project_id = (params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"},
            status_code=422,
        )

    try:
        from core.admin_api import _strict_project_capability_allowed  # noqa: PLC0415
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
            "audit_access_unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        allowed = False
    if not allowed:
        return JSONResponse(
            {"code": "not_found", "message": "Project not found"},
            status_code=404,
        )

    start = params.get("start") or None
    end = params.get("end") or None
    action = params.get("action") or None
    connection_ref = params.get("connection_ref") or None
    fmt = (params.get("format") or "json").lower()
    try:
        limit = int(params.get("limit") or 500)
    except ValueError:
        limit = 500

    try:
        rows = query_audit_log(
            project_id=project_id,
            limit=limit,
            start=start,
            end=end,
            action=action,
            connection_ref=connection_ref,
        )
    except Exception as exc:
        logger.error("audit_query_error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "audit_query_error", "message": "Audit evidence is unavailable"},
            status_code=500,
        )

    if fmt == "csv":
        csv_body = rows_to_csv(rows)
        return Response(
            content=csv_body,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="audit.csv"'},
        )

    return JSONResponse({"rows": rows, "count": len(rows)})


def build_asgi_app(mcp):
    """Return the FastMCP streamable-HTTP ASGI app wrapped with the host guard.

    FastMCP 3.4 exposes ``http_app(transport="streamable-http")`` returning an
    ASGI app mounted at ``/mcp``. We wrap it with
    :class:`HostHeaderValidationMiddleware` so the Story-1.1 421 guard runs for
    every request without depending on FastMCP internals (the native
    ``FASTMCP_HTTP_ALLOWED_HOSTS`` guard still applies underneath).

    Story 2.4: /api/connections routes (admin_api.router) are merged into the
    /api/* dispatcher. Static files for the admin console are served at /admin.

    Story 2.5: health poller background thread is started here (not at import time).
    HEALTH_POLLER_ENABLED env var (default "true") gates the thread; set to "false"
    in CI. /api/connections/{id}/refresh-health is routed via admin_router.

    Story 2.6: a minimal Starlette Router for /api/audit is mounted alongside
    the FastMCP app. Both are wrapped by HostHeaderValidationMiddleware.

    Story 5.1: OTel tracing is initialised here (no-op when TRACING_ENABLED=false).
    """
    # Story 5.1: initialise tracing (no-op unless TRACING_ENABLED=true; never raises).
    from core import tracing  # noqa: PLC0415
    from core.admin_api import router as admin_router  # noqa: PLC0415

    tracing.init_tracing()

    # Story 2.5: start background health poller (only when HEALTH_POLLER_ENABLED=true)
    from core.health_poller import start_health_poller  # noqa: PLC0415

    start_health_poller()

    # Story 3.2: start background queue worker (only when QUEUE_WORKER_ENABLED=true)
    from core.queue import start_queue_worker  # noqa: PLC0415

    start_queue_worker()

    # Story 3.4: start nightly scheduler (only when SCHEDULER_ENABLED=true)
    from core.scheduler import start_nightly_scheduler  # noqa: PLC0415

    start_nightly_scheduler()

    # AI-346: re-derive stale compiled Semantic View artifacts once, at process
    # start, best-effort (SEMANTIC_RECOMPILE_ON_START, default true). A deploy
    # that bumps the compiler must not leave a published View unqueryable until
    # somebody notices.
    from core.semantic_artifact_sweep import start_startup_sweep  # noqa: PLC0415

    start_startup_sweep()

    # Story 2.6 -- /api/audit custom route (Option B, standalone)
    # BEGIN Story-2.6 fence
    audit_router = Router(
        routes=[Route("/api/audit", endpoint=audit_endpoint, methods=["GET"])]
    )
    mcp_app = mcp.http_app(transport="streamable-http")  # streamable HTTP transport (T3.1)

    # BEGIN Story-2.4 fence -- /api/connections + /admin static files
    # Determine the admin dist path (env-configurable for CI / Cloud Run).
    admin_dist_raw = os.environ.get("ADMIN_DIST_PATH", "ui/admin/dist")
    _repo_root = Path(__file__).parent.parent.parent
    admin_dist_path = Path(admin_dist_raw)
    if not admin_dist_path.is_absolute():
        admin_dist_path = _repo_root / admin_dist_raw

    if admin_dist_path.exists():
        admin_static = StaticFiles(directory=str(admin_dist_path), html=True)
        logger.info("admin_static: serving /admin from %s", admin_dist_path)
    else:
        admin_static = None
        logger.warning(
            "admin_static: dist not found at %s -- /admin not served "
            "(run: pnpm --filter @toorow/admin build)",
            admin_dist_path,
        )

    # The console build uses an ABSOLUTE base (vite.config.ts: the console is
    # served at the root in production), so its index.html calls /assets/… and
    # /imports/… at the ROOT, not under /admin. Serving only /admin/* answered
    # 200 with a blank page — the worst state, and the one measured on
    # 2026-08-07 (assets 404, deep links 500). The fallback is the SPA rule:
    # any /admin/* path that names no real file gets index.html.
    admin_index = admin_dist_path / "index.html" if admin_static is not None else None

    async def _serve_admin_index(scope, receive, send):
        from starlette.responses import FileResponse  # noqa: PLC0415

        await FileResponse(str(admin_index))(scope, receive, send)

    async def _serve_admin_static(scope, receive, send, not_found_json: bool):
        """StaticFiles, with its raised 404 translated instead of propagated.

        This Starlette raises HTTPException(404) for a missing file; through a
        bare ASGI dispatcher that surfaces as a 500. A missing bundle is a
        plain 404 -- and never index.html, which a browser would try to run as
        a module.
        """
        from starlette.exceptions import HTTPException  # noqa: PLC0415
        from starlette.responses import JSONResponse  # noqa: PLC0415

        try:
            await admin_static(scope, receive, send)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            if not_found_json:
                await JSONResponse(
                    {"code": "not_found", "message": "Not found"}, status_code=404
                )(scope, receive, send)
            else:
                await _serve_admin_index(scope, receive, send)

    # END Story-2.4 fence

    async def _combined_app(scope, receive, send):
        """Route requests:
          /admin*      -> admin static files (Story 2.4), SPA fallback included
          /assets|/imports/* -> the console's absolute-base bundles (same dist)
          /api/*       -> api_router (connections + audit) (Stories 2.4, 2.6)
          everything else -> MCP app
        """
        path = scope.get("path", "")

        # /admin static files (Story 2.4, T6.1)
        # review-2-4 F-02: exact /admin or /admin/ prefix only — a bare
        # startswith("/admin") would capture e.g. /adminX/... too.
        if (path == "/admin" or path.startswith("/admin/")) and admin_static is not None:
            # Strip /admin prefix for StaticFiles mount
            new_path = path[len("/admin"):]
            if not new_path:
                new_path = "/"
            scope = dict(scope)
            scope["path"] = new_path
            candidate = (admin_dist_path / new_path.lstrip("/")).resolve()
            in_dist = candidate.is_relative_to(admin_dist_path.resolve())
            # SPA fallback: a deep link names no file and no EXTENSION -- it is
            # a client route, the console owns it, it gets the index. A missing
            # file (favicon, bundle) is a plain 404, never HTML a browser would
            # try to execute as a module.
            route_like = in_dist and not candidate.suffix
            await _serve_admin_static(scope, receive, send, not_found_json=not route_like)
            return

        # The console's absolute-base bundles, served from the same dist. A
        # missing bundle is a plain 404 -- never the index.
        if (
            path.startswith(("/assets/", "/imports/"))
            and admin_static is not None
            and (admin_dist_path / path.lstrip("/")).resolve().is_relative_to(
                admin_dist_path.resolve()
            )
        ):
            await _serve_admin_static(scope, receive, send, not_found_json=True)
            return

        if path == "/invite":
            await admin_router(scope, receive, send)
            return

        # Story 50.7 -- the public Render Share surface. Without these two entries
        # the paths fall through to the MCP app and a recipient meets a 404 on a
        # link that is perfectly valid. Exact equality, never a `startswith`: a bare
        # prefix test would also capture `/shareX/...`, which is the bug the `/admin`
        # comment above records having already been fixed once.
        if path in ("/share", "/share/view"):
            await admin_router(scope, receive, send)
            return

        if path.startswith("/api/"):
            # /api/audit lives on its own standalone router (Story 2.6, Option B).
            # EVERYTHING else under /api/ belongs to the admin router.
            # review-global-gaps follow-up: the previous prefix allowlist
            # (connections/jobs/context-events/mirror/alert-definitions/health)
            # was never extended for Stories 4.x-7.x -- /api/projects,
            # /api/notebooks (incl. the public shared endpoint), /api/feedback,
            # /api/reports* all fell through to the audit router and 404'd in
            # the REAL app while route-mounted tests stayed green.
            if path == "/api/audit":
                await audit_router(scope, receive, send)
                return
            await admin_router(scope, receive, send)
            return

        # Route /internal/* to admin_router (Story 3.4, AC3 -- Cloud Scheduler stub)
        if path.startswith("/internal/"):
            await admin_router(scope, receive, send)
            return

        await mcp_app(scope, receive, send)

    return HostHeaderValidationMiddleware(_combined_app)
