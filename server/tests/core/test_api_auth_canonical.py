from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from core import org_members_api  # AD-43 : le handler vit chez son sujet
from starlette.requests import Request


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/test",
            "headers": [(b"authorization", b"Bearer token")],
        }
    )


def _access(*, subject="subject-1", issuer="https://issuer.example", verified=True):
    """Mirror what FastMCP's JWTVerifier ACTUALLY returns.

    This fixture used to set ``access.subject = subject`` and leave ``sub`` out
    of the claims. No real verifier behaves that way, and the difference was not
    cosmetic: JWTVerifier leaves ``subject`` at None, puts the token's ``sub`` in
    ``client_id``, and exposes the claim only through ``.claims``. Because the
    fixture supplied a ``subject`` the production code read, every test here
    passed while the deployed code rejected 100% of valid tokens -- observed in
    production on 2026-07-27, where a correctly signed Google token got a flat
    401 and nobody could authenticate at all.

    ``subject`` now lands where the verifier really puts it (claims["sub"]), and
    ``access.subject`` is None exactly as in production, so these tests exercise
    the seam that actually broke.
    """
    access = MagicMock()
    access.subject = None
    access.client_id = "shared-client"
    access.claims = {
        "iss": issuer,
        "email": "Person@Example.com",
        "email_verified": verified,
        "name": "Person One",
    }
    if subject:
        access.claims["sub"] = subject
    return access


def _access_like_jwtverifier(sub: str):
    """A token shaped exactly like a Google ID token seen through JWTVerifier."""
    access = MagicMock()
    access.subject = None
    access.client_id = sub  # JWTVerifier puts `sub` here; it is NOT the identity key
    access.claims = {
        "iss": "https://accounts.google.com",
        "sub": sub,
        "aud": "client-id.apps.googleusercontent.com",
        "email": "person@example.com",
        "email_verified": True,
    }
    return access


def test_canonical_principal_uses_exact_issuer_subject_and_verified_claim(monkeypatch):
    from core import api_auth, canonical_identity

    access = _access()
    verifier = MagicMock()
    verifier.verify_token = AsyncMock(return_value=access)
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setattr(api_auth, "_verifier", lambda: verifier)
    resolved = canonical_identity.CanonicalIdentity(
        person_id="person_1",
        issuer="https://issuer.example",
        subject="subject-1",
        verified_email="person@example.com",
        created=True,
    )
    resolver = MagicMock(return_value=resolved)
    monkeypatch.setattr(canonical_identity, "resolve_canonical_identity", resolver)

    ok, principal = asyncio.run(api_auth.authenticate_canonical_principal(_request(), MagicMock()))

    assert ok is True
    assert principal is not None
    assert principal.person_id == "person_1"
    assert principal.display_name == "Person One"
    assert resolver.call_args.kwargs == {
        "issuer": "https://issuer.example",
        "subject": "subject-1",
        "verified_email": "Person@Example.com",
    }


def test_missing_subject_never_falls_back_to_client_id(monkeypatch):
    from core import api_auth, canonical_identity

    access = _access(subject="")
    verifier = MagicMock()
    verifier.verify_token = AsyncMock(return_value=access)
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setattr(api_auth, "_verifier", lambda: verifier)
    resolver = MagicMock()
    monkeypatch.setattr(canonical_identity, "resolve_canonical_identity", resolver)

    assert asyncio.run(api_auth.authenticate_canonical_principal(_request(), MagicMock())) == (
        False,
        None,
    )
    resolver.assert_not_called()


def test_configured_issuer_mismatch_is_refused(monkeypatch):
    from core import api_auth

    verifier = MagicMock()
    verifier.verify_token = AsyncMock(return_value=_access(issuer="https://foreign.example"))
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setattr(api_auth, "_verifier", lambda: verifier)

    assert asyncio.run(api_auth.authenticate_canonical_principal(_request(), MagicMock())) == (
        False,
        None,
    )


