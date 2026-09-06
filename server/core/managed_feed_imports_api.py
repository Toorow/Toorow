"""toorow -- the managed-feed import ledger surface (Story 12.8).

Six routes under `/api/datastreams/{id}/managed-feed/imports`: open an import,
record its rows, publish it through the blocking rejection gate and the 12.5 DQ
gates, and read the ledger back. Extracted from `admin_api.py` under AD-40 --
`docs/product-architecture/module-boundaries.md` -- with the handler bodies
unchanged and in the order they were declared.

`ManagedFeedError` subclasses map to typed HTTP codes; the generic base maps by
its stable `.code`. No `str(exc)` ever reaches a 5xx body.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The two seams this surface shares with the rest of the console.
# ---------------------------------------------------------------------------
#
# Reached at CALL time, never at import time, and that is not a style choice.
# `admin_api` imports the route collection below, so a module-level import here
# would close a cycle. It is also what keeps `patch("core.admin_api._check_auth")`
# working: a test that replaces the name on `admin_api` must still be the object
# these handlers call. The 56 sibling `*_api.py` modules do it this way for the
# same two reasons.


async def _check_auth(request: Request) -> tuple[bool, str]:
    """The one authentication seam of the console."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415

    return await _impl(request)


def _require_datastream_role(
    project_id: str,
    identity: str,
    minimum_role: str,
    conn,
    *,
    datastream_id: str | None = None,
    pair_proven_by_read: bool = False,
) -> "Response | None":
    """Strict Viewer/Member/Owner access for Datastream surfaces (see AI-219)."""
    from core.admin_api import _require_datastream_role as _impl  # noqa: PLC0415

    return _impl(
        project_id,
        identity,
        minimum_role,
        conn,
        datastream_id=datastream_id,
        pair_proven_by_read=pair_proven_by_read,
    )


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
        return JSONResponse({"code": exc.code, "message": "Conflicting payload"}, 409)
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
        body.get("idempotency_key") or request.headers.get("Idempotency-Key") or ""
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
    if not landing_relation or not content_hash or not isinstance(accepted_row_count, int):
        return JSONResponse(
            {
                "code": "missing_field",
                "message": ("landing_relation, accepted_row_count et content_hash sont requis"),
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

    ATOMICITY NOTE: the validating->ready advance commits before
    commit_publication (which is itself atomic); a crash in between leaves the
    candidate at `ready` and the prior published pointer intact, and a retry
    re-publishes idempotently.
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
                        {"code": "no_candidate", "message": "No execution to publish"},
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
                        exec_id,
                        STATE_VALIDATING,
                        STATE_READY,
                        identity or "anonymous",
                        conn,
                        project_id=project_id,
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
                return JSONResponse({"code": "not_found", "message": "Execution introuvable"}, 404)
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
                return JSONResponse({"code": "not_found", "message": "Import introuvable"}, 404)
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
            rows = get_rejected_rows(ledger_id, project_id, conn, limit=limit, offset=offset)
    except Exception as exc:
        logger.error("admin_api: get_managed_feed_rejected_rows_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Lignes rejetees indisponibles"}, 503
        )
    return JSONResponse({"rejected_rows": rows})


# ---------------------------------------------------------------------------
# The collection, in the order `admin_api` declared it.
# ---------------------------------------------------------------------------
#
# Starlette resolves in declaration order, so this order IS the contract: the
# more-specific `/imports/{ledger_id}/<verb>` routes precede
# `/imports/{ledger_id}`, which precedes `/imports`; all of them precede the
# `/{id}` catch-alls that stay in `admin_api`.

MANAGED_FEED_IMPORT_ROUTES = [
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
]
