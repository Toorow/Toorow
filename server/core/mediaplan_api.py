"""toorow -- Media plan REST route handlers (Story 22.1, FR38 / CAP-26).

Exports MEDIAPLAN_ROUTES: list[Route] -- spliced into admin_api.router at startup.

AD-5 scoping (pattern: core.context_api): writes require Member+ of the project,
reads require Viewer+; cross-project denials are audited and return 404 (existence
is never disclosed). Business errors from core.mediaplan_store map to 4xx; 5xx
bodies are opaque (never str(exc) -- lesson 12.3).

Routes:
  POST   /api/projects/{project_id}/mediaplans            -> create plan
  GET    /api/projects/{project_id}/mediaplans            -> list plans
  GET    /api/mediaplans/{plan_id}                        -> plan detail (active version + lines)
  POST   /api/mediaplans/{plan_id}/versions               -> create candidate version (lines)
  GET    /api/mediaplans/{plan_id}/versions               -> list versions
  POST   /api/mediaplans/versions/{version_id}/publish    -> publish a candidate version
  GET    /api/mediaplans/{plan_id}/diff?from=..&to=..     -> diff two versions
  PUT    /api/mediaplans/{plan_id}/lines/{line_key}/mappings -> set a line's mappings (22.3)
  GET    /api/mediaplans/{plan_id}/mappings               -> list mappings by line (22.3)
  GET    /api/mediaplans/{plan_id}/unmapped-actuals       -> unmapped perimeter spend (22.3)
  GET    /api/mediaplans/{plan_id}/pacing                  -> plan-vs-actual pacing (22.4)

RETIRED 2026-08-24 -- the three Story 22.2 import addresses are gone with the
second xlsx engine they were the only door to:

  PUT    /api/mediaplans/{plan_id}/import-contracts/{sheet_name}
  GET    /api/mediaplans/{plan_id}/import-contracts
  POST   /api/mediaplans/{plan_id}/import

`file-source-ingestion.md` ratified ONE ingestion engine on 2026-08-17 and
`mediaplan_import` was the second one. A media plan now reaches
`app.media_plan_lines` through `import_runner.run_import()` with a Template
declaring `landing_target: "plan_store"`, on the plan's own carrier Datastream
(amendment of 2026-08-24). The retirement, its measurement and the two
capabilities the one engine does not yet express are written in
`docs/product-architecture/file-source-ingestion.md`, amendment « the second
xlsx engine is retired »; the addresses are held shut by
`server/tests/conformance/test_retired_mediaplan_import.py`.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import ACTION_CROSS_SCOPE_ATTEMPT, write_audit_row

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. `write_audit_row` refuse une action que personne n'a
# declaree.
#
# `media_plan.import_contract.set` et `media_plan.imported` ne sont plus
# declarees ici : leurs deux ecrivains sont partis avec le second moteur xlsx le
# 2026-08-24. Une action declaree que rien n'ecrit est une porte qui parait
# ouverte. Les lignes d'audit deja ecrites sous ces deux noms restent lisibles --
# la declaration ne gouverne que l'ECRITURE.


logger = logging.getLogger(__name__)


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _check_project_role(
    conn: Any, project_id: str, identity: str, minimum_role: str
) -> bool:
    """Return whether ``identity`` meets ``minimum_role`` on ``project_id``."""
    from core.project_access import identity_has_project_role  # noqa: PLC0415

    try:
        return identity_has_project_role(project_id, identity, minimum_role, conn)
    except Exception:
        return False


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


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
    )


def _not_found(message: str = "Ressource introuvable") -> JSONResponse:
    return JSONResponse({"code": "not_found", "message": message}, status_code=404)


def _is_uuid(value: str) -> bool:
    """Guard: malformed ids must 404 like unknown ones (never a DB-cast 500)."""
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _datastream_belongs_to_project(conn: Any, datastream_id: str, project_id: str) -> bool:
    """Is this Datastream a live one of this project? (never disclose otherwise)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM app.datastreams "
            "WHERE id = %s AND project_id = %s AND archived_at IS NULL",
            (datastream_id, project_id),
        )
        return cur.fetchone() is not None


def _plan_project_id(conn: Any, plan_id: str) -> str | None:
    """Resolve a plan's project_id (None if the plan does not exist)."""
    with conn.cursor() as cur:
        cur.execute("SELECT project_id FROM app.media_plans WHERE id = %s", (plan_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _version_plan(conn: Any, version_id: str) -> tuple[str, str] | None:
    """Resolve (plan_id, project_id) for a version id (None if missing)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.plan_id, p.project_id
            FROM app.media_plan_versions v
            JOIN app.media_plans p ON p.id = v.plan_id
            WHERE v.id = %s
            """,
            (version_id,),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


def _store_error_response(exc: Exception) -> JSONResponse | None:
    """Map a typed store error to a 4xx JSONResponse, or None if not one."""
    from core.mediaplan_store import (  # noqa: PLC0415
        MediaPlanNotFoundError,
        MediaPlanStateError,
        MediaPlanValidationError,
    )

    if isinstance(exc, MediaPlanNotFoundError):
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=404)
    if isinstance(exc, MediaPlanStateError):
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=409)
    if isinstance(exc, MediaPlanValidationError):
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=422)
    return None


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


