"""Unit tests for server/core/auth_config.py (Story 2.3, T5).

Tests cover:
  T5.1 - disabled mode (explicit and default) returns None
  T5.2 - static mode builds a StaticTokenVerifier instance
  T5.3 - static mode without TOOROW_STATIC_TOKEN raises ValueError
  T5.4 - oauth mode with TOOROW_JWT_PUBLIC_KEY builds a JWTVerifier
  T5.5 - oauth mode without key or JWKS URI raises ValueError
  T5.6 - invalid TOOROW_AUTH_MODE raises ValueError

All env mutations use monkeypatch (never os.environ directly).
"""

from __future__ import annotations

import pytest


def _reload_build(monkeypatch):
    """Import build_auth_provider fresh so monkeypatched env is visible."""
    # The function reads os.environ at call time, so a plain import suffices.
    from core.auth_config import build_auth_provider

    return build_auth_provider()


# ---------------------------------------------------------------------------
# T5.1 — disabled mode
# ---------------------------------------------------------------------------


def test_disabled_mode_explicit_returns_none(monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    result = _reload_build(monkeypatch)
    assert result is None


def test_disabled_mode_unset_returns_none(monkeypatch):
    """Default (env var absent) must be disabled."""
    monkeypatch.delenv("TOOROW_AUTH_MODE", raising=False)
    result = _reload_build(monkeypatch)
    assert result is None


# ---------------------------------------------------------------------------
# T5.2 — static mode builds StaticTokenVerifier
# ---------------------------------------------------------------------------


def test_static_mode_builds_verifier(monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", "my-test-token")
    monkeypatch.delenv("TOOROW_STATIC_SUBJECT", raising=False)

    from fastmcp.server.auth import StaticTokenVerifier

    result = _reload_build(monkeypatch)
    assert isinstance(result, StaticTokenVerifier)


def test_static_mode_custom_subject(monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", "tok-abc")
    monkeypatch.setenv("TOOROW_STATIC_SUBJECT", "alice")

    result = _reload_build(monkeypatch)
    # The token map must include the subject value
    assert result is not None
    assert "tok-abc" in result.tokens
    token_data = result.tokens["tok-abc"]
    assert token_data["client_id"] == "alice"


def test_static_mode_default_subject(monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", "tok-xyz")
    monkeypatch.delenv("TOOROW_STATIC_SUBJECT", raising=False)

    result = _reload_build(monkeypatch)
    assert result is not None
    token_data = result.tokens["tok-xyz"]
    assert token_data["client_id"] == "default-user"


# ---------------------------------------------------------------------------
# T5.3 — static mode missing token raises ValueError
# ---------------------------------------------------------------------------


def test_static_mode_missing_token_raises(monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.delenv("TOOROW_STATIC_TOKEN", raising=False)

    with pytest.raises(ValueError, match="TOOROW_STATIC_TOKEN"):
        _reload_build(monkeypatch)


# ---------------------------------------------------------------------------
# T5.4 — oauth mode with public key builds JWTVerifier
# ---------------------------------------------------------------------------


def test_oauth_mode_with_public_key_builds_verifier(monkeypatch):
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    kp = RSAKeyPair.generate()
    # Use a single-line representation with literal \n (as env files would have)
    pem_oneline = kp.public_key.replace("\n", "\\n")

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_PUBLIC_KEY", pem_oneline)
    monkeypatch.delenv("TOOROW_JWKS_URI", raising=False)
    monkeypatch.setenv("TOOROW_JWT_ISSUER", "https://issuer.example")
    # review-2-3: audience is now MANDATORY in oauth mode (replay guard)
    monkeypatch.setenv("TOOROW_JWT_AUDIENCE", "connector-mcp")

    from fastmcp.server.auth import JWTVerifier

    result = _reload_build(monkeypatch)
    assert isinstance(result, JWTVerifier)


def test_oauth_mode_with_real_pem_builds_verifier(monkeypatch):
    """PEM with real newlines (not escaped) must also work."""
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    kp = RSAKeyPair.generate()

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_PUBLIC_KEY", kp.public_key)
    monkeypatch.delenv("TOOROW_JWKS_URI", raising=False)
    monkeypatch.setenv("TOOROW_JWT_ISSUER", "https://issuer.example")
    # review-2-3: audience is now MANDATORY in oauth mode (replay guard)
    monkeypatch.setenv("TOOROW_JWT_AUDIENCE", "connector-mcp")

    from fastmcp.server.auth import JWTVerifier

    result = _reload_build(monkeypatch)
    assert isinstance(result, JWTVerifier)


# ---------------------------------------------------------------------------
# T5.5 — oauth mode missing key raises ValueError
# ---------------------------------------------------------------------------


def test_oauth_mode_missing_key_raises(monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.delenv("TOOROW_JWT_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("TOOROW_JWKS_URI", raising=False)

    with pytest.raises(ValueError, match="TOOROW_JWT_PUBLIC_KEY"):
        _reload_build(monkeypatch)


def test_oauth_mode_missing_issuer_raises(monkeypatch):
    """oauth mode refuses an unpinned issuer even when keys and audience exist."""
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    kp = RSAKeyPair.generate()
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_PUBLIC_KEY", kp.public_key.replace("\n", "\\n"))
    monkeypatch.delenv("TOOROW_JWKS_URI", raising=False)
    monkeypatch.setenv("TOOROW_JWT_AUDIENCE", "connector-mcp")
    monkeypatch.delenv("TOOROW_JWT_ISSUER", raising=False)

    with pytest.raises(ValueError, match="TOOROW_JWT_ISSUER"):
        _reload_build(monkeypatch)


def test_oauth_mode_both_key_and_jwks_raises(monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_PUBLIC_KEY", "some-key")
    monkeypatch.setenv("TOOROW_JWKS_URI", "https://example.com/.well-known/jwks.json")

    with pytest.raises(ValueError, match="not both"):
        _reload_build(monkeypatch)


# ---------------------------------------------------------------------------
# T5.6 — invalid mode raises ValueError naming the bad value
# ---------------------------------------------------------------------------


def test_invalid_mode_raises(monkeypatch):
    monkeypatch.setenv("TOOROW_AUTH_MODE", "bogus")

    with pytest.raises(ValueError, match="bogus"):
        _reload_build(monkeypatch)


def test_oauth_mode_missing_audience_raises(monkeypatch):
    """oauth mode refuses to start without TOOROW_JWT_AUDIENCE (review-2-3)."""
    from fastmcp.server.auth.providers.jwt import RSAKeyPair

    kp = RSAKeyPair.generate()
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_JWT_PUBLIC_KEY", kp.public_key.replace("\n", "\\n"))
    monkeypatch.delenv("TOOROW_JWKS_URI", raising=False)
    monkeypatch.delenv("TOOROW_JWT_AUDIENCE", raising=False)

    with pytest.raises(ValueError, match="TOOROW_JWT_AUDIENCE"):
        _reload_build(monkeypatch)
