from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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
    access = MagicMock()
    access.subject = subject
    access.client_id = "shared-client"
    access.claims = {
        "iss": issuer,
        "email": "Person@Example.com",
        "email_verified": verified,
        "name": "Person One",
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


def test_canonical_mode_disallows_direct_member_enrollment(monkeypatch):
    from core import admin_api

    monkeypatch.setenv("TOOROW_CANONICAL_IDENTITY_ENABLED", "1")
    monkeypatch.setattr(
        admin_api,
        "_check_auth",
        AsyncMock(return_value=(True, "person-1")),
    )

    response = asyncio.run(admin_api._add_org_member(_request()))

    assert response.status_code == 409
    assert b"invitation_required" in response.body
