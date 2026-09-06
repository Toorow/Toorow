"""toorow -- Context layer REST route handlers (Story 11.1 / 11.4).

Provides CONTEXT_ROUTES: list[Route] -- spliced into admin_api.router at startup.

Routes:
  POST /api/context/topics                -> Create topic
  GET  /api/context/topics                -> List topics (visible scope)
  GET  /api/context/topics/{id}           -> Get topic by ID
  PATCH /api/context/topics/{id}          -> Update topic
  POST /api/context/topics/{id}/archive   -> Archive topic
  POST /api/context/topics/{id}/restore   -> Restore an archived topic. An
                                              archive is a version, not a
                                              deletion, so the way back is a
                                              version too (2026-08-18). Refuses
                                              a topic that is not archived.
  POST /api/context/procedures            -> Create procedure
  GET  /api/context/procedures            -> List procedures (visible scope)
  GET  /api/context/procedures/{id}       -> Get procedure by ID
  PATCH /api/context/procedures/{id}      -> Update procedure
  POST /api/context/procedures/{id}/archive -> Archive procedure
  POST /api/context/procedures/{id}/restore -> Restore an archived procedure
  GET  /api/context/graph/edges           -> List graph edges (visible scope, AD-5)
  POST /api/context/graph/edges           -> Create graph edge. Scope in
                                              ?project_id= (absent = platform,
                                              deny-by-default); a body
                                              project_id is accepted only when
                                              it repeats the query one.
  DELETE /api/context/graph/edges/{id}    -> Retire the relation this edge
                                              projects. Story 49-6 AC5: a
                                              supersede, never a delete -- the
                                              relation and its version history
                                              survive, the projection row does
                                              not.
  GET  /api/context/relationships         -> The "Used by / Related" facet of one
                                              object, both directions, bounded,
                                              with exact owner links (49-6 AC6).
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
  POST /api/context/nodes/{id}/request-review -> Deposer une remarque en file sur
                                              un noeud topic/procedure, A SA VERSION
                                              (Story 44.11 + AI-157). Ecrit la ligne
                                              d'audit ET la ligne de file. Aucune
                                              notification n'est delivree, et la
                                              reponse ne pretend pas le contraire.
  GET  /api/context/review-requests       -> La file : ce qui reste a trancher
  POST /api/context/review-requests/{id}/resolve -> Clore : accepted | declined,
                                              en appliquant la charge s'il y en a une
  GET  /api/context/recurrent-fates       -> Le repli : ce que la recherche ecarte
                                              a repetition, donc un lien manquant
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
    ACTION_CROSS_SCOPE_ATTEMPT,
    declare_action,
    insert_audit_row,
    write_audit_row,
)
from core.row_json import RowJSON

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_CONTEXT_REVIEW_REQUESTED = declare_action("context.review_requested")


logger = logging.getLogger(__name__)


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def check_platform_write_authorized(conn: Any, identity: str) -> bool:
    """Deny-by-default platform-scope write gate (Fix 3 / HIGH).

    Policy v1: allow only if the identity is listed in the CONTEXT_PLATFORM_WRITERS
    env var (comma-separated), OR the auth mode is 'disabled' (anonymous → owner dev
    mapping from strict capability / project_access).

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
    from core.project_access import identity_has_project_role, project_exists  # noqa: PLC0415

    if project_id is not None:
        # Disabled-auth dev mode: the same documented bypass as the canonical
        # strict seam (admin_api._strict_project_capability_allowed). Without
        # it the graph endpoints answered 404 in `make dev` while every other
        # context surface answered -- two seams, two truths (live finding A1,
        # 2026-08-05).
        #
        # It grants the capability, never the project's existence: an unknown
        # id is a 404 here as it is under OAuth, instead of a 200 carrying an
        # empty graph that reads as "this project has no context yet" (live
        # finding C1).
        auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
        if auth_mode == "disabled" and identity == "anonymous":
            return project_exists(project_id, conn)
        try:
            return identity_has_project_role(project_id, identity, minimum_role, conn)
        except Exception:
            return False

    # Platform scope
    if is_write:
        return check_platform_write_authorized(conn, identity)
    # Platform reads: any authenticated identity (documented: AC-4 / Fix 5).
    return True


def _strict_url_project_allowed(
    conn: Any,
    *,
    identity: str,
    project_id: str,
    minimum_capability: str,
) -> bool:
    """Authorize the explicit URL project through the canonical strict seam."""
    try:
        from core.admin_api import _strict_project_capability_allowed  # noqa: PLC0415

        return _strict_project_capability_allowed(
            conn,
            identity=identity,
            project_id=project_id,
            minimum_capability=minimum_capability,
            hold_access=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "context_api: project access unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return False


def _required_url_project(request: Request) -> str | None:
    return (request.query_params.get("project_id") or "").strip() or None


def _body_project_scope(
    body: dict[str, Any], project_id: str
) -> tuple[bool, JSONResponse | None]:
    """Validate duplicated body scope and report whether it differs from the URL."""
    if "project_id" not in body:
        return False, None
    body_project_id = body.get("project_id")
    if not isinstance(body_project_id, str):
        return False, JSONResponse(
            {"code": "invalid_param", "message": "project_id must be a string"},
            status_code=422,
        )
    return body_project_id.strip() != project_id, None



def _context_capabilities(*, can_write: bool, version_history: bool) -> dict[str, bool]:
    return {
        "can_write": can_write,
        "version_history": version_history,
        "usage": False,
    }


def _context_not_found(label: str = "Project not found") -> JSONResponse:
    return RowJSON({"code": "not_found", "message": label}, status_code=404)


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
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    body_project_present = "project_id" in body
    body_project_id = body.get("project_id")
    if body_project_present and not isinstance(body_project_id, str):
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id must be a string"},
            status_code=422,
        )
    body_project_mismatch = body_project_present and body_project_id != project_id
    title = body.get("title", "")
    body_md = body.get("body_md", "")
    owner = body.get("owner")

    from core.context_store import PayloadTooLargeError, create_topic  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                _audit_cross_scope(identity, "topic_create", project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Project not found"}, status_code=404
                )
            if body_project_mismatch:
                return _context_not_found("Project not found")

            topic = create_topic(
                conn,
                project_id=project_id,
                title=title,
                body_md=body_md,
                owner=owner,
                created_by=identity,
            )
            conn.commit()
            return RowJSON(topic, status_code=201)
    except PayloadTooLargeError as exc:
        # Meme contrat que feedback_*_api.py : un corps refuse pour sa taille
        # est un 413 body_too_large, pas un 422.
        return JSONResponse({"code": "body_too_large", "message": str(exc)}, status_code=413)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)
    except psycopg.errors.UniqueViolation:
        # Migration 113: platform-scoped titles are unique (partial index) --
        # a duplicate is a curator-facing conflict, not a 500 (44.2 re-review).
        return JSONResponse(
            {"code": "conflict", "message": "A platform topic already uses this title"},
            status_code=409,
        )
    except Exception as exc:
        logger.warning("context_api: create_topic error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to create topic"}, status_code=500
        )