async def _create_plan(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    project_id = request.path_params.get("project_id", "")
    try:
        body = await request.json()
    except Exception:
        body = {}

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import create_plan  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _check_project_role(conn, project_id, identity, "member"):
                _audit_cross_scope(identity, "mediaplan_create", project_id)
                return _not_found("Project not found")

            # WHICH DATASTREAM CARRIES IT (ratified 2026-08-24). The console
            # always sends one -- creation is a gesture of the carrier's
            # Workbench and of no other screen -- and the composite foreign key
            # `(carrier_datastream_id, project_id)` keeps a carrier of another
            # project out. It is checked HERE all the same, so the answer is a
            # 404 that discloses nothing rather than a constraint violation
            # surfacing as an opaque 500.
            carrier = str(body.get("carrier_datastream_id") or "").strip()
            if carrier and not _datastream_belongs_to_project(conn, carrier, project_id):
                _audit_cross_scope(identity, carrier, project_id)
                return _not_found("Datastream not found")

            plan = create_plan(
                conn,
                project_id=project_id,
                name=body.get("name", ""),
                currency=body.get("currency", "EUR"),
                created_by=identity,
                carrier_datastream_id=carrier or None,
            )
            conn.commit()
            return JSONResponse(plan, status_code=201)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: create_plan error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Error while creating the plan"},
            status_code=500,
        )


async def _list_plans(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    project_id = request.path_params.get("project_id", "")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import (  # noqa: PLC0415
        list_carrier_datastreams,
        list_plans,
    )

    try:
        with get_connection() as conn:
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, "mediaplans_list", project_id)
                return _not_found("Project not found")

            plans = list_plans(conn, project_id=project_id)
            # THE ADDRESS OF THE GESTURE, BESIDE THE COLLECTION IT FILLS
            # (ratified 2026-08-24, `analyze-and-test.md`). A reader of this list
            # with nothing in it needs the Workbench that creates the first plan,
            # and until now no response said where that was -- which is why the
            # Pacing lens could only name a gesture with no address.
            #
            # SENT ON EVERY READ AND NOT ONLY ON THE EMPTY ONE: a project that
            # already has one plan and a second empty carrier is exactly the case
            # the decision opens ("one or several plans, each on its own
            # carrier"), and a payload that hid the carriers as soon as one plan
            # existed would close it again.
            carriers = list_carrier_datastreams(conn, project_id=project_id)
            return JSONResponse({"plans": plans, "carriers": carriers})
    except Exception as exc:
        logger.warning("mediaplan_api: list_plans error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Error while fetching the plans"},
            status_code=500,
        )


