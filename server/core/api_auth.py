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
from dataclasses import dataclass
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


def _canonical_identity_enabled() -> bool:
    return os.environ.get("TOOROW_CANONICAL_IDENTITY_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


async def _authenticate_canonical_api_request(request: "Request") -> tuple[bool, str]:
    """Resolve the same person identity for every HTTP API module."""
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        ok, principal = await authenticate_canonical_principal(request, conn)
        if ok and principal is not None:
            conn.commit()
            return True, principal.person_id
        return False, ""


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
                "TOOROW_AUTH_MODE=disabled -- API ouverte sans authentification (dev uniquement)"
            )
        return True, "anonymous"

    if _canonical_identity_enabled():
        return await _authenticate_canonical_api_request(request)

    from core.browser_oidc import get_browser_session  # noqa: PLC0415

    browser_session = get_browser_session(request)
    if browser_session is not None:
        return True, browser_session.subject

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


async def authenticate_subject_and_name(request: "Request") -> tuple[bool, str, str | None]:
    """Resolve the PROFILE key and the name the identity provider gives us.

    Returns (ok, subject, display_name_or_None).

    Why a third resolver rather than widening the two above: they answer two
    DIFFERENT questions and callers rely on that. `authenticate_api_request`
    answers "who is this, for authorization" (the opaque subject);
    `authenticate_invitation_identity` answers "which invited EMAIL is this"
    (an invitation is addressed to an address, never to an opaque `sub`).

    This one exists because the invitation-acceptance path needs BOTH at once,
    and the subject is the one that matters here: ``app.user_profiles`` is keyed
    on the subject, and so is GET/PATCH /api/me/profile. Writing a profile under
    the verified email instead would store the name where nothing reads it --
    the screen would keep asking for a name we already had.
    """
    ok, subject = await authenticate_api_request(request)
    if not ok or not subject:
        return False, "", None
    # Re-reading the token here costs one verification on a path that runs once
    # per person, ever. The alternative -- threading claims through
    # authenticate_api_request's return type -- would change a signature every
    # authenticated endpoint depends on, for one caller.
    from core.browser_oidc import get_browser_session  # noqa: PLC0415

    browser_session = get_browser_session(request)
    if browser_session is not None:
        from core.user_profiles import display_name_from_claims  # noqa: PLC0415

        return True, subject, display_name_from_claims(browser_session.claims)

    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return True, subject, None
    verifier = _verifier()
    if verifier is None:
        return True, subject, None
    try:
        access = await verifier.verify_token(auth_header[7:].strip())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - a name is a nicety; never fail the caller
        logger.exception("api_auth: could not re-read claims for a profile name")
        return True, subject, None
    if access is None:
        return True, subject, None
    from core.user_profiles import display_name_from_claims  # noqa: PLC0415

    return True, subject, display_name_from_claims(getattr(access, "claims", None))


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
    from core.browser_oidc import get_browser_session  # noqa: PLC0415

    browser_session = get_browser_session(request)
    if browser_session is not None:
        claims = browser_session.claims
        email = claims.get("email")
        email_verified = claims.get("email_verified")
        if (
            isinstance(email, str)
            and email.strip()
            and (email_verified is True or str(email_verified).lower() == "true")
        ):
            return True, email
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


@dataclass(frozen=True, slots=True)
class ResolvedPrincipal:
    """One authenticated human bound to the canonical application person."""

    person_id: str
    issuer: str
    subject: str
    verified_email: str | None
    display_name: str | None


def _resolve_browser_canonical_principal(
    session,
    conn,
) -> tuple[bool, ResolvedPrincipal | None]:
    """Bind an already-verified OIDC browser session to its canonical person."""
    claims = session.claims
    verified_email = None
    email = claims.get("email")
    email_verified = claims.get("email_verified")
    if isinstance(email, str) and (email_verified is True or str(email_verified).lower() == "true"):
        verified_email = email

    from core.canonical_identity import (  # noqa: PLC0415
        CanonicalIdentityUnavailable,
        CanonicalIdentityValidationError,
        resolve_canonical_identity,
    )
    from core.user_profiles import display_name_from_claims  # noqa: PLC0415

    try:
        canonical = resolve_canonical_identity(
            conn,
            issuer=session.issuer,
            subject=session.subject,
            verified_email=verified_email,
        )
    except CanonicalIdentityValidationError:
        logger.warning("canonical_identity_denied reason=invalid_claim")
        return False, None
    except CanonicalIdentityUnavailable as exc:
        logger.warning(
            "canonical_identity_denied correlation_id=%s",
            exc.correlation_id,
        )
        return False, None
    return True, ResolvedPrincipal(
        person_id=canonical.person_id,
        issuer=canonical.issuer,
        subject=canonical.subject,
        verified_email=canonical.verified_email,
        display_name=display_name_from_claims(claims),
    )


async def authenticate_canonical_principal(
    request: "Request",
    conn,
) -> tuple[bool, ResolvedPrincipal | None]:
    """Verify one human token and resolve its exact ``(issuer, subject)`` person.

    Missing ``sub`` never falls back to ``client_id`` or a shared sentinel.
    OAuth requires an issuer. Static mode uses an explicit local issuer. A
    verified email is retained as a claim only; the resolver never merges on it.
    """

    mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if mode == "disabled":
        return False, None

    from core.browser_oidc import get_browser_session  # noqa: PLC0415

    browser_session = get_browser_session(request)
    if browser_session is not None:
        return _resolve_browser_canonical_principal(browser_session, conn)

    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return False, None
    token = auth_header[7:].strip()
    if not token:
        return False, None
    verifier = _verifier()
    if verifier is None:
        return False, None
    access = await verifier.verify_token(token)  # type: ignore[attr-defined]
    if access is None:
        return False, None
    subject = access.subject if isinstance(access.subject, str) else ""
    if not subject:
        return False, None
    claims = access.claims if isinstance(access.claims, dict) else {}
    issuer_claim = claims.get("iss")
    if mode == "static":
        issuer = "static://toorow"
    else:
        configured_issuer = os.environ.get("TOOROW_JWT_ISSUER", "").strip()
        issuer = issuer_claim.strip() if isinstance(issuer_claim, str) else configured_issuer
        if not issuer:
            return False, None
        if configured_issuer and issuer != configured_issuer:
            return False, None

    verified_email = None
    email = claims.get("email")
    email_verified = claims.get("email_verified")
    if isinstance(email, str) and (email_verified is True or str(email_verified).lower() == "true"):
        verified_email = email
    elif mode == "static" and "@" in subject:
        verified_email = subject

    from core.canonical_identity import (  # noqa: PLC0415
        CanonicalIdentityUnavailable,
        CanonicalIdentityValidationError,
        resolve_canonical_identity,
    )
    from core.user_profiles import display_name_from_claims  # noqa: PLC0415

    try:
        canonical = resolve_canonical_identity(
            conn,
            issuer=issuer,
            subject=subject,
            verified_email=verified_email,
        )
    except CanonicalIdentityValidationError:
        logger.warning("canonical_identity_denied reason=invalid_claim")
        return False, None
    except CanonicalIdentityUnavailable as exc:
        logger.warning(
            "canonical_identity_denied correlation_id=%s",
            exc.correlation_id,
        )
        return False, None
    return True, ResolvedPrincipal(
        person_id=canonical.person_id,
        issuer=canonical.issuer,
        subject=canonical.subject,
        verified_email=canonical.verified_email,
        display_name=display_name_from_claims(claims),
    )