async def _list_topics(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    status_filter = (request.query_params.get("status") or "active").strip() or "active"
    if status_filter not in ("active", "archived", "all"):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "status must be one of: active, archived, all",
            },
            status_code=422,
        )

    from core.context_store import list_topics  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _context_not_found()
            can_write = _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            )
            topics = list_topics(conn, project_id=project_id, status=status_filter)
            platform_write = can_write and check_platform_write_authorized(conn, identity)
            for topic in topics:
                topic["capabilities"] = _context_capabilities(
                    can_write=can_write
                    if topic.get("project_id") is not None
                    else platform_write,
                    version_history=True,
                )
            return RowJSON(
                {
                    "topics": topics,
                    "capabilities": _context_capabilities(
                        can_write=can_write, version_history=True
                    ),
                }
            )
    except Exception as exc:
        logger.warning("context_api: list_topics error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to retrieve topics"},
            status_code=500,
        )

async def _get_topic(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    topic_id = request.path_params.get("id", "")

    from core.context_store import get_topic  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _context_not_found("Topic not found")
            topic = get_topic(conn, topic_id=topic_id, caller_project_id=project_id)
            if not topic:
                return _context_not_found("Topic not found")
            # The WORKBENCH reads this route, and a workbench that cannot ask
            # "may I write here?" either hides an edit the caller is entitled to
            # or offers one the server will refuse. The list route has answered
            # this since Story 44.1; the detail route stayed silent, which is
            # why editing a context object lived only on the collection screen.
            # Same rule, same helper: a platform row needs the platform gate on
            # top of the project's edit capability.
            can_write = _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            )
            topic["capabilities"] = _context_capabilities(
                can_write=can_write
                if topic.get("project_id") is not None
                else can_write and check_platform_write_authorized(conn, identity),
                version_history=True,
            )
            return RowJSON(topic)
    except Exception as exc:
        logger.warning("context_api: get_topic error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to retrieve topic"},
            status_code=500,
        )

async def _update_topic(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    topic_id = request.path_params.get("id", "")
    try:
        patch = await request.json()
    except Exception:
        patch = None
    if not isinstance(patch, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )
    body_scope_mismatch, scope_error = _body_project_scope(patch, project_id)
    if scope_error is not None:
        return scope_error
    patch.pop("project_id", None)
    expected_version = patch.pop("expected_version", None)
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        return JSONResponse(
            {"code": "invalid_param", "message": "expected_version must be an integer"},
            status_code=422,
        )

    from core.context_store import (  # noqa: PLC0415
        ArchivedContextError,
        PayloadTooLargeError,
        StaleContextVersionError,
        get_topic,
        update_topic,
    )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _context_not_found("Topic not found")
            if body_scope_mismatch:
                return _context_not_found("Topic not found")
            topic = get_topic(conn, topic_id=topic_id, caller_project_id=project_id)
            if not topic:
                return _context_not_found("Topic not found")
            if topic["project_id"] is None and not check_platform_write_authorized(
                conn, identity
            ):
                return _context_not_found("Topic not found")
            updated = update_topic(
                conn,
                topic_id=topic_id,
                patch=patch,
                changed_by=identity,
                expected_version=expected_version,
            )
            conn.commit()
            return RowJSON(updated)
    except StaleContextVersionError as exc:
        return JSONResponse(
            {"code": "version_conflict", "message": str(exc)}, status_code=409
        )
    except ArchivedContextError as exc:
        return JSONResponse({"code": "archived", "message": str(exc)}, status_code=409)
    except PayloadTooLargeError as exc:
        return JSONResponse({"code": "body_too_large", "message": str(exc)}, status_code=413)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)
    except KeyError:
        return _context_not_found("Topic not found")
    except psycopg.errors.UniqueViolation:
        return JSONResponse(
            {"code": "conflict", "message": "A platform topic already uses this title"},
            status_code=409,
        )
    except Exception as exc:
        logger.warning("context_api: update_topic error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to update topic"},
            status_code=500,
        )

async def _archive_topic(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    topic_id = request.path_params.get("id", "")
    try:
        body = await request.json()
    except Exception:
        # AN ABSENT BODY IS FINE HERE; A MALFORMED ONE IS NOT. These handlers take
        # an OPTIONAL body, so "nothing sent" legitimately becomes `{}`.
        body = {}
    if not isinstance(body, dict):
        # A JSON PRIMITIVE IS NOT AN EMPTY BODY, and coercing it to `{}` sent a
        # malformed request all the way to the database, which then answered 404
        # -- "not found" about an object the caller never got to name. Five
        # handlers did this (`_archive_topic`, `_archive_procedure`,
        # `_create_graph_edge`, `_request_review`, `_resolve_review_request`)
        # while the four that validate properly refused at 422 before opening a
        # connection. Same door, two answers to the same malformed request.
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )
    body_scope_mismatch, scope_error = _body_project_scope(body, project_id)
    if scope_error is not None:
        return scope_error
    expected_version = body.get("expected_version")
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "expected_version must be a positive integer if provided",
            },
            status_code=422,
        )

    from core.context_store import (  # noqa: PLC0415
        StaleContextVersionError,
        archive_topic,
        get_topic,
    )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _context_not_found("Topic not found")
            if body_scope_mismatch:
                return _context_not_found("Topic not found")
            topic = get_topic(conn, topic_id=topic_id, caller_project_id=project_id)
            if not topic:
                return _context_not_found("Topic not found")
            if topic["project_id"] is None and not check_platform_write_authorized(
                conn, identity
            ):
                return _context_not_found("Topic not found")
            archived = archive_topic(
                conn,
                topic_id=topic_id,
                changed_by=identity,
                expected_version=expected_version,
            )
            conn.commit()
            return RowJSON(archived)
    except StaleContextVersionError as exc:
        return JSONResponse(
            {"code": "version_conflict", "message": str(exc)}, status_code=409
        )
    except KeyError:
        return _context_not_found("Topic not found")
    except Exception as exc:
        logger.warning("context_api: archive_topic error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to archive topic"},
            status_code=500,
        )


