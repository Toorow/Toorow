"""toorow -- org-plan control surface (Story 34.3, Epic 34).

The SINGLE admin->prod WRITE of the trial-governance epic. A narrow, gated,
audited REST route that promotes/demotes an org's plan by delegating to
``core.org_entitlements.set_org_plan`` (34.1). This is what the private
``toorow-admin`` surface calls; here we ship only the secured prod endpoint.

  POST /api/admin/org-plan
      Body: {org_id, plan, entitlements?, reason?}
      -> set_org_plan(org_id, plan, entitlements, granted_by=<caller>).
         200 {org_id, plan} on success.

GATING (deny-by-default, AD-7 audited):
  * Bearer token required (core.api_auth) -> 401 when absent/invalid.
  * Caller identity MUST be in the TOOROW_SUPER_ADMINS allow-list
    (core.super_admin.is_super_admin). A non-super-admin gets 404 -- we do NOT
    reveal the surface exists (deny-by-default, no 403).
  * Every accepted change is written to app.audit_log via write_audit_row.

Because set_org_plan writes app.org_plan directly, the 34.2/34.3 guards read the
new plan on their VERY NEXT call (resolve_entitlements re-reads live) -- so a
super-admin promotion lifts the caps immediately.

ASCII-only strings (Windows/CI safe). Lazy imports keep this module import-safe
even when admin_api imports it at startup (no circular import at load time).
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# Audit action for the plan change (string literal -- no edit to core.audit constants).
_ACTION_ORG_PLAN_CHANGED = "org_plan.changed"

# Deny-by-default 404 body: identical shape whether the org exists or not so a
# non-super-admin cannot probe org existence via this surface.
_NOT_FOUND = {"code": "not_found", "message": "Ressource introuvable"}


def _service_identity_or_none(request: Request) -> str | None:
    """Server-to-server provisioning path (toorow-admin).

    A strong shared secret in the ``X-Provision-Token`` header authorizes the call
    as the service identity ``svc:toorow-admin`` -- so the admin backend can
    provision a trial org at invitation-acceptance without a human OAuth identity.
    Constant-time compare; disabled unless ``TOOROW_PROVISION_TOKEN`` is set.
    Never falls back to a truthy default (empty secret => never authorizes).
    """
    import hmac  # noqa: PLC0415
    import os  # noqa: PLC0415

    expected = os.environ.get("TOOROW_PROVISION_TOKEN", "")
    provided = request.headers.get("X-Provision-Token", "")
    if expected and provided and hmac.compare_digest(provided, expected):
        return "svc:toorow-admin"
    return None


async def _set_org_plan(request: Request) -> Response:
    """POST /api/admin/org-plan -- plan control.

    Two authorized callers: a human super-admin (OAuth identity in the allow-list)
    OR the toorow-admin service (X-Provision-Token shared secret). Anyone else: the
    surface stays hidden (404 for an authenticated non-super-admin, 401 if no auth).
    """
    from core.super_admin import is_super_admin  # noqa: PLC0415

    identity = _service_identity_or_none(request)
    if identity is None:
        from core.api_auth import authenticate_api_request  # noqa: PLC0415

        authorized, oauth_identity = await authenticate_api_request(request)
        if not authorized:
            return JSONResponse(
                {"code": "unauthorized", "message": "Token d'acces requis"}, status_code=401
            )
        # Deny-by-default: a caller not in the allow-list gets 404 (surface hidden).
        if not is_super_admin(oauth_identity):
            logger.info("org_plan_api: denied non-super-admin identity=%r", oauth_identity)
            return JSONResponse(_NOT_FOUND, status_code=404)
        identity = oauth_identity

    try:
        body: dict = json.loads(await request.body())
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide: {exc}"},
            status_code=400,
        )

    org_id = (body.get("org_id") or "").strip()
    plan = (body.get("plan") or "").strip()
    if not org_id or not plan:
        return JSONResponse(
            {"code": "missing_field", "message": "org_id et plan sont requis"},
            status_code=422,
        )

    # Default entitlements: trial gets the bounded defaults; full/internal are
    # unlimited (empty dict -> None limits resolved Python-side by 34.1).
    from core.org_entitlements import (  # noqa: PLC0415
        DEFAULT_TRIAL_ENTITLEMENTS,
        PLAN_TRIAL,
        set_org_plan,
    )

    entitlements = body.get("entitlements")
    if not isinstance(entitlements, dict):
        entitlements = dict(DEFAULT_TRIAL_ENTITLEMENTS) if plan == PLAN_TRIAL else {}

    try:
        set_org_plan(org_id, plan, entitlements, granted_by=identity)
    except ValueError as exc:
        # Invalid plan or unknown org_id -- no partial state committed (34.1).
        return JSONResponse(
            {"code": "invalid_input", "message": str(exc)}, status_code=422
        )
    except Exception as exc:
        logger.error("org_plan_api: set_org_plan_error org=%s: %s", org_id, exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Ecriture indisponible"}, status_code=503
        )

    # AD-7: audit the accepted control action.
    from core.audit import write_audit_row  # noqa: PLC0415

    write_audit_row(
        identity=identity,
        action=_ACTION_ORG_PLAN_CHANGED,
        provider_account="",
        connection_ref="",
        metadata={
            "org_id": org_id,
            "plan": plan,
            "reason": body.get("reason", ""),
        },
    )
    logger.info("org_plan_api: org=%s plan=%s granted_by=%s", org_id, plan, identity)
    return JSONResponse({"org_id": org_id, "plan": plan}, status_code=200)


ORG_PLAN_ROUTES = [
    Route("/api/admin/org-plan", endpoint=_set_org_plan, methods=["POST"]),
]
