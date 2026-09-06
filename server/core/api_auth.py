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
from typing import TYPE_CHECKING, NamedTuple

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


def request_carries_a_credential(request: "Request") -> bool:
    """A Bearer token or a session cookie -- the only things worth looking at.

    A HEADER check, nothing else: it must not cost what it exists to save.
    """
    from core.browser_oidc import SESSION_COOKIE_NAME  # noqa: PLC0415

    if request.cookies.get(SESSION_COOKIE_NAME):
        return True
    auth_header = request.headers.get("authorization", "")
    return auth_header.lower().startswith("bearer ") and bool(auth_header[7:].strip())


async def resolve_request_principal(
    request: "Request",
) -> tuple[bool, ResolvedPrincipal | None]:
    """THE door every HTTP module walks through to learn who is asking.

    Order, ratified in README.md "One authorization key" (2026-08-30):
      1. no Bearer token and no session cookie -> refused, no connection;
      2. a session cookie is read ONCE (`get_browser_session` validates and
         checks revocation against its own store) -- then bound with one
         connection;
      3. a Bearer token is verified WITHOUT the database and only a token the
         verifier accepted is bound to its person with one connection.
    A wrong token is a 401 that owes the database nothing; an anonymous probe
    costs no connection; a session costs exactly what it cost before.
    """
    mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if mode == "disabled" or not request_carries_a_credential(request):
        return False, None

    from core.browser_oidc import get_browser_session  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    browser_session = get_browser_session(request)
    credential: VerifiedCredential | None = None
    if browser_session is None:
        credential = await verify_bearer_credential(request)
        if credential is None:
            return False, None

    with get_connection() as conn:
        if credential is not None:
            ok, principal = bind_verified_credential(credential, conn)
        else:
            ok, principal = _resolve_browser_canonical_principal(browser_session, conn)
        if ok and principal is not None:
            conn.commit()
            # THE DOOR'S ANSWER TRAVELS WITH THE REQUEST (AI-323, 2026-08-30): a
            # later "who is this?" on the same request -- the verified e-mail an
            # invitation transition needs -- reads it here instead of reading the
            # session (and its revocation store) a second time.
            _remember_principal(request, principal)
            return True, principal
        return False, None


_PRINCIPAL_STATE_KEY = "toorow_principal"


def _remember_principal(request: "Request", principal: ResolvedPrincipal) -> None:
    try:
        setattr(request.state, _PRINCIPAL_STATE_KEY, principal)
    except Exception:  # noqa: BLE001 -- a request without a state is still authenticated
        return


def remembered_principal(request: "Request") -> ResolvedPrincipal | None:
    """The principal the door already bound for this request, or None."""
    try:
        value = getattr(request.state, _PRINCIPAL_STATE_KEY, None)
    except Exception:  # noqa: BLE001
        return None
    return value if isinstance(value, ResolvedPrincipal) else None


async def _authenticate_canonical_api_request(request: "Request") -> tuple[bool, str]:
    """Resolve the same person identity for every HTTP API module."""
    ok, principal = await resolve_request_principal(request)
    if ok and principal is not None:
        return True, principal.person_id
    return False, ""