async def _restore_topic(request: Request) -> Response:
    """Undo an archive. Same door, same guards, same version semantics.

    An archive was never a deletion -- it is a version like any other -- so the
    way back is a version too. Everything the archive handler checks is checked
    here in the same order; the ONE thing this route adds is that a topic which
    is not archived is refused rather than silently answered "done".
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    topic_id = request.path_params.get("id", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )
    body_scope_mismatch, scope_error = _body_project_scope(body, project_id)
    if scope_error is not None:
        return scope_error
    expected_version = body.get("expected_version")
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "expected_version must be a positive integer if provided",
            },
            status_code=422,
        )

    from core.context_store import (  # noqa: PLC0415
        NotArchivedContextError,
        StaleContextVersionError,
        get_topic,
        restore_topic,
    )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _context_not_found("Topic not found")
            if body_scope_mismatch:
                return _context_not_found("Topic not found")
            topic = get_topic(conn, topic_id=topic_id, caller_project_id=project_id)
            if not topic:
                return _context_not_found("Topic not found")
            if topic["project_id"] is None and not check_platform_write_authorized(
                conn, identity
            ):
                return _context_not_found("Topic not found")
            restored = restore_topic(
                conn,
                topic_id=topic_id,
                changed_by=identity,
                expected_version=expected_version,
            )
            conn.commit()
            return RowJSON(restored)
    except NotArchivedContextError as exc:
        return JSONResponse({"code": "not_archived", "message": str(exc)}, status_code=409)
    except StaleContextVersionError as exc:
        return JSONResponse(
            {"code": "version_conflict", "message": str(exc)}, status_code=409
        )
    except KeyError:
        return _context_not_found("Topic not found")
    except psycopg.errors.UniqueViolation:
        return JSONResponse(
            {"code": "conflict", "message": "A platform topic already uses this title"},
            status_code=409,
        )
    except Exception as exc:
        logger.warning("context_api: restore_topic error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to restore topic"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Procedures Handlers
# ---------------------------------------------------------------------------


async def _create_procedure(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    body_project_present = "project_id" in body
    body_project_id = body.get("project_id")
    if body_project_present and not isinstance(body_project_id, str):
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id must be a string"},
            status_code=422,
        )
    body_project_mismatch = body_project_present and body_project_id != project_id
    frontmatter_yaml = body.get("frontmatter_yaml", "")
    body_md = body.get("body_md", "")

    from core.context_store import (  # noqa: PLC0415
        DuplicateProcedureNameError,
        PayloadTooLargeError,
        _clean_owner,
        create_procedure,
    )
    from core.db import get_connection  # noqa: PLC0415

    # Validate owner HERE so its type error reports invalid_param, matching the
    # topics endpoint -- the generic ValueError branch below is reserved for
    # frontmatter problems and reports invalid_frontmatter (44.11 re-review).
    try:
        owner = _clean_owner(body.get("owner"))
    except ValueError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                _audit_cross_scope(identity, "procedure_create", project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Project not found"}, status_code=404
                )
            if body_project_mismatch:
                return _context_not_found("Project not found")

            proc = create_procedure(
                conn,
                project_id=project_id,
                frontmatter_yaml=frontmatter_yaml,
                body_md=body_md,
                owner=owner,
                created_by=identity,
            )
            conn.commit()
            return RowJSON(proc, status_code=201)
    except DuplicateProcedureNameError as exc:
        return JSONResponse({"code": "duplicate_name", "message": str(exc)}, status_code=409)
    except PayloadTooLargeError as exc:
        return JSONResponse({"code": "body_too_large", "message": str(exc)}, status_code=413)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_frontmatter", "message": str(exc)}, status_code=422)
    except Exception as exc:
        logger.warning("context_api: create_procedure error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to create procedure"},
            status_code=500,
        )


async def _list_procedures(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    status_filter = (request.query_params.get("status") or "active").strip() or "active"
    projection = (request.query_params.get("projection") or "full").strip() or "full"
    if projection not in ("full", "analysis-picker"):
        return JSONResponse(
            {"code": "invalid_param", "message": "unknown procedure projection"},
            status_code=422,
        )
    if status_filter not in ("active", "archived", "all"):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "status must be one of: active, archived, all",
            },
            status_code=422,
        )

    from core.context_store import list_procedure_picker, list_procedures  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _context_not_found()
            can_write = _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            )
            if projection == "analysis-picker":
                procedures = list_procedure_picker(
                    conn, project_id=project_id, status=status_filter, limit=201
                )
            else:
                procedures = list_procedures(
                    conn, project_id=project_id, status=status_filter
                )
            truncated = projection == "analysis-picker" and len(procedures) > 200
            if projection == "analysis-picker":
                procedures = [
                    {
                        "id": procedure["id"],
                        "name": str(procedure.get("name") or "")[:160],
                        "version_number": procedure.get("version_number"),
                    }
                    for procedure in procedures[:200]
                ]
            if projection == "full":
                platform_write = can_write and check_platform_write_authorized(conn, identity)
                for procedure in procedures:
                    procedure["capabilities"] = _context_capabilities(
                        can_write=can_write
                        if procedure.get("project_id") is not None
                        else platform_write,
                        version_history=True,
                    )
            return RowJSON(
                {
                    "procedures": procedures,
                    "truncated": truncated,
                    "capabilities": _context_capabilities(
                        can_write=can_write, version_history=True
                    ),
                }
            )
    except Exception as exc:
        logger.warning("context_api: list_procedures error: %s", type(exc).__name__)
        return JSONResponse(
            {
                "code": "db_error",
                "message": "Failed to retrieve procedures",
            },
            status_code=500,
        )

async def _get_procedure_by_id(request: Request) -> Response:
    """GET /api/context/procedures/{id}?project_id=<id> -- la Skill, ET ce qu'elle
    DESIGNE du modele de donnees.

    ⚠️ `mdm_references` NE VIVAIT QUE DANS L'OUTIL MCP (AI-157, 2026-08-04).
    `get_procedure` (`core/main.py`) resolvait les `{{champ}}` et les `mdm_tags`
    contre `app.target_fields` et les rendait dans son `structuredContent` ; la
    console, elle, rendait `SkillStepList` avec une prop `mdmReferences` que RIEN
    ne remplissait. Un agent savait donc de quoi parlait une Skill, et la
    personne qui l'ecrit ne le savait pas.

    RESOLU DANS LA MEME CONNEXION que la lecture, pour la meme raison qu'en MCP :
    deux connexions rendraient un catalogue d'un autre instant que la Skill.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    procedure_id = request.path_params.get("id", "")

    from core.context_store import get_procedure  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _context_not_found("Procedure not found")
            procedure = get_procedure(
                conn, procedure_id=procedure_id, caller_project_id=project_id
            )
            if not procedure:
                return _context_not_found("Procedure not found")
            from core import mdm_references  # noqa: PLC0415

            front = procedure.get("frontmatter_yaml") or ""
            procedure["mdm_references"] = mdm_references.resolve(
                conn,
                frontmatter_yaml=front,
                body_md=procedure.get("body_md") or "",
                mdm_tags=mdm_references.tags_of(front),
                # The Project whose Semantic Model answers first. Without it the
                # catalogue would consult the platform half only, and a Concept
                # this Project published would read as an unresolved reference.
                project_id=project_id,
            )
            # See `_get_topic`: the workbench is a reading surface that now also
            # answers a remark with a new version, and it must know whether the
            # caller may write before it offers the gesture.
            can_write = _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            )
            procedure["capabilities"] = _context_capabilities(
                can_write=can_write
                if procedure.get("project_id") is not None
                else can_write and check_platform_write_authorized(conn, identity),
                version_history=True,
            )
            return RowJSON(procedure)
    except Exception as exc:
        logger.warning("context_api: get_procedure error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to retrieve procedure"},
            status_code=500,
        )

