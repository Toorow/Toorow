"""Review a governed publication before it ships, and be able to walk it back.

AD-43, 2026-08-13. Three routes: prepare the review, confirm it exactly, undo
it. Exact confirmation and rollback are what tell a governed publication apart
from a plain save; keeping them out of the read-only governance surface keeps
that contract visible.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _setup_gate_response(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _setup_gate_response as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _setup_host_context(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _setup_host_context as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _setup_no_store(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _setup_no_store as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

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
                    {"code": "invalid_request", "message": "proposal_id is required."},
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
                    {"code": "invalid_request", "message": "confirmation_secret is required."},
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
                        "message": "target_mapping_version_id is required.",
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
        status, code, message = 409, exc.code, "Confirmation refused"
    elif isinstance(exc, OperationIdempotencyConflict):
        status, code, message = 409, "conflict", "Publication already in progress"
    elif isinstance(exc, (json.JSONDecodeError, TypeError, ValueError)):
        status, code, message = 422, "invalid_request", "Invalid request"
    else:
        logger.error("admin_api: governed publication failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "Publication unavailable"
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


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : memes
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

PUBLICATION_REVIEWS_ROUTES_1 = [
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
]