def test_unverified_email_is_not_retained(monkeypatch):
    from core import api_auth, canonical_identity

    verifier = MagicMock()
    verifier.verify_token = AsyncMock(return_value=_access(verified=False))
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setattr(api_auth, "_verifier", lambda: verifier)
    resolved = canonical_identity.CanonicalIdentity(
        person_id="person_2",
        issuer="https://issuer.example",
        subject="subject-1",
        verified_email=None,
        created=True,
    )
    resolver = MagicMock(return_value=resolved)
    monkeypatch.setattr(canonical_identity, "resolve_canonical_identity", resolver)

    ok, _ = asyncio.run(api_auth.authenticate_canonical_principal(_request(), MagicMock()))

    assert ok is True
    assert resolver.call_args.kwargs["verified_email"] is None


def test_malformed_canonical_claim_is_a_controlled_refusal(monkeypatch):
    from core import api_auth, canonical_identity

    verifier = MagicMock()
    verifier.verify_token = AsyncMock(return_value=_access())
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_ISSUER", "https://issuer.example")
    monkeypatch.setattr(api_auth, "_verifier", lambda: verifier)
    resolver = MagicMock(
        side_effect=canonical_identity.CanonicalIdentityValidationError(
            "verified_email is malformed"
        )
    )
    monkeypatch.setattr(canonical_identity, "resolve_canonical_identity", resolver)

    assert asyncio.run(api_auth.authenticate_canonical_principal(_request(), MagicMock())) == (
        False,
        None,
    )
    resolver.assert_called_once()


def test_disabled_auth_does_not_create_a_canonical_person(monkeypatch):
    from core import api_auth

    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    assert asyncio.run(api_auth.authenticate_canonical_principal(_request(), MagicMock())) == (
        False,
        None,
    )


def test_direct_member_enrollment_is_refused_unconditionally(monkeypatch):
    """No environment, no configuration, re-opens the direct-write path.

    Renamed from `test_canonical_mode_disallows_direct_member_enrollment` on
    2026-08-24: "canonical mode" was one of two modes, and the other one let this
    route write a caller-supplied string into `app.org_members.identity`. There
    is no mode any more, so there is nothing left for the name to distinguish.
    """

    from core import admin_api

    monkeypatch.setattr(
        admin_api,
        "_check_auth",
        AsyncMock(return_value=(True, "person-1")),
    )

    response = asyncio.run(org_members_api._add_org_member(_request()))

    assert response.status_code == 409
    assert b"invitation_required" in response.body


def test_google_id_token_through_jwtverifier_authenticates(monkeypatch):
    """Regression, production incident 2026-07-27.

    A Google-signed ID token with correct iss / aud / sub / email_verified was
    rejected with 401 by every endpoint once canonical resolution was turned on,
    because the resolver read `access.subject` (always None with JWTVerifier)
    instead of the `sub` claim. Nobody could authenticate.

    This asserts the real shape end to end: subject=None, `sub` only in claims,
    and `client_id` carrying the same value -- the identity must resolve from
    the CLAIM, and the resolver must be handed that claim, not the client id.
    """
    from core import api_auth, canonical_identity

    verifier = MagicMock()
    verifier.verify_token = AsyncMock(
        return_value=_access_like_jwtverifier("117505563874619937900")
    )
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_ISSUER", "https://accounts.google.com")
    monkeypatch.setattr(api_auth, "_verifier", lambda: verifier)
    resolved = canonical_identity.CanonicalIdentity(
        person_id="person_2",
        issuer="https://accounts.google.com",
        subject="117505563874619937900",
        verified_email="person@example.com",
        created=True,
    )
    resolver = MagicMock(return_value=resolved)
    monkeypatch.setattr(canonical_identity, "resolve_canonical_identity", resolver)

    ok, principal = asyncio.run(api_auth.authenticate_canonical_principal(_request(), MagicMock()))

    assert ok is True, "a valid Google ID token must authenticate"
    assert principal is not None
    assert principal.person_id == "person_2"
    assert resolver.call_args.kwargs["subject"] == "117505563874619937900"
    assert resolver.call_args.kwargs["issuer"] == "https://accounts.google.com"