async def _update_procedure(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    procedure_id = request.path_params.get("id", "")
    try:
        patch = await request.json()
    except Exception:
        patch = None
    if not isinstance(patch, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )
    expected_version = patch.pop("expected_version", None)
    body_scope_mismatch, scope_error = _body_project_scope(patch, project_id)
    if scope_error is not None:
        return scope_error
    patch.pop("project_id", None)
    if (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        return JSONResponse(
            {"code": "invalid_param", "message": "expected_version must be an integer"},
            status_code=422,
        )

    from core.context_store import (  # noqa: PLC0415
        ArchivedContextError,
        DuplicateProcedureNameError,
        PayloadTooLargeError,
        StaleContextVersionError,
        _clean_owner,
        get_procedure,
        update_procedure,
    )
    from core.db import get_connection  # noqa: PLC0415

    if "owner" in patch:
        try:
            patch["owner"] = _clean_owner(patch["owner"])
        except ValueError as exc:
            return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _context_not_found("Procedure not found")
            if body_scope_mismatch:
                return _context_not_found("Procedure not found")
            procedure = get_procedure(
                conn, procedure_id=procedure_id, caller_project_id=project_id
            )
            if not procedure:
                return _context_not_found("Procedure not found")
            if procedure["project_id"] is None and not check_platform_write_authorized(
                conn, identity
            ):
                return _context_not_found("Procedure not found")
            updated = update_procedure(
                conn,
                procedure_id=procedure_id,
                patch=patch,
                changed_by=identity,
                expected_version=expected_version,
            )
            conn.commit()
            return RowJSON(updated)
    except StaleContextVersionError as exc:
        return JSONResponse(
            {"code": "version_conflict", "message": str(exc)}, status_code=409
        )
    except ArchivedContextError as exc:
        return JSONResponse({"code": "archived", "message": str(exc)}, status_code=409)
    except DuplicateProcedureNameError as exc:
        return JSONResponse({"code": "duplicate_name", "message": str(exc)}, status_code=409)
    except PayloadTooLargeError as exc:
        return JSONResponse({"code": "body_too_large", "message": str(exc)}, status_code=413)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_frontmatter", "message": str(exc)}, status_code=422)
    except KeyError:
        return _context_not_found("Procedure not found")
    except Exception as exc:
        logger.warning("context_api: update_procedure error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to update procedure"},
            status_code=500,
        )

async def _archive_procedure(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    procedure_id = request.path_params.get("id", "")
    try:
        body = await request.json()
    except Exception:
        # AN ABSENT BODY IS FINE HERE; A MALFORMED ONE IS NOT. These handlers take
        # an OPTIONAL body, so "nothing sent" legitimately becomes `{}`.
        body = {}
    if not isinstance(body, dict):
        # A JSON PRIMITIVE IS NOT AN EMPTY BODY, and coercing it to `{}` sent a
        # malformed request all the way to the database, which then answered 404
        # -- "not found" about an object the caller never got to name. Five
        # handlers did this (`_archive_topic`, `_archive_procedure`,
        # `_create_graph_edge`, `_request_review`, `_resolve_review_request`)
        # while the four that validate properly refused at 422 before opening a
        # connection. Same door, two answers to the same malformed request.
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )
    expected_version = body.get("expected_version")
    body_scope_mismatch, scope_error = _body_project_scope(body, project_id)
    if scope_error is not None:
        return scope_error
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "expected_version must be a positive integer if provided",
            },
            status_code=422,
        )

    from core.context_store import (  # noqa: PLC0415
        StaleContextVersionError,
        archive_procedure,
        get_procedure,
    )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _context_not_found("Procedure not found")
            if body_scope_mismatch:
                return _context_not_found("Procedure not found")
            procedure = get_procedure(
                conn, procedure_id=procedure_id, caller_project_id=project_id
            )
            if not procedure:
                return _context_not_found("Procedure not found")
            if procedure["project_id"] is None and not check_platform_write_authorized(
                conn, identity
            ):
                return _context_not_found("Procedure not found")
            archived = archive_procedure(
                conn,
                procedure_id=procedure_id,
                changed_by=identity,
                expected_version=expected_version,
            )
            conn.commit()
            return RowJSON(archived)
    except StaleContextVersionError as exc:
        return JSONResponse(
            {"code": "version_conflict", "message": str(exc)}, status_code=409
        )
    except KeyError:
        return _context_not_found("Procedure not found")
    except Exception as exc:
        logger.warning("context_api: archive_procedure error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to archive procedure"},
            status_code=500,
        )


