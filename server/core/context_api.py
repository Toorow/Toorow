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
  GET  /api/context/graph                 -> One-bundle read: nodes + edges (Story 44.3)
                                              node payload includes a "doc_kind" key
                                              (schema_doc nodes only; null/absent for
                                              topic/procedure nodes) so the mindmap
                                              (44.4) can render a badge and disambiguate
                                              same-relation schema docs of different kinds.
                                              topic/procedure nodes also carry "owner"
                                              (resolved: explicit -> created_by -> "auto")
                                              and "owner_raw" (the explicit column value,
                                              string or null) -- Story 44.11.
  POST /api/context/nodes/{id}/request-review -> Audit-only "request review" intent
                                              capture for a topic/procedure node
                                              (Story 44.11). No notification delivery.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import psycopg
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import (
    ACTION_CONTEXT_REVIEW_REQUESTED,
    ACTION_CROSS_SCOPE_ATTEMPT,
    insert_audit_row,
    write_audit_row,
)

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
    owner = body.get("owner")

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
                conn,
                project_id=project_id,
                title=title,
                body_md=body_md,
                owner=owner,
                created_by=identity,
            )
            conn.commit()
            return JSONResponse(topic, status_code=201)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)
    except psycopg.errors.UniqueViolation:
        # Migration 113: platform-scoped titles are unique (partial index) --
        # a duplicate is a curator-facing conflict, not a 500 (44.2 re-review).
        return JSONResponse(
            {"code": "conflict", "message": "Un topic plateforme porte déjà ce titre"},
            status_code=409,
        )
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
    except psycopg.errors.UniqueViolation:
        # Migration 113: renaming a platform topic onto an existing platform
        # title now violates the partial unique index -- 409, not 500.
        return JSONResponse(
            {"code": "conflict", "message": "Un topic plateforme porte déjà ce titre"},
            status_code=409,
        )
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

    from core.context_store import (  # noqa: PLC0415
        DuplicateProcedureNameError,
        _clean_owner,
        create_procedure,
    )
    from core.db import get_connection  # noqa: PLC0415

    # Validate owner HERE so its type error reports invalid_param, matching the
    # topics endpoint -- the generic ValueError branch below is reserved for
    # frontmatter problems and reports frontmatter_invalide (44.11 re-review).
    try:
        owner = _clean_owner(body.get("owner"))
    except ValueError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)

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
                owner=owner,
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
        _clean_owner,
        get_procedure,
        update_procedure,
    )
    from core.db import get_connection  # noqa: PLC0415

    # Owner type error -> invalid_param, matching the topics endpoint; the
    # generic ValueError branch below stays frontmatter_invalide (44.11
    # re-review).
    if "owner" in patch:
        try:
            patch["owner"] = _clean_owner(patch["owner"])
        except ValueError as exc:
            return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)

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


# ---------------------------------------------------------------------------
# Graph Bundle Handler (Story 44.3)
# ---------------------------------------------------------------------------

_EXCERPT_LEN = 280


def _excerpt(body_md: str | None) -> str:
    """Verbatim first 280-char prefix of a doc body, server-side (R44-UX03)."""
    if not body_md:
        return ""
    return body_md[:_EXCERPT_LEN]


def _node_scope(node_project_id: str | None) -> str:
    return "platform" if node_project_id is None else "project"


def _resolve_owner(owner: str | None, created_by: str | None) -> str:
    """Story 44.11 display rule: explicit owner, else created_by, else 'auto'.

    Centralised here (the graph bundle) so every consumer of the mindmap
    agrees on the same displayed owner -- topics/procedures created before
    this story have no explicit `owner` and fall back to `created_by`;
    seeded/system rows with neither fall back to the literal "auto".
    """
    if owner:
        return owner
    if created_by:
        return created_by
    return "auto"


