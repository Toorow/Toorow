"""Shared authentication for the non-MCP HTTP API routes (/api/*, /admin).

review-2-6 F-01: the _combined_app dispatcher routes /api/* AROUND FastMCP's
RequireAuthMiddleware, and the first implementations only checked that a
Bearer header was PRESENT — any string passed in static/oauth mode. This
module performs REAL verification by reusing the exact verifier that
auth_config builds for the MCP boundary (public FastMCP API:
``verify_token(token) -> AccessToken | None``), so both boundaries accept
precisely the same tokens.

Modes (TOOROW_AUTH_MODE):
    disabled -> every request authorized, identity "anonymous" (dev/test).
    static / oauth -> Bearer token verified against the configured verifier.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from core.auth_config import build_auth_provider

if TYPE_CHECKING:  # pragma: no cover
    from starlette.requests import Request

logger = logging.getLogger(__name__)

# Verifier cache: auth mode is process-lifetime configuration; rebuilding the
# JWKS-backed verifier per request would hammer the JWKS endpoint. Tests that
# switch TOOROW_AUTH_MODE call reset_verifier_cache().
_cached: dict[str, object] = {}

# One-time disabled-mode warning: set to False after the first emission so we
# log exactly once per process, not once per request.
_disabled_warning_emitted: bool = False


def reset_verifier_cache() -> None:
    """Clear the memoized verifier (used by tests switching auth modes)."""
    global _disabled_warning_emitted  # noqa: PLW0603
    _cached.clear()
    _disabled_warning_emitted = False


def _verifier():
    mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    key = f"verifier::{mode}"
    if key not in _cached:
        _cached.clear()
        _cached[key] = build_auth_provider()
    return _cached[key]


async def authenticate_api_request(request: "Request") -> tuple[bool, str]:
    """Verify the request's Bearer token against the configured auth mode.

    Returns:
        (True, identity)  -- request may proceed; identity is the token's
                             subject (or client_id fallback), "anonymous" in
                             disabled mode.
        (False, "")       -- caller must return 401.
    """
    global _disabled_warning_emitted  # noqa: PLW0603
    mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if mode == "disabled":
        if not _disabled_warning_emitted:
            _disabled_warning_emitted = True
            logger.warning(
                "TOOROW_AUTH_MODE=disabled -- "
                "API ouverte sans authentification (dev uniquement)"
            )
        return True, "anonymous"

    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return False, ""
    token = auth_header[7:].strip()
    if not token:
        return False, ""

    verifier = _verifier()
    if verifier is None:  # defensive: mode says protected but no verifier built
        logger.error("api_auth_misconfigured: mode=%s but no verifier", mode)
        return False, ""

    access = await verifier.verify_token(token)  # type: ignore[attr-defined]
    if access is None:
        return False, ""

    identity = access.subject or access.client_id or "authenticated-user"
    return True, identity

async def authenticate_invitation_identity(request: "Request") -> tuple[bool, str]:
    """Resolve the verified email identity required by invitation acceptance.

    OAuth subjects are commonly opaque identifiers, so invitation matching must
    use an explicitly verified email claim rather than assuming `sub` is an
    email address. Static mode may use its configured email-shaped subject for
    local development. Disabled mode is never accepted.
    """
    mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if mode == "disabled":
        return False, ""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return False, ""
    token = auth_header[7:].strip()
    if not token:
        return False, ""
    verifier = _verifier()
    if verifier is None:
        return False, ""
    access = await verifier.verify_token(token)  # type: ignore[attr-defined]
    if access is None:
        return False, ""
    claims = access.claims if isinstance(access.claims, dict) else {}
    email = claims.get("email")
    email_verified = claims.get("email_verified")
    if (
        isinstance(email, str)
        and email.strip()
        and (email_verified is True or str(email_verified).lower() == "true")
    ):
        return True, email
    subject = access.subject or access.client_id or ""
    if mode == "static" and isinstance(subject, str) and "@" in subject:
        return True, subject
    return False, ""