async def _restore_procedure(request: Request) -> Response:
    """Undo a Skill archive -- the exact mirror of `_restore_topic`.

    The store is symmetric, so the way back is opened on both objects at once.
    The Skills console surface comes later; a route the console does not yet
    call is still the capability, and half a symmetric store is a trap for the
    next reader.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    procedure_id = request.path_params.get("id", "")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )
    expected_version = body.get("expected_version")
    body_scope_mismatch, scope_error = _body_project_scope(body, project_id)
    if scope_error is not None:
        return scope_error
    if expected_version is not None and (
        isinstance(expected_version, bool)
        or not isinstance(expected_version, int)
        or expected_version < 1
    ):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "expected_version must be a positive integer if provided",
            },
            status_code=422,
        )

    from core.context_store import (  # noqa: PLC0415
        DuplicateProcedureNameError,
        NotArchivedContextError,
        StaleContextVersionError,
        get_procedure,
        restore_procedure,
    )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="edit",
            ):
                return _context_not_found("Procedure not found")
            if body_scope_mismatch:
                return _context_not_found("Procedure not found")
            procedure = get_procedure(
                conn, procedure_id=procedure_id, caller_project_id=project_id
            )
            if not procedure:
                return _context_not_found("Procedure not found")
            if procedure["project_id"] is None and not check_platform_write_authorized(
                conn, identity
            ):
                return _context_not_found("Procedure not found")
            restored = restore_procedure(
                conn,
                procedure_id=procedure_id,
                changed_by=identity,
                expected_version=expected_version,
            )
            conn.commit()
            return RowJSON(restored)
    except NotArchivedContextError as exc:
        return JSONResponse({"code": "not_archived", "message": str(exc)}, status_code=409)
    except StaleContextVersionError as exc:
        return JSONResponse(
            {"code": "version_conflict", "message": str(exc)}, status_code=409
        )
    except DuplicateProcedureNameError as exc:
        # The partial unique index covers non-archived rows only: while this
        # Skill was archived its name could be taken. Say which gesture repairs.
        return JSONResponse(
            {
                "code": "duplicate_name",
                "message": (
                    f"{exc} Rename the active procedure that now holds this name, "
                    "then restore this one."
                ),
            },
            status_code=409,
        )
    except KeyError:
        return _context_not_found("Procedure not found")
    except Exception as exc:
        logger.warning("context_api: restore_procedure error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to restore procedure"},
            status_code=500,
        )

async def _list_topic_versions(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )
    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    topic_id = request.path_params.get("id", "")

    from core.context_store import get_topic, list_topic_versions  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _context_not_found("Topic not found")
            topic = get_topic(conn, topic_id=topic_id, caller_project_id=project_id)
            if not topic:
                return _context_not_found("Topic not found")
            return RowJSON(
                {
                    "versions": list_topic_versions(
                        conn, topic_id=topic_id, caller_project_id=project_id
                    )
                }
            )
    except Exception as exc:
        logger.warning("context_api: topic_versions error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "History unavailable"}, status_code=500
        )


async def _list_procedure_versions(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )
    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    procedure_id = request.path_params.get("id", "")


    from core.context_store import (  # noqa: PLC0415
        get_procedure,
        list_procedure_versions,
    )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _context_not_found("Procedure not found")
            procedure = get_procedure(
                conn, procedure_id=procedure_id, caller_project_id=project_id
            )
            if not procedure:
                return _context_not_found("Procedure not found")
            return RowJSON(
                {
                    "versions": list_procedure_versions(
                        conn,
                        procedure_id=procedure_id,
                        caller_project_id=project_id,
                    )
                }
            )
    except Exception as exc:
        logger.warning("context_api: procedure_versions error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "History unavailable"}, status_code=500
        )


# ---------------------------------------------------------------------------
# Skill authoring catalog (Story 45.2)
# ---------------------------------------------------------------------------


async def _list_skill_tools(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )
    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )

    from core.db import get_connection  # noqa: PLC0415
    from core.skill_tool_catalog import list_skill_tool_catalog  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _context_not_found("Project not found")
        return RowJSON(await list_skill_tool_catalog())
    except RuntimeError as exc:
        return JSONResponse(
            {"code": "catalog_unavailable", "message": str(exc)}, status_code=503
        )
    except Exception as exc:
        logger.warning("context_api: skill_tools error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "catalog_unavailable", "message": "Skill tool catalog is unavailable"},
            status_code=503,
        )


# ---------------------------------------------------------------------------
# Graph Edge Handlers (Story 11.4)
# ---------------------------------------------------------------------------


async def _list_graph_edges(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
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
                    {"code": "not_found", "message": "Project not found"}, status_code=404
                )

            edges = list_graph_edges(conn, project_id=project_id)
            return RowJSON({"edges": edges})
    except Exception as exc:
        logger.warning("context_api: list_graph_edges error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to retrieve graph edges"},
            status_code=500,
        )


async def _create_graph_edge(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    try:
        body = await request.json()
    except Exception:
        # AN ABSENT BODY IS FINE HERE; A MALFORMED ONE IS NOT. These handlers take
        # an OPTIONAL body, so "nothing sent" legitimately becomes `{}`.
        body = {}
    if not isinstance(body, dict):
        # A JSON PRIMITIVE IS NOT AN EMPTY BODY, and coercing it to `{}` sent a
        # malformed request all the way to the database, which then answered 404
        # -- "not found" about an object the caller never got to name. Five
        # handlers did this (`_archive_topic`, `_archive_procedure`,
        # `_create_graph_edge`, `_request_review`, `_resolve_review_request`)
        # while the four that validate properly refused at 422 before opening a
        # connection. Same door, two answers to the same malformed request.
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )

    # The scope travels in the QUERY, as it does on every other context route
    # (`_required_url_project`). This handler read it from the BODY alone, so
    # `POST /graph/edges?project_id=proj_A` created a PLATFORM edge and the
    # deny-by-default platform gate answered 404 "Project not found" on a
    # project the caller owns (live finding F3). The body is still accepted as
    # a duplicate -- and must agree, like `_body_project_scope` everywhere else.
    #
    # Unlike topics and procedures, an ABSENT project_id is meaningful here: it
    # is the platform scope, guarded by check_platform_write_authorized. So this
    # route harmonizes WHERE the scope is read, not whether it is required --
    # and a body-only scope is still HONOURED rather than silently demoted to
    # platform, because turning an old caller's project edge into a platform one
    # would be a worse failure than the 404 this change removes.
    query_project_id = _required_url_project(request)
    body_project_present = "project_id" in body
    body_project_id = body.get("project_id")
    if body_project_present and body_project_id is not None and not isinstance(
        body_project_id, str
    ):
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id must be a string"},
            status_code=422,
        )
    body_project_id = (body_project_id or "").strip() or None
    if query_project_id and body_project_present and body_project_id != query_project_id:
        _audit_cross_scope(identity, "graph_edge_create", query_project_id)
        return _context_not_found()
    project_id = query_project_id or body_project_id
    from_id = (body.get("from_id") or "").strip()
    from_type = (body.get("from_type") or "").strip()
    to_id = (body.get("to_id") or "").strip()
    to_type = (body.get("to_type") or "").strip()
    edge_type = (body.get("edge_type") or "").strip()

    # STORY 49-6 AC5 -- THIS DOOR CALLS THE AUTHORITY, and no longer writes a
    # row of its own. `core.context_relationships` appends the immutable version
    # and writes `app.context_graph` as a READ PROJECTION in the same
    # transaction, so the payload this route returns is unchanged for every
    # console and every test that reads it.
    #
    # The vocabulary this door serves is the PROJECTED one -- the five endpoint
    # types `app.context_graph` can hold. A relation naming a Semantic View, a
    # metric or a Datastream lives in the authority alone and is declared
    # through its own address, never here: this route's contract is an edge row,
    # and there is no edge row to hand back for one.
    from core.context_relationships import (  # noqa: PLC0415
        PROJECTED_ENDPOINT_TYPES,
        ContextRelationshipRefused,
        create_relationship,
    )
    from core.db import get_connection  # noqa: PLC0415

    for side, node_type in (("source", from_type), ("target", to_type)):
        if node_type not in PROJECTED_ENDPOINT_TYPES:
            return JSONResponse(
                {
                    "code": "invalid_param",
                    "message": (
                        f"Invalid {side} node type: '{node_type}'. "
                        f"Allowed values: {sorted(PROJECTED_ENDPOINT_TYPES)}."
                    ),
                },
                status_code=422,
            )

    try:
        with get_connection() as conn:
            if not _check_project_role(
                conn, project_id, identity, minimum_role="member", is_write=True
            ):
                _audit_cross_scope(identity, "graph_edge_create", project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Project not found"}, status_code=404
                )

            relation = create_relationship(
                conn,
                project_id=project_id,
                source_type=from_type,
                source_id=from_id,
                target_type=to_type,
                target_id=to_id,
                relationship_kind=edge_type,
                actor=identity,
                provenance="context_graph_door",
            )
            edge = relation["projection"] or {}
            conn.commit()
            return RowJSON(edge, status_code=201)
    except ContextRelationshipRefused as refusal:
        return RowJSON({"code": refusal.code, "message": refusal.message}, status_code=422)
    except ValueError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=422)
    except Exception as exc:
        logger.warning("context_api: create_graph_edge error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to create graph edge"},
            status_code=500,
        )


async def _delete_graph_edge(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    edge_id = request.path_params.get("id", "")

    # STORY 49-6 AC5 -- A RETIREMENT IS A SUPERSEDE, NEVER A DELETE. The route
    # keeps its address, its 404s and its 204, and the row it used to destroy is
    # now kept: the authority appends a version naming who retired it and when,
    # and only the READ projection loses its row -- which is why the four legacy
    # readers of `app.context_graph` see exactly what they saw before.
    from core.context_relationships import (  # noqa: PLC0415
        ContextRelationshipRefused,
        find_by_projection_edge,
        supersede_relationship,
    )
    from core.context_store import get_graph_edge  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # Fetch unfiltered first so cross-scope attempts are audited (non-disclosing).
            edge = get_graph_edge(conn, edge_id=edge_id)
            if not edge:
                _audit_cross_scope(identity, edge_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Graph edge not found"}, status_code=404
                )

            # Enforce scope: platform-scope edge requires platform write auth;
            # project-scope edge requires Member in that project.
            if not _check_project_role(
                conn, edge["project_id"], identity, minimum_role="member", is_write=True
            ):
                _audit_cross_scope(identity, edge_id, edge["project_id"])
                return JSONResponse(
                    {"code": "not_found", "message": "Graph edge not found"}, status_code=404
                )

            relation = find_by_projection_edge(conn, edge_id=edge_id)
            if relation is None:
                # A PROJECTION WITH NO AUTHORITY IS NOT A THING THIS DOOR MAY
                # DESTROY. Migration 317 carries every incumbent edge, and the
                # projection is written by the authority alone -- so an edge with
                # no relation behind it means a second writer exists, and
                # answering 204 would hide it. 410 says the object is no longer
                # served through this door and names where it is held.
                logger.warning(
                    "context_api: graph edge with no relationship behind it "
                    "project=%s", edge["project_id"],
                )
                return JSONResponse(
                    {
                        "code": "relation_not_governed",
                        "message": (
                            "Re-declare this relation from the Knowledge Graph: "
                            "it is not held by the relationship authority and "
                            "cannot be retired from here."
                        ),
                    },
                    status_code=410,
                )

            supersede_relationship(
                conn,
                project_id=relation["project_id"],
                relationship_id=relation["id"],
                actor=identity,
                reason="retired through the Knowledge Graph",
            )
            conn.commit()
            return Response(status_code=204)
    except ContextRelationshipRefused as refusal:
        return RowJSON({"code": refusal.code, "message": refusal.message}, status_code=409)
    except Exception as exc:
        logger.warning("context_api: delete_graph_edge error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to delete graph edge"},
            status_code=500,
        )


async def _list_node_relationships(request: Request) -> Response:
    """GET /api/context/relationships -- the "Used by / Related" facet (AC6).

    ONE ADDRESS FOR BOTH SIDES OF THE PRODUCT. A Governance object (a Master
    Data node, a governed field) and a Data object (a Datastream, a Semantic
    View) ask the same question -- *what knowledge, what Skill, what governed
    field names this?* -- and story 49-6 asks that both be answered by the same
    bounded server projection over the same relationship fact, with exact owner
    links and no browser fan-out.

    A FAILED READ IS NOT AN EMPTY LIST. The service answers ``state:
    'unavailable'`` with a sentence, and this route hands it back at 200 rather
    than at 500: the panel renders the sentence, and the rest of the owner's
    screen keeps working. The *Incomplete if* it closes is *"a failed
    reverse-link read renders as 'no relations'"*.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    node_type = (request.query_params.get("node_type") or "").strip()
    node_id = (request.query_params.get("node_id") or "").strip()
    if not node_type or not node_id:
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "Name the object whose relations you want: node_type and node_id.",
            },
            status_code=422,
        )
    try:
        limit = int(request.query_params.get("limit") or 0)
    except ValueError:
        return JSONResponse(
            {"code": "invalid_param", "message": "limit must be a whole number"},
            status_code=422,
        )

    from core.context_relationships import (  # noqa: PLC0415
        DEFAULT_FACET_LIMIT,
        ContextRelationshipRefused,
        list_for_node,
    )
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if project_id and not _check_project_role(
                conn, project_id, identity, minimum_role="viewer"
            ):
                _audit_cross_scope(identity, "context_relationships_list", project_id)
                return _context_not_found()

            facet = list_for_node(
                conn,
                project_id=project_id,
                node_type=node_type,
                node_id=node_id,
                limit=limit or DEFAULT_FACET_LIMIT,
                include_superseded=(
                    (request.query_params.get("include_superseded") or "").strip() == "true"
                ),
            )
            return RowJSON(facet)
    except ContextRelationshipRefused as refusal:
        return RowJSON({"code": refusal.code, "message": refusal.message}, status_code=422)
    except Exception as exc:
        logger.warning("context_api: list_node_relationships error: %s", exc)
        # THE READ FAILED, AND THE PANEL MUST SAY SO RATHER THAN SHOW NOTHING.
        return RowJSON(
            {
                "state": "unavailable",
                "node": {"type": node_type, "id": node_id},
                "reason": "related_items_unreadable",
                "message": (
                    "Open this panel again in a moment: the related items could "
                    "not be read."
                ),
            }
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


def _project_org_id(conn: Any, project_id: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.projects WHERE id = %s AND status = 'active'",
            (project_id,),
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        raise ValueError("project organization is unavailable")
    return str(row[0])


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
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
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
                    {"code": "not_found", "message": "Project not found"}, status_code=404
                )

            topics = list_topics(conn, project_id=project_id, status="active")
            procedures = list_procedures(conn, project_id=project_id, status="active")
            schema_docs = list_schema_docs(conn, project_id=project_id)
            edges = list_graph_edges(conn, project_id=project_id)

            # Story 45.1: taxonomy links are authoritative in mdm_business_links.
            # The Context Graph projects them but never duplicates them into
            # app.context_graph as a second writable relationship store.
            try:
                from core.business_taxonomy import graph_projection  # noqa: PLC0415

                business_projection = graph_projection(
                    conn, org_id=_project_org_id(conn, project_id), project_id=project_id
                )
            except Exception as projection_exc:  # noqa: BLE001
                logger.debug(
                    "context_api: business projection unavailable project=%s: %s",
                    project_id,
                    type(projection_exc).__name__,
                )
                business_projection = {
                    "nodes": [], "edges": [], "referenced_target_fields": []
                }

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
            referenced_fields: set[str] = set(
                business_projection["referenced_target_fields"]
            )
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

            nodes.extend(business_projection["nodes"])
            edges.extend(business_projection["edges"])

            # A target can already be present from the governed context tables.
            # Keep the canonical rendering and add only genuinely new projections.
            deduped_nodes: list[dict[str, Any]] = []
            seen_node_keys: set[tuple[str, str]] = set()
            for node in nodes:
                key = (node["id"], node["node_type"])
                if key not in seen_node_keys:
                    deduped_nodes.append(node)
                    seen_node_keys.add(key)
            nodes = deduped_nodes

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

            # RowJSON, pas JSONResponse : les noeuds composent des lignes venues
            # de quatre tables, dont certaines rendent des `datetime` bruts. La
            # route entiere rendait 500 -- donc le graphe paraissait VIDE alors
            # que la base portait 3 domaines, 9 Skills, 36 Knowledge, 45 liens.
            return RowJSON({"nodes": nodes, "edges": edges})
    except Exception as exc:
        logger.warning("context_api: get_graph error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to retrieve context graph"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Request Review Handler (Story 44.11)
# ---------------------------------------------------------------------------


async def _request_review(request: Request) -> Response:
    """POST /api/context/nodes/{id}/request-review?project_id=<id> -- DEPOSER en file.

    ⚠️ CE HANDLER N'ECRIVAIT QU'UNE LIGNE D'AUDIT (AI-157, 2026-08-04). Il
    acceptait `{node_type, note}`, ecrivait `context.review_requested` et rendait
    `201` -- et `review_requested` n'apparaissait NULLE PART ailleurs. Un journal
    d'audit est append-only et scelle : ce n'est pas une file de travail, et une
    remarque qui y tombe n'en ressort pas. `core/context_review.py` portait la
    file depuis le 2026-08-04, testee et prouvee en base, et aucune route ne
    l'appelait.

    Ce que le branchement ajoute, et pourquoi chaque piece est exigee :

      * `project_id` DANS L'URL, comme toute la famille des routes de contexte.
        C'est la seule facon d'obtenir l'`org_id` que la table exige -- et un
        noeud de PLATEFORME (`project_id IS NULL`) n'en porte aucun.
      * la VERSION du noeud, lue dans la meme transaction. « Cette contrainte
        n'existe plus » ne veut rien dire si l'on ignore de quelle version on
        parle ; `get_topic` / `get_procedure` la rendent deja.
      * une NOTE non vide. La table le demande (`length(btrim(note)) >= 1`), et
        c'est juste : une demande de relecture qui ne dit rien n'est pas une
        remarque, c'est un clic. Le handler la refusait implicitement en
        l'ecrivant `null` dans l'audit -- desormais il le dit, en 422.

    L'audit RESTE ecrit : il n'est pas remplace, il est complete. Le journal dit
    qu'on a demande, la file dit ce qui reste a trancher -- deux faits distincts,
    et le second manquait.

    Corps : {"node_type": "topic" | "procedure", "note": string,
             "origin"?: "human" | "agent", "proposed_change"?: {...}}.
    `node_type` est explicite plutot que deduit du prefixe de l'id, pour qu'une
    forme inconnue ne resolve jamais en silence vers la mauvaise lecture.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    node_id = request.path_params.get("id", "")
    try:
        body = await request.json()
    except Exception:
        # AN ABSENT BODY IS FINE HERE; A MALFORMED ONE IS NOT. These handlers take
        # an OPTIONAL body, so "nothing sent" legitimately becomes `{}`.
        body = {}
    if not isinstance(body, dict):
        # A JSON PRIMITIVE IS NOT AN EMPTY BODY, and coercing it to `{}` sent a
        # malformed request all the way to the database, which then answered 404
        # -- "not found" about an object the caller never got to name. Five
        # handlers did this (`_archive_topic`, `_archive_procedure`,
        # `_create_graph_edge`, `_request_review`, `_resolve_review_request`)
        # while the four that validate properly refused at 422 before opening a
        # connection. Same door, two answers to the same malformed request.
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )

    node_type = (body.get("node_type") or "").strip()
    note_raw = body.get("note")
    note = note_raw.strip() if isinstance(note_raw, str) else ""

    if node_type not in ("topic", "procedure"):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "node_type must be 'topic' or 'procedure'.",
            },
            status_code=422,
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )

    if not note:
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "note is required: a review request that says nothing "
                           "cannot be reviewed.",
            },
            status_code=422,
        )

    origin = (body.get("origin") or "human").strip()
    proposed_change = body.get("proposed_change")
    if proposed_change is not None and not isinstance(proposed_change, dict):
        return JSONResponse(
            {"code": "invalid_param", "message": "proposed_change must be an object"},
            status_code=422,
        )

    from core import context_review  # noqa: PLC0415
    from core.context_store import get_procedure, get_topic  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # Viewer suffit : n'importe quel consommateur de la connaissance peut
            # signaler un noeud, pas seulement ceux qui l'ecrivent.
            if not _strict_url_project_allowed(
                conn, identity=identity, project_id=project_id, minimum_capability="view"
            ):
                _audit_cross_scope(identity, node_id, project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Node not found"}, status_code=404
                )

            node = (
                get_topic(conn, topic_id=node_id, caller_project_id=project_id)
                if node_type == "topic"
                else get_procedure(
                    conn, procedure_id=node_id, caller_project_id=project_id
                )
            )
            if not node:
                _audit_cross_scope(identity, node_id, project_id)
                return JSONResponse(
                    {"code": "not_found", "message": "Node not found"}, status_code=404
                )

            try:
                queued = context_review.request_review(
                    conn,
                    org_id=_project_org_id(conn, project_id),
                    # La remarque garde la portee du NOEUD, pas celle de l'appelant :
                    # une remarque sur une Skill de plateforme concerne toute
                    # l'organisation, et `list_open` lit un `project_id` NULL
                    # exactement ainsi.
                    project_id=node["project_id"],
                    node_type=node_type,
                    node_id=node_id,
                    node_version=int(node.get("version_number") or 1),
                    note=note,
                    requested_by=identity,
                    origin=origin,
                    proposed_change=proposed_change,
                )
            except context_review.ReviewRequestError as exc:
                return JSONResponse(
                    {"code": "invalid_param", "message": str(exc)}, status_code=422
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
                    # La ligne d'audit POINTE desormais sur la file. Sans cet id,
                    # les deux traces du meme fait ne se rejoignent pas.
                    "request_id": queued.get("id"),
                    "node_version": queued.get("node_version"),
                },
            )
            conn.commit()
            # `{"status": "requested"}` seul ne rendait rien qu'on puisse adresser
            # ensuite -- une file dont on ne peut pas nommer l'entree est une file
            # qu'on ne peut pas clore.
            return RowJSON(
                {"status": "requested", "request": queued}, status_code=201
            )
    except ValueError:
        # Meme cas que _list_review_requests : projet inconnu sous seam
        # court-circuitee (mode auth desactivee) -> _project_org_id leve.
        return JSONResponse(
            {"code": "not_found", "message": "Node not found"}, status_code=404
        )
    except Exception as exc:
        logger.warning("context_api: request_review error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to request review"},
            status_code=500,
        )


async def _list_review_requests(request: Request) -> Response:
    """GET /api/context/review-requests?project_id=<id>[&node_id=<id>] -- la file.

    Ce qui reste a trancher, le plus recent d'abord. Une remarque de PLATEFORME
    (`project_id` NULL) remonte pour tout projet de l'organisation : c'est ce que
    `context_review.list_open` lit, et c'est ce qu'on veut -- une Skill de
    plateforme concerne tout le monde.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    node_id = (request.query_params.get("node_id") or "").strip() or None

    from core import context_review  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn, identity=identity, project_id=project_id, minimum_capability="view"
            ):
                return _context_not_found("Project not found")
            can_resolve = _strict_url_project_allowed(
                conn, identity=identity, project_id=project_id, minimum_capability="edit"
            )
            requests = context_review.list_open(
                conn,
                org_id=_project_org_id(conn, project_id),
                project_id=project_id,
                node_id=node_id,
            )
            return RowJSON({"requests": requests, "can_resolve": can_resolve})
    except ValueError:
        # La seam d'acces court-circuite en mode auth desactivee sans verifier
        # l'existence du projet ; _project_org_id leve alors ValueError. Un
        # projet inconnu est un 404, pas une erreur serveur (constat live A2).
        return _context_not_found("Project not found")
    except Exception as exc:
        logger.warning("context_api: list_review_requests error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to retrieve review requests"},
            status_code=500,
        )


