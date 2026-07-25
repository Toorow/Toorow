"""toorow -- Context layer REST route handlers (Story 11.1 / 11.4).

Provides CONTEXT_ROUTES: list[Route] -- spliced into admin_api.router at startup.

Routes:
  POST /api/context/topics                -> Create topic
  GET  /api/context/topics                -> List topics (visible scope)
  GET  /api/context/topics/{id}           -> Get topic by ID
  PATCH /api/context/topics/{id}          -> Update topic
  POST /api/context/topics/{id}/archive   -> Archive topic
  POST /api/context/procedures            -> Create procedure
  GET  /api/context/procedures            -> List procedures (visible scope)
  GET  /api/context/procedures/{id}       -> Get procedure by ID
  PATCH /api/context/procedures/{id}      -> Update procedure
  POST /api/context/procedures/{id}/archive -> Archive procedure
  GET  /api/context/graph/edges           -> List graph edges (visible scope, AD-5)
  POST /api/context/graph/edges           -> Create graph edge
  DELETE /api/context/graph/edges/{id}    -> Delete graph edge (scoped)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import ACTION_CROSS_SCOPE_ATTEMPT, write_audit_row

logger = logging.getLogger(__name__)


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def check_platform_write_authorized(conn: Any, identity: str) -> bool:
    """Deny-by-default platform-scope write gate (Fix 3 / HIGH).

    Policy v1: allow only if the identity is listed in the CONTEXT_PLATFORM_WRITERS
    env var (comma-separated), OR the auth mode is 'disabled' (anonymous → owner dev
    mapping from resolve_project_role / project_access).

    Deny everyone else. This is intentionally restrictive; org-level governance will
    supersede this allowlist in a future story once an org role model exists.

    # TODO(org-level): replace this env allowlist with an org-level role check once
    # the org member/admin table is introduced.
    """
    # Disabled-auth dev mode: 'anonymous' is the injected identity (see project_access.py).
    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if auth_mode == "disabled" and identity == "anonymous":
        return True

    allowlist_raw = os.environ.get("CONTEXT_PLATFORM_WRITERS", "")
    if not allowlist_raw.strip():
        return False

    allowed = {s.strip() for s in allowlist_raw.split(",") if s.strip()}
    return identity in allowed


def _check_project_role(
    conn: Any,
    project_id: str | None,
    identity: str,
    minimum_role: str = "member",
    *,
    is_write: bool = False,
) -> bool:
    """Helper to verify identity project role.

    For project-scoped rows: delegates to identity_has_project_role.
    For platform-scoped (project_id is None) WRITES: calls check_platform_write_authorized
      (deny-by-default; Fix 3).
    For platform-scoped READS: any authenticated identity passes (policy documented in AC-4).
    """
    from core.project_access import identity_has_project_role  # noqa: PLC0415

    if project_id is not None:
        try:
            return identity_has_project_role(project_id, identity, minimum_role, conn)
        except Exception:
            return False

    # Platform scope
    if is_write:
        return check_platform_write_authorized(conn, identity)
    # Platform reads: any authenticated identity (documented: AC-4 / Fix 5).
    return True


def _audit_cross_scope(
    identity: str, resource_id: str, attempted_project_id: str | None = None
) -> None:
    write_audit_row(
        identity=identity,
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="platform",
        connection_ref="",
        metadata={"resource_id": resource_id, "attempted_project_id": attempted_project_id},
    )


# ---------------------------------------------------------------------------
# Topics Handlers
# ---------------------------------------------------------------------------


async def _create_topic(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    project_id = (body.get("project_id") or "").strip() or None
    title = body.get("title", "")
    body_md = body.get("body_md", "")

    from core.context_store import create_topic  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _check_project_role(
                conn, project_id, identity, minimum_role="member", is_write=True
            ):
                _audit_cross_scope(identity, "topic_create", project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"}, status_code=404
                )

            topic = create_topic(
                conn, project_id=project_id, title=title, body_md=body_md, created_by=identity
            )
            conn.commit()
            return JSONResponse(topic, status_code=201)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)
    except Exception as exc:
        logger.warning("context_api: create_topic error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la création du topic"}, status_code=500
        )


async def _list_topics(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    include_archived = request.query_params.get("status") == "archived"

    from core.context_store import list_topics  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if project_id and not _check_project_role(
                conn, project_id, identity, minimum_role="viewer"
            ):
                _audit_cross_scope(identity, "topics_list", project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"}, status_code=404
                )

            topics = list_topics(conn, project_id=project_id, include_archived=include_archived)
            return JSONResponse({"topics": topics})
    except Exception as exc:
        logger.warning("context_api: list_topics error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération des topics"},
            status_code=500,
        )


async def _get_topic(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    topic_id = request.path_params.get("id", "")

    from core.context_store import get_topic  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # Fetch unfiltered so a cross-scope hit is detected and audited below
            # (the handler-level role check is the enforcement point).
            topic = get_topic(conn, topic_id=topic_id)
            if not topic:
                return JSONResponse(
                    {"code": "not_found", "message": "Topic introuvable"}, status_code=404
                )

            # Handler-level scope check (outer layer; store-level is belt-and-suspenders).
            if topic["project_id"] and not _check_project_role(
                conn, topic["project_id"], identity, minimum_role="viewer"
            ):
                _audit_cross_scope(identity, topic_id, topic["project_id"])
                return JSONResponse(
                    {"code": "not_found", "message": "Topic introuvable"}, status_code=404
                )

            return JSONResponse(topic)
    except Exception as exc:
        logger.warning("context_api: get_topic error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération du topic"},
            status_code=500,
        )


async def _update_topic(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    topic_id = request.path_params.get("id", "")
    try:
        patch = await request.json()
    except Exception:
        patch = {}

    from core.context_store import get_topic, update_topic  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # Fetch unfiltered so a cross-scope attempt is audited below.
            topic = get_topic(conn, topic_id=topic_id)
            if not topic:
                _audit_cross_scope(identity, topic_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Topic introuvable"}, status_code=404
                )

            if not _check_project_role(
                conn, topic["project_id"], identity, minimum_role="member", is_write=True
            ):
                _audit_cross_scope(identity, topic_id, topic["project_id"])
                return JSONResponse(
                    {"code": "not_found", "message": "Topic introuvable"}, status_code=404
                )

            updated = update_topic(conn, topic_id=topic_id, patch=patch, changed_by=identity)
            conn.commit()
            return JSONResponse(updated)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)
    except KeyError as exc:
        return JSONResponse({"code": "not_found", "message": str(exc)}, status_code=404)
    except Exception as exc:
        logger.warning("context_api: update_topic error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la mise à jour du topic"},
            status_code=500,
        )


async def _archive_topic(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    topic_id = request.path_params.get("id", "")

    from core.context_store import archive_topic, get_topic  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # No caller_project_id from path/body for archive;
            # handler-level check is primary (Fix 4).
            topic = get_topic(conn, topic_id=topic_id)
            if not topic:
                _audit_cross_scope(identity, topic_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Topic introuvable"}, status_code=404
                )

            if not _check_project_role(
                conn, topic["project_id"], identity, minimum_role="member", is_write=True
            ):
                _audit_cross_scope(identity, topic_id, topic["project_id"])
                return JSONResponse(
                    {"code": "not_found", "message": "Topic introuvable"}, status_code=404
                )

            archived = archive_topic(conn, topic_id=topic_id, changed_by=identity)
            conn.commit()
            return JSONResponse(archived)
    except KeyError as exc:
        return JSONResponse({"code": "not_found", "message": str(exc)}, status_code=404)
    except Exception as exc:
        logger.warning("context_api: archive_topic error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de l'archivage du topic"}, status_code=500
        )


# ---------------------------------------------------------------------------
# Procedures Handlers
# ---------------------------------------------------------------------------


async def _create_procedure(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    project_id = (body.get("project_id") or "").strip() or None
    frontmatter_yaml = body.get("frontmatter_yaml", "")
    body_md = body.get("body_md", "")

    from core.context_store import DuplicateProcedureNameError, create_procedure  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _check_project_role(
                conn, project_id, identity, minimum_role="member", is_write=True
            ):
                _audit_cross_scope(identity, "procedure_create", project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"}, status_code=404
                )

            proc = create_procedure(
                conn,
                project_id=project_id,
                frontmatter_yaml=frontmatter_yaml,
                body_md=body_md,
                created_by=identity,
            )
            conn.commit()
            return JSONResponse(proc, status_code=201)
    except DuplicateProcedureNameError as exc:
        return JSONResponse({"code": "nom_deja_utilise", "message": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"code": "frontmatter_invalide", "message": str(exc)}, status_code=422)
    except Exception as exc:
        logger.warning("context_api: create_procedure error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la création de la procédure"},
            status_code=500,
        )


async def _list_procedures(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    include_archived = request.query_params.get("status") == "archived"

    from core.context_store import list_procedures  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if project_id and not _check_project_role(
                conn, project_id, identity, minimum_role="viewer"
            ):
                _audit_cross_scope(identity, "procedures_list", project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"}, status_code=404
                )

            procs = list_procedures(conn, project_id=project_id, include_archived=include_archived)
            return JSONResponse({"procedures": procs})
    except Exception as exc:
        logger.warning("context_api: list_procedures error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération des procédures"},
            status_code=500,
        )


async def _get_procedure_by_id(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    proc_id = request.path_params.get("id", "")

    from core.context_store import get_procedure  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # Fetch unfiltered so a cross-scope hit is detected and audited below
            # (the handler-level role check is the enforcement point).
            proc = get_procedure(conn, procedure_id=proc_id)
            if not proc:
                return JSONResponse(
                    {"code": "not_found", "message": "Procédure introuvable"}, status_code=404
                )

            # Handler-level scope check (outer layer; store-level is belt-and-suspenders).
            if proc["project_id"] and not _check_project_role(
                conn, proc["project_id"], identity, minimum_role="viewer"
            ):
                _audit_cross_scope(identity, proc_id, proc["project_id"])
                return JSONResponse(
                    {"code": "not_found", "message": "Procédure introuvable"}, status_code=404
                )

            return JSONResponse(proc)
    except Exception as exc:
        logger.warning("context_api: get_procedure error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération de la procédure"},
            status_code=500,
        )


async def _update_procedure(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    proc_id = request.path_params.get("id", "")
    try:
        patch = await request.json()
    except Exception:
        patch = {}

    from core.context_store import (  # noqa: PLC0415
        DuplicateProcedureNameError,
        get_procedure,
        update_procedure,
    )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # Fetch unfiltered so a cross-scope attempt is audited below.
            proc = get_procedure(conn, procedure_id=proc_id)
            if not proc:
                _audit_cross_scope(identity, proc_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Procédure introuvable"}, status_code=404
                )

            if not _check_project_role(
                conn, proc["project_id"], identity, minimum_role="member", is_write=True
            ):
                _audit_cross_scope(identity, proc_id, proc["project_id"])
                return JSONResponse(
                    {"code": "not_found", "message": "Procédure introuvable"}, status_code=404
                )

            updated = update_procedure(conn, procedure_id=proc_id, patch=patch, changed_by=identity)
            conn.commit()
            return JSONResponse(updated)
    except DuplicateProcedureNameError as exc:
        return JSONResponse({"code": "nom_deja_utilise", "message": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"code": "frontmatter_invalide", "message": str(exc)}, status_code=422)
    except KeyError as exc:
        return JSONResponse({"code": "not_found", "message": str(exc)}, status_code=404)
    except Exception as exc:
        logger.warning("context_api: update_procedure error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la mise à jour de la procédure"},
            status_code=500,
        )


async def _archive_procedure(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    proc_id = request.path_params.get("id", "")

    from core.context_store import archive_procedure, get_procedure  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # No caller_project_id from path/body for archive;
            # handler-level check is primary (Fix 4).
            proc = get_procedure(conn, procedure_id=proc_id)
            if not proc:
                _audit_cross_scope(identity, proc_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Procédure introuvable"}, status_code=404
                )

            if not _check_project_role(
                conn, proc["project_id"], identity, minimum_role="member", is_write=True
            ):
                _audit_cross_scope(identity, proc_id, proc["project_id"])
                return JSONResponse(
                    {"code": "not_found", "message": "Procédure introuvable"}, status_code=404
                )

            archived = archive_procedure(conn, procedure_id=proc_id, changed_by=identity)
            conn.commit()
            return JSONResponse(archived)
    except KeyError as exc:
        return JSONResponse({"code": "not_found", "message": str(exc)}, status_code=404)
    except Exception as exc:
        logger.warning("context_api: archive_procedure error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de l'archivage de la procédure"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Graph Edge Handlers (Story 11.4)
# ---------------------------------------------------------------------------


async def _list_graph_edges(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None

    from core.context_store import list_graph_edges  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if project_id and not _check_project_role(
                conn, project_id, identity, minimum_role="viewer"
            ):
                _audit_cross_scope(identity, "graph_edges_list", project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"}, status_code=404
                )

            edges = list_graph_edges(conn, project_id=project_id)
            return JSONResponse({"edges": edges})
    except Exception as exc:
        logger.warning("context_api: list_graph_edges error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération des liaisons"},
            status_code=500,
        )


async def _create_graph_edge(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    try:
        body = await request.json()
    except Exception:
        body = {}

    project_id = (body.get("project_id") or "").strip() or None
    from_id = (body.get("from_id") or "").strip()
    from_type = (body.get("from_type") or "").strip()
    to_id = (body.get("to_id") or "").strip()
    to_type = (body.get("to_type") or "").strip()
    edge_type = (body.get("edge_type") or "").strip()

    from core.context_store import create_graph_edge  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _check_project_role(
                conn, project_id, identity, minimum_role="member", is_write=True
            ):
                _audit_cross_scope(identity, "graph_edge_create", project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"}, status_code=404
                )

            edge = create_graph_edge(
                conn,
                project_id=project_id,
                from_id=from_id,
                from_type=from_type,
                to_id=to_id,
                to_type=to_type,
                edge_type=edge_type,
                created_by=identity,
            )
            conn.commit()
            return JSONResponse(edge, status_code=201)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)
    except Exception as exc:
        logger.warning("context_api: create_graph_edge error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la création de la liaison"},
            status_code=500,
        )


async def _delete_graph_edge(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    edge_id = request.path_params.get("id", "")

    from core.context_store import delete_graph_edge, get_graph_edge  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # Fetch unfiltered first so cross-scope attempts are audited (non-disclosing).
            edge = get_graph_edge(conn, edge_id=edge_id)
            if not edge:
                _audit_cross_scope(identity, edge_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Liaison introuvable"}, status_code=404
                )

            # Enforce scope: platform-scope edge requires platform write auth;
            # project-scope edge requires Member in that project.
            if not _check_project_role(
                conn, edge["project_id"], identity, minimum_role="member", is_write=True
            ):
                _audit_cross_scope(identity, edge_id, edge["project_id"])
                return JSONResponse(
                    {"code": "not_found", "message": "Liaison introuvable"}, status_code=404
                )

            delete_graph_edge(conn, edge_id=edge_id, deleted_by=identity)
            conn.commit()
            return Response(status_code=204)
    except Exception as exc:
        logger.warning("context_api: delete_graph_edge error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la suppression de la liaison"},
            status_code=500,
        )


# NOTE (scope creep — Fix 8): _generate_schema_context was wired into CONTEXT_ROUTES in the
# initial implementation, but it belongs to Story 11.2 (schema context auto-generation).
# It imported core.schema_context_gen which is unreviewed 11.2 scope and its
# upsert_schema_context_doc
# mutates schema_context rows in place (AD-17 concern).
# DECISION: the route and its handler are REMOVED from 11.1's CONTEXT_ROUTES.
# The schema_context TABLE remains (legitimate 11.1 deliverable / 11.2's write target).
# The schema_context_gen.py file is left untouched on disk for the separate 11.2 review.
# The route will be reintroduced under a dedicated 11.2 route module after that review.

CONTEXT_ROUTES: list[Route] = [
    Route("/api/context/topics", endpoint=_create_topic, methods=["POST"]),
    Route("/api/context/topics", endpoint=_list_topics, methods=["GET"]),
    Route("/api/context/topics/{id}", endpoint=_get_topic, methods=["GET"]),
    Route("/api/context/topics/{id}", endpoint=_update_topic, methods=["PATCH"]),
    Route("/api/context/topics/{id}/archive", endpoint=_archive_topic, methods=["POST"]),
    Route("/api/context/procedures", endpoint=_create_procedure, methods=["POST"]),
    Route("/api/context/procedures", endpoint=_list_procedures, methods=["GET"]),
    Route("/api/context/procedures/{id}", endpoint=_get_procedure_by_id, methods=["GET"]),
    Route("/api/context/procedures/{id}", endpoint=_update_procedure, methods=["PATCH"]),
    Route("/api/context/procedures/{id}/archive", endpoint=_archive_procedure, methods=["POST"]),
    # Story 11.4 — graph edge routes (AD-5 scoping, deny-by-default platform writes)
    Route("/api/context/graph/edges", endpoint=_list_graph_edges, methods=["GET"]),
    Route("/api/context/graph/edges", endpoint=_create_graph_edge, methods=["POST"]),
    Route("/api/context/graph/edges/{id}", endpoint=_delete_graph_edge, methods=["DELETE"]),
]