async def _get_plan(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import get_plan  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            plan = get_plan(conn, plan_id=plan_id)
            if plan is None:
                return _not_found("Plan introuvable")
            return JSONResponse(plan)
    except Exception as exc:
        logger.warning("mediaplan_api: get_plan error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Error while fetching the plan"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


#: Body keys that would be asking this door to publish. A version arrives as a
#: CANDIDATE and by no other status, so these are refused rather than ignored:
#: silently dropping `"publish": true` returns a 201 to a caller who believes the
#: plan is live, which is the same silent direct write in a politer costume.
_PUBLICATION_INTENT_KEYS = ("publish", "published", "publish_candidate", "status", "is_active")


async def _create_version(request: Request) -> Response:
    """POST a new CANDIDATE version of a plan (Member+).

    THE GOVERNED TWO-STEP, THROUGH THIS DOOR TOO (AI-331, Jean 2026-08-31).
    The one-engine clause of `file-source-ingestion.md` is scoped to FILES: a
    mediaplan FILE reaches `app.media_plan_lines` only through `run_import()`.
    This door is not a file import -- it is the plan's own editing gesture -- and
    what Jean ratified is that it takes the SAME governed two-step the plan
    object already owns: a candidate, then an explicit publish, never a silent
    direct write.

    Concretely: every rule about the shape and the money of a version lives in
    `mediaplan_store.create_version_with_lines`, the ONE seam the file door goes
    through as well (`import_landing.land_plan_store_rows`), so this handler adds
    no validation of its own -- it would be a second rule. What it owns is the
    wire contract, and the wire contract is where publication intent has to be
    refused, because a JSON body can carry it and a landed row cannot.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")
    try:
        body = await request.json()
    except Exception:
        body = {}

    if isinstance(body, dict):
        asked = [key for key in _PUBLICATION_INTENT_KEYS if key in body]
        if asked:
            return JSONResponse(
                {
                    "code": "publication_is_a_separate_act",
                    "message": (
                        "A version is created as a candidate. Publish it with "
                        "POST /api/mediaplans/versions/{version_id}/publish."
                    ),
                    "refused_keys": asked,
                },
                status_code=422,
            )

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import create_version_with_lines  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "member"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            version = create_version_with_lines(
                conn,
                plan_id=plan_id,
                lines=body.get("lines", []),
                source_note=body.get("source_note"),
                created_by=identity,
            )
            conn.commit()
            return JSONResponse(version, status_code=201)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: create_version error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Error while creating the version"},
            status_code=500,
        )


async def _list_versions(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import list_versions  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            versions = list_versions(conn, plan_id=plan_id)
            return JSONResponse({"versions": versions})
    except Exception as exc:
        logger.warning("mediaplan_api: list_versions error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Error while fetching the versions"},
            status_code=500,
        )


async def _publish_version(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    version_id = request.path_params.get("version_id", "")
    if not _is_uuid(version_id):
        return _not_found("Version introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import publish_version  # noqa: PLC0415

    try:
        with get_connection() as conn:
            resolved = _version_plan(conn, version_id)
            if resolved is None:
                return _not_found("Version introuvable")
            _plan_id, project_id = resolved
            if not _check_project_role(conn, project_id, identity, "member"):
                _audit_cross_scope(identity, version_id, project_id)
                return _not_found("Version introuvable")

            version = publish_version(conn, version_id=version_id, published_by=identity)
            conn.commit()
            return JSONResponse(version)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: publish_version error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la publication de la version"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


async def _diff_versions(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    version_from = (request.query_params.get("from") or "").strip()
    version_to = (request.query_params.get("to") or "").strip()

    if not version_from or not version_to:
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "The 'from' and 'to' parameters are required.",
            },
            status_code=422,
        )
    if not _is_uuid(plan_id) or not _is_uuid(version_from) or not _is_uuid(version_to):
        return _not_found("Plan ou version introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import diff_versions  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            # Both versions must belong to this plan (no cross-plan diff).
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM app.media_plan_versions
                    WHERE plan_id = %s AND id IN (%s, %s)
                    """,
                    (plan_id, version_from, version_to),
                )
                count = int(cur.fetchone()[0])
            if count != len({version_from, version_to}):
                return _not_found("Version introuvable pour ce plan")

            diff = diff_versions(conn, version_a=version_from, version_b=version_to)
            return JSONResponse(diff)
    except Exception as exc:
        logger.warning("mediaplan_api: diff_versions error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors du calcul du diff"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Mappings (Story 22.3, FR38 / CAP-26)
# ---------------------------------------------------------------------------


async def _set_line_mappings(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    line_key = request.path_params.get("line_key", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")
    if not line_key:
        return _not_found("Ligne introuvable")
    try:
        body = await request.json()
    except Exception:
        body = {}

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_mapping import set_line_mappings  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "member"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            result = set_line_mappings(
                conn,
                plan_id=plan_id,
                line_key=line_key,
                entries=body.get("mappings", []),
                actor=identity,
            )
            conn.commit()
            return JSONResponse(result)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: set_line_mappings error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de l'enregistrement des mappings"},
            status_code=500,
        )


async def _list_mappings(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_mapping import list_mappings  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            return JSONResponse(list_mappings(conn, plan_id=plan_id))
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: list_mappings error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Error while fetching the mappings"},
            status_code=500,
        )


async def _list_unmapped_actuals(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_mapping import list_unmapped_actuals  # noqa: PLC0415
    from core.warehouse import query_campaign_spend  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            result = list_unmapped_actuals(
                conn, plan_id=plan_id, campaign_spend_fn=query_campaign_spend
            )
            return JSONResponse(result)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        # WarehouseUnavailable (and any other read failure): opaque 5xx, never a
        # fake empty perimeter (AD-9 / story rule "JAMAIS filtré silencieusement").
        logger.warning("mediaplan_api: list_unmapped_actuals error: %s", exc)
        return JSONResponse(
            {
                "code": "warehouse_unavailable",
                "message": "Actual spend unavailable for now",
            },
            status_code=503,
        )


# ---------------------------------------------------------------------------
# Pacing (Story 22.4, FR38 / CAP-26) -- read-only plan-vs-actual.
# ---------------------------------------------------------------------------


async def _get_pacing(request: Request) -> Response:
    """GET the plan-vs-actual pacing for a plan (Viewer+).

    Returns the by-line, by-channel and by-plan pacing (each row already carrying its
    provenance: plan_version_id + actual_pull_id_min/max/count). Every ratio is
    NULL-honest (pace NULL when allocated-to-date=0; actual NULL for plan-only lines) --
    the mart never fabricates a 0 (AD-9). A 503 (opaque) is returned when the warehouse
    is unavailable -- never a fake empty pacing (story rule "JAMAIS filtré
    silencieusement").
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.warehouse import query_plan_vs_actual  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

        # The pacing read is a pure warehouse read (no Postgres write) -- run it
        # OUTSIDE the Postgres connection scope. project_id is resolved + authorised
        # above (AD-5); the mart is also project-scoped in SQL.
        result = query_plan_vs_actual(project_id=project_id, plan_id=plan_id)
        return JSONResponse(
            {
                "plan_id": plan_id,
                "project_id": project_id,
                "lines": result.get("lines", []),
                "channels": result.get("channels", []),
                # Epic review E3-F-1: the mart returns 0..1 plan-level rows as a
                # list; the route contract is ONE object or null (the console
                # types PacingPlan | null -- a leaked list rendered silently
                # empty). Unwrap here: the route owns its contract.
                "plan": (result.get("plan") or [None])[0],
            }
        )
    except Exception as exc:
        # WarehouseUnavailable (and any other read failure): opaque 503, never a fake
        # empty pacing (AD-9 / story rule).
        logger.warning("mediaplan_api: get_pacing error: %s", exc)
        return JSONResponse(
            {
                "code": "warehouse_unavailable",
                "message": "Pacing indisponible pour le moment",
            },
            status_code=503,
        )


# ---------------------------------------------------------------------------
# RETIRED 2026-08-24 -- the Story 22.2 import surface lived here: three
# handlers, `_set_import_contract`, `_list_import_contracts` and
# `_import_workbook`. They were the only door onto `core/mediaplan_import.py`,
# the SECOND xlsx engine, and they went with it.
#
# `file-source-ingestion.md` ratified ONE ingestion engine on 2026-08-17. A plan
# now lands through `import_runner.run_import()` against a Template declaring
# `landing_target: "plan_store"`, on the plan's own carrier Datastream. The
# measurement that authorised the removal -- no console caller, no server caller
# but the routes, and zero plans ever imported in production -- and the two
# capabilities the one engine does NOT yet express are written in the amendment
# « the second xlsx engine is retired ».
#
# The trace is kept HERE, beside the route table, because that is where the next
# reader looks before re-adding an address. The addresses are held shut by
# `server/tests/conformance/test_retired_mediaplan_import.py`.
# ---------------------------------------------------------------------------


# NOTE: static path segments precede parametrised ones so Starlette matches the
# static route first (e.g. /mediaplans/versions/{id}/publish before nothing that
# would shadow it). The plan-scoped and version-scoped roots are disjoint.
# The {line_key:path} converter allows a line_key that contains '/' (e.g.
# 'digital/meta'); the static '/mappings' suffix keeps the PUT route unambiguous.
MEDIAPLAN_ROUTES: list[Route] = [
    Route(
        "/api/projects/{project_id}/mediaplans", endpoint=_create_plan, methods=["POST"]
    ),
    Route("/api/projects/{project_id}/mediaplans", endpoint=_list_plans, methods=["GET"]),
    Route(
        "/api/mediaplans/versions/{version_id}/publish",
        endpoint=_publish_version,
        methods=["POST"],
    ),
    Route("/api/mediaplans/{plan_id}/versions", endpoint=_create_version, methods=["POST"]),
    Route("/api/mediaplans/{plan_id}/versions", endpoint=_list_versions, methods=["GET"]),
    Route("/api/mediaplans/{plan_id}/diff", endpoint=_diff_versions, methods=["GET"]),
    # Story 22.3: mapping surface (line_key may contain '/', hence :path).
    Route(
        "/api/mediaplans/{plan_id}/lines/{line_key:path}/mappings",
        endpoint=_set_line_mappings,
        methods=["PUT"],
    ),
    Route("/api/mediaplans/{plan_id}/mappings", endpoint=_list_mappings, methods=["GET"]),
    Route(
        "/api/mediaplans/{plan_id}/unmapped-actuals",
        endpoint=_list_unmapped_actuals,
        methods=["GET"],
    ),
    Route("/api/mediaplans/{plan_id}/pacing", endpoint=_get_pacing, methods=["GET"]),
    Route("/api/mediaplans/{plan_id}", endpoint=_get_plan, methods=["GET"]),
]