async def authenticate_api_request(request: "Request") -> tuple[bool, str]:
    """Verify the request's Bearer token against the configured auth mode.

    Returns:
        (True, identity)  -- request may proceed; identity is the CANONICAL
                             ``person_<ULID>`` this token resolves to,
                             "anonymous" in disabled mode.
        (False, "")       -- caller must return 401.

    THERE IS NO SECOND AUTHORIZATION KEY, and no configuration selects one.
    Until 2026-08-24 an environment flag chose between this resolver and a legacy
    path that returned the raw OIDC ``sub``; its code default was OFF, so an
    environment that merely never set it authorized against a key
    ``app.org_members`` no longer stores -- fail-open by omission, and the wrong
    SHAPE for a security switch whatever value it carried.
    `tests/conformance/test_no_identity_flag_returns.py` refuses its return, and
    carries the full account.
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

    return await _authenticate_canonical_api_request(request)


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
    # Already answered by the door on this request: its `verified_email` is
    # computed by the same rules as below (session claims, or the token's
    # verified e-mail / static e-mail-shaped subject). No second read (AI-323).
    remembered = remembered_principal(request)
    if remembered is not None:
        email = remembered.verified_email
        return (True, email) if email else (False, "")
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

    credential = await verify_bearer_credential(request)
    if credential is None:
        return False, None
    return bind_verified_credential(credential, conn)


class VerifiedCredential(NamedTuple):
    """A Bearer token the verifier accepted, reduced to what binds a person."""

    issuer: str
    subject: str
    verified_email: str | None
    claims: dict


async def verify_bearer_credential(request: "Request") -> VerifiedCredential | None:
    """Verify the Bearer token WITHOUT a database: the claims, or None.

    Split out on 2026-08-30 so the connection is opened only for a token the
    verifier accepted -- a wrong token answered 500 (connection first, then the
    refusal) instead of 401, and every probe with an arbitrary token cost a
    connection before it was refused.
    """

    mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if mode == "disabled":
        return None
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[7:].strip()
    if not token:
        return None
    verifier = _verifier()
    if verifier is None:
        return None
    access = await verifier.verify_token(token)  # type: ignore[attr-defined]
    if access is None:
        return None
    claims = access.claims if isinstance(access.claims, dict) else {}
    # The canonical identity key is the `sub` CLAIM, read from the verified
    # claims -- not `access.subject`.
    #
    # FastMCP's JWTVerifier leaves `AccessToken.subject` at None and puts the
    # `sub` value in `client_id`; the claim itself is only ever exposed through
    # `.claims`. Reading `.subject` therefore rejected EVERY valid token, and
    # since this resolver is the only authentication path that meant nobody could
    # authenticate at all (observed in production 2026-07-27: a Google-signed
    # token with iss/aud/sub/email_verified all correct got a flat 401, and no
    # `canonical_identity_denied` log line because this branch returns silently).
    #
    # `client_id` is deliberately NOT used as a fallback, per this function's own
    # contract: it happens to carry the same value for a Google ID token, but it
    # answers "which client", not "which person", and a verifier that filled it
    # differently would silently bind two people to one identity.
    subject_claim = claims.get("sub")
    subject = subject_claim.strip() if isinstance(subject_claim, str) else ""
    if not subject:
        return None
    issuer_claim = claims.get("iss")
    if mode == "static":
        issuer = "static://toorow"
    else:
        configured_issuer = os.environ.get("TOOROW_JWT_ISSUER", "").strip()
        issuer = issuer_claim.strip() if isinstance(issuer_claim, str) else configured_issuer
        if not issuer:
            return None
        if configured_issuer and issuer != configured_issuer:
            return None

    verified_email = None
    email = claims.get("email")
    email_verified = claims.get("email_verified")
    if isinstance(email, str) and (email_verified is True or str(email_verified).lower() == "true"):
        verified_email = email
    elif mode == "static" and "@" in subject:
        verified_email = subject
    return VerifiedCredential(
        issuer=issuer, subject=subject, verified_email=verified_email, claims=claims
    )


def bind_verified_credential(
    credential: VerifiedCredential, conn
) -> tuple[bool, ResolvedPrincipal | None]:
    """Bind a verified credential to its exact person -- the only step that reads."""

    from core.canonical_identity import (  # noqa: PLC0415
        CanonicalIdentityUnavailable,
        CanonicalIdentityValidationError,
        resolve_canonical_identity,
    )
    from core.user_profiles import display_name_from_claims  # noqa: PLC0415

    claims = credential.claims
    try:
        canonical = resolve_canonical_identity(
            conn,
            issuer=credential.issuer,
            subject=credential.subject,
            verified_email=credential.verified_email,
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