async def _list_recurrent_fates(request: Request) -> Response:
    """GET /api/context/recurrent-fates?project_id=<id>[&minimum=<n>] -- le repli.

    CE QUE CETTE ROUTE REND LISIBLE, ET POURQUOI ELLE MANQUAIT. `record_fates`
    garde depuis le 2026-08-04 le sort de chaque candidat atteint puis ecarte, et
    `recurrent_fates` sait dire lesquels reviennent assez souvent pour meriter un
    reclassement. Mesure du 2026-08-05 : `recurrent_fates` n'avait AUCUN
    appelant. Le signal etait donc calcule, agrege, conserve -- et invisible.

    C'est la moitie qui manquait au repli : un candidat rejete a repetition pour
    des questions d'un metier auquel il n'est pas lie est un lien manquant, et
    ce lien se comble par le rail existant (une remarque avec sa charge, puis un
    humain qui tranche, puis un lien `derived`). Sans cette lecture, la file de
    reclassement ne pouvait etre alimentee que par une relecture humaine -- donc
    jamais par l'usage.

    Le SEUIL voyage en parametre, avec sa valeur par defaut rendue dans la
    reponse : combien de fois vaut << souvent >> est une decision de produit, et
    une constante cachee la prendrait en silence.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )

    raw_minimum = (request.query_params.get("minimum") or "").strip()
    minimum = 3
    if raw_minimum:
        try:
            minimum = int(raw_minimum)
        except ValueError:
            return JSONResponse(
                {"code": "invalid_param", "message": "minimum must be an integer"},
                status_code=422,
            )
        if minimum < 1:
            return JSONResponse(
                {"code": "invalid_param", "message": "minimum must be at least 1"},
                status_code=422,
            )

    from core.context_search import recurrent_fates  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn, identity=identity, project_id=project_id, minimum_capability="view"
            ):
                return _context_not_found("Project not found")
            rows = recurrent_fates(conn, project_id=project_id, minimum=minimum)
            return RowJSON({"fates": rows, "minimum": minimum})
    except Exception as exc:
        logger.warning("context_api: list_recurrent_fates error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to retrieve recurrent fates"},
            status_code=500,
        )


async def _resolve_review_request(request: Request) -> Response:
    """POST /api/context/review-requests/{id}/resolve?project_id=<id> -- clore.

    `accepted` ou `declined`, jamais un silence. Une remarque qui portait une
    CHARGE (`proposed_change`) la voit appliquee ICI, et seulement sur
    `accepted` -- c'est le point ou un HUMAIN tranche, et c'est pour cela que le
    lien qui en nait est marque `derived` : distinguable pour toujours d'une
    decision humaine directe.

    Il faut `edit` : deposer une remarque est un droit de lecteur, la trancher
    est un acte d'ecriture -- et une acceptation peut poser un lien en base.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )

    project_id = _required_url_project(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id is required"}, status_code=422
        )
    request_id = request.path_params.get("id", "")
    try:
        body = await request.json()
    except Exception:
        # AN ABSENT BODY IS FINE HERE; A MALFORMED ONE IS NOT. These handlers take
        # an OPTIONAL body, so "nothing sent" legitimately becomes `{}`.
        body = {}
    if not isinstance(body, dict):
        # A JSON PRIMITIVE IS NOT AN EMPTY BODY, and coercing it to `{}` sent a
        # malformed request all the way to the database, which then answered 404
        # -- "not found" about an object the caller never got to name. Five
        # handlers did this (`_archive_topic`, `_archive_procedure`,
        # `_create_graph_edge`, `_request_review`, `_resolve_review_request`)
        # while the four that validate properly refused at 422 before opening a
        # connection. Same door, two answers to the same malformed request.
        return JSONResponse(
            {"code": "invalid_body", "message": "JSON object body required"}, status_code=422
        )
    status = (body.get("status") or "").strip()

    from core import context_review  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _strict_url_project_allowed(
                conn, identity=identity, project_id=project_id, minimum_capability="edit"
            ):
                return _context_not_found("Review request not found")
            try:
                resolved = context_review.resolve_request(
                    conn,
                    request_id=request_id,
                    org_id=_project_org_id(conn, project_id),
                    status=status,
                    resolved_by=identity,
                )
            except context_review.ReviewRequestAlreadyResolvedError as exc:
                return JSONResponse(
                    {"code": "already_resolved", "message": str(exc)}, status_code=409
                )
            except context_review.ReviewRequestError as exc:
                return JSONResponse(
                    {"code": "invalid_param", "message": str(exc)}, status_code=422
                )
            if resolved is None:
                # Deja close, inconnue, ou d'une autre organisation -- meme
                # reponse : une file ne dit pas ce qu'elle contient a qui n'y a
                # pas droit.
                return _context_not_found("Review request not found")
            conn.commit()
            return RowJSON(resolved)
    except ValueError:
        # Projet inconnu sous seam court-circuitee (mode auth desactivee) :
        # _project_org_id leve ValueError. Non-divulgateur, comme la file.
        return _context_not_found("Review request not found")
    except Exception as exc:
        logger.warning("context_api: resolve_review_request error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Failed to resolve review request"},
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
    Route(
        "/api/context/topics/{id}/versions",
        endpoint=_list_topic_versions,
        methods=["GET"],
    ),
    Route("/api/context/topics/{id}", endpoint=_get_topic, methods=["GET"]),
    Route("/api/context/topics/{id}", endpoint=_update_topic, methods=["PATCH"]),
    Route("/api/context/topics/{id}/archive", endpoint=_archive_topic, methods=["POST"]),
    Route("/api/context/topics/{id}/restore", endpoint=_restore_topic, methods=["POST"]),
    Route("/api/context/procedures", endpoint=_create_procedure, methods=["POST"]),
    Route("/api/context/procedures", endpoint=_list_procedures, methods=["GET"]),
    Route("/api/context/skill-tools", endpoint=_list_skill_tools, methods=["GET"]),
    Route(
        "/api/context/procedures/{id}/versions",
        endpoint=_list_procedure_versions,
        methods=["GET"],
    ),
    Route("/api/context/procedures/{id}", endpoint=_get_procedure_by_id, methods=["GET"]),
    Route("/api/context/procedures/{id}", endpoint=_update_procedure, methods=["PATCH"]),
    Route("/api/context/procedures/{id}/archive", endpoint=_archive_procedure, methods=["POST"]),
    Route(
        "/api/context/procedures/{id}/restore", endpoint=_restore_procedure, methods=["POST"]
    ),
    # Story 11.4 — graph edge routes (AD-5 scoping, deny-by-default platform writes)
    Route("/api/context/graph/edges", endpoint=_list_graph_edges, methods=["GET"]),
    Route("/api/context/graph/edges", endpoint=_create_graph_edge, methods=["POST"]),
    Route("/api/context/graph/edges/{id}", endpoint=_delete_graph_edge, methods=["DELETE"]),
    # Story 49-6 AC6 — the reverse-link facet, one address for a Governance
    # object and for a Data object. Read only: the WRITE doors are the two above.
    Route(
        "/api/context/relationships", endpoint=_list_node_relationships, methods=["GET"]
    ),
    # Story 44.3 — one-bundle graph read (nodes + edges)
    Route("/api/context/graph", endpoint=_get_graph, methods=["GET"]),
    # Story 44.11 / AI-157 — request review: deposer en file ET journaliser.
    # La route existait depuis 44.11 et n'ecrivait qu'un audit ; les deux routes
    # qui suivent sont ce qui manquait pour que la file se LISE et se CLOSE.
    Route(
        "/api/context/nodes/{id}/request-review", endpoint=_request_review, methods=["POST"]
    ),
    Route(
        "/api/context/review-requests", endpoint=_list_review_requests, methods=["GET"]
    ),
    Route(
        "/api/context/review-requests/{id}/resolve",
        endpoint=_resolve_review_request,
        methods=["POST"],
    ),
    # Le repli : ce que la recherche a ecarte assez souvent pour que ce soit un
    # lien manquant, et non un accident de formulation.
    Route(
        "/api/context/recurrent-fates", endpoint=_list_recurrent_fates, methods=["GET"]
    ),
]