async def _get_graph(request: Request) -> Response:
    """GET /api/context/graph?project_id=<id> -- one bundle of nodes + edges.

    Composes topics + procedures + schema_docs into a single node list (orphans
    included, no edge required) alongside the project's visible graph edges.
    project_id is required: the mindmap always renders for one project's scope.

    schema_doc nodes carry a "doc_kind" key (e.g. "columns", "sample_values") so
    that up to 3 schema_doc nodes sharing the same relation title (one per
    doc_kind) remain distinguishable to the consumer; topic/procedure nodes
    OMIT the key entirely (consumers must treat a missing doc_kind as none).

    Story 44.10 adds a fourth node type: target_field (a data-dictionary field).
    Its node id is the field NAME (app.target_fields has no id column), its
    scope is always "platform" (the dictionary is platform-global -- no org or
    project column), and it carries an extra "field_kind" key ("metric" /
    "dimension"). Inclusion rule -- the whole dictionary is NOT drawn by
    default: only fields referenced by at least one visible edge, plus every
    APPROVED field when ``?include_fields=all`` is passed. Soft-deleted fields
    are never included, in either mode.

    Edges are filtered post-hoc to those whose from_id AND to_id both resolve to
    a node actually present in the returned node list -- an edge pointing at an
    archived or otherwise out-of-scope endpoint is dropped rather than left
    dangling for the React Flow consumer.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id est requis"}, status_code=422
        )

    include_fields_all = (request.query_params.get("include_fields") or "").strip() == "all"

    from core.context_store import (  # noqa: PLC0415
        list_graph_edges,
        list_procedures,
        list_schema_docs,
        list_topics,
    )
    from core.datamodel import list_target_field_nodes  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _check_project_role(conn, project_id, identity, minimum_role="viewer"):
                _audit_cross_scope(identity, "graph_read", project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"}, status_code=404
                )

            topics = list_topics(conn, project_id=project_id, include_archived=False)
            procedures = list_procedures(conn, project_id=project_id, include_archived=False)
            schema_docs = list_schema_docs(conn, project_id=project_id)
            edges = list_graph_edges(conn, project_id=project_id)

            nodes: list[dict[str, Any]] = []
            for topic in topics:
                nodes.append(
                    {
                        "id": topic["id"],
                        "node_type": "topic",
                        "title": topic["title"],
                        "excerpt": _excerpt(topic["body_md"]),
                        # Story 44.11: resolved for display; owner_raw is the
                        # explicit column value (string or null) editors need
                        # to distinguish "no explicit owner" from "explicit
                        # owner happens to equal created_by".
                        "owner": _resolve_owner(topic.get("owner"), topic["created_by"]),
                        "owner_raw": topic.get("owner"),
                        "version_number": topic["version_number"],
                        "scope": _node_scope(topic["project_id"]),
                        "status": topic["status"],
                    }
                )
            for proc in procedures:
                nodes.append(
                    {
                        "id": proc["id"],
                        "node_type": "procedure",
                        "title": proc["name"],
                        "excerpt": _excerpt(proc["body_md"]),
                        "owner": _resolve_owner(proc.get("owner"), proc["created_by"]),
                        "owner_raw": proc.get("owner"),
                        "version_number": proc["version_number"],
                        "scope": _node_scope(proc["project_id"]),
                        "status": proc["status"],
                    }
                )
            for doc in schema_docs:
                # app.schema_context has no created_by/status columns (AD-17: read-only,
                # generator-written docs) and project_id is NOT NULL, so scope is always
                # 'project' and owner is not fabricated.
                # "status" is deliberately the constant "active" in this v1: schema_context
                # rows are not archivable today, so there is no include_archived param on
                # this endpoint yet. The field is kept on the payload for forward
                # compatibility with topic/procedure nodes (which do carry real status).
                nodes.append(
                    {
                        "id": doc["id"],
                        "node_type": "schema_doc",
                        "title": doc["relation"],
                        "doc_kind": doc["doc_kind"],
                        "excerpt": _excerpt(doc["body_md"]),
                        "owner": None,
                        "version_number": doc["version_number"],
                        "scope": "project",
                        "status": "active",
                    }
                )

            # Story 44.10 -- target_field nodes. Referenced = named by an edge
            # endpoint whose type is 'target_field'. The endpoint id IS the
            # field name, so the referenced set is read straight off the edges
            # already fetched (no extra round trip, no whole-dictionary scan).
            # Both ends are inspected independently so a field->field edge
            # contributes BOTH of its endpoints, not just one.
            referenced_fields: set[str] = set()
            for edge in edges:
                if edge["from_type"] == "target_field":
                    referenced_fields.add(edge["from_id"])
                if edge["to_type"] == "target_field":
                    referenced_fields.add(edge["to_id"])
            field_rows = list_target_field_nodes(
                conn,
                names=sorted(referenced_fields),
                include_approved=include_fields_all,
            )
            for field in field_rows:
                nodes.append(
                    {
                        # app.target_fields is keyed by name -- there is no id
                        # column, so the name IS the node id (and what edges
                        # store in from_id/to_id).
                        "id": field["name"],
                        "node_type": "target_field",
                        # display_name is nullable in the dictionary; the field
                        # name is the honest fallback, never a fabricated label.
                        "title": field["display_name"] or field["name"],
                        # Verbatim (prefix-capped like every other node type);
                        # a field with no description gets "" rather than an
                        # invented summary.
                        "excerpt": _excerpt(field["description"]),
                        "owner": field["created_by"],
                        "version_number": field["version_number"],
                        # The dictionary is platform-global: no org_id, no
                        # project_id column at all (see core.datamodel).
                        "scope": "platform",
                        "status": field["status"],
                        # 'metric' | 'dimension' -- rendered as the node card's
                        # kind badge. target_field nodes only.
                        "field_kind": field["field_kind"],
                    }
                )

            node_ids = {node["id"] for node in nodes}
            # 44.10 re-review: target_field ids are bare field names sharing the
            # id namespace with top_/proc_/sctx_ ids. A collision cannot be
            # designed away (edges store the raw name) -- make it observable
            # instead of silently collapsing two nodes into one.
            if len(node_ids) != len(nodes):
                seen: set[str] = set()
                dupes = {n["id"] for n in nodes if n["id"] in seen or seen.add(n["id"])}
                logger.warning(
                    "context_api: graph_node_id_collision project=%s ids=%s",
                    project_id, sorted(dupes),
                )
            edges = [
                edge
                for edge in edges
                if edge["from_id"] in node_ids and edge["to_id"] in node_ids
            ]

            return JSONResponse({"nodes": nodes, "edges": edges})
    except Exception as exc:
        logger.warning("context_api: get_graph error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération du graphe"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Request Review Handler (Story 44.11)
# ---------------------------------------------------------------------------


async def _request_review(request: Request) -> Response:
    """POST /api/context/nodes/{id}/request-review -- audit-only intent capture.

    Writes an `context.review_requested` audit row with {node_id, node_type,
    requester, note}. Deliberately does NOT deliver a notification of any
    kind (out of scope, per the story record) -- the caller is told exactly
    that in the response the UI renders, never implying a message was sent.

    Body: {"node_type": "topic" | "procedure", "note"?: string}. node_type is
    required and explicit (the same pattern as graph edge creation) rather
    than inferred from the id prefix, so a malformed/unknown id shape never
    silently resolves to the wrong lookup.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
        )

    node_id = request.path_params.get("id", "")
    try:
        body = await request.json()
    except Exception:
        body = {}

    node_type = (body.get("node_type") or "").strip()
    note_raw = body.get("note")
    note = note_raw.strip() if isinstance(note_raw, str) else None
    note = note or None

    if node_type not in ("topic", "procedure"):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "node_type doit être 'topic' ou 'procedure'.",
            },
            status_code=422,
        )

    from core.context_store import get_procedure, get_topic  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            node = (
                get_topic(conn, topic_id=node_id)
                if node_type == "topic"
                else get_procedure(conn, procedure_id=node_id)
            )
            if not node:
                _audit_cross_scope(identity, node_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Nœud introuvable"}, status_code=404
                )

            # Viewer role suffices (AC: any knowledge consumer can flag a node
            # for review, not just its writers).
            if not _check_project_role(
                conn, node["project_id"], identity, minimum_role="viewer"
            ):
                _audit_cross_scope(identity, node_id, node["project_id"])
                return JSONResponse(
                    {"code": "not_found", "message": "Nœud introuvable"}, status_code=404
                )

            insert_audit_row(
                conn,
                identity=identity,
                action=ACTION_CONTEXT_REVIEW_REQUESTED,
                provider_account="platform",
                connection_ref="",
                metadata={
                    "node_id": node_id,
                    "node_type": node_type,
                    "requester": identity,
                    "note": note,
                },
            )
            conn.commit()
            return JSONResponse({"status": "requested"}, status_code=201)
    except Exception as exc:
        logger.warning("context_api: request_review error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la demande de revue"},
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
    # Story 44.3 — one-bundle graph read (nodes + edges)
    Route("/api/context/graph", endpoint=_get_graph, methods=["GET"]),
    # Story 44.11 — request review (audit-only intent capture, no delivery)
    Route(
        "/api/context/nodes/{id}/request-review", endpoint=_request_review, methods=["POST"]
    ),
]
