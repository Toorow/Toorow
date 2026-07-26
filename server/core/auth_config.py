"""Auth mode resolver for the connector MCP server.

Story 2.3 - Every Call Has an Identity.

Reads TOOROW_AUTH_MODE at import time of build_auth_provider() and returns
the appropriate FastMCP AuthProvider (or None for disabled mode).

Accepted values for TOOROW_AUTH_MODE:
  disabled  (default) - no auth, all requests accepted
  static              - StaticTokenVerifier, for dev/CI with a known token
  oauth               - JWTVerifier (RS256), for cloud deployments

Only this module reads auth-related env vars. Call build_auth_provider() once
at server startup and pass the result to FastMCP("toorow", auth=...).
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_VALID_MODES = ("disabled", "static", "oauth")


def build_auth_provider():
    """Build and return the FastMCP AuthProvider for the configured mode.

    Reads TOOROW_AUTH_MODE (default: "disabled"). Returns None in disabled
    mode so FastMCP runs without any auth middleware.

    Raises:
        ValueError: If TOOROW_AUTH_MODE is invalid, or required env vars
                    for the selected mode are missing/conflicting.
    """
    mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()

    if mode not in _VALID_MODES:
        raise ValueError(
            f"TOOROW_AUTH_MODE={mode!r} is not valid. Accepted values: {', '.join(_VALID_MODES)}"
        )

    if mode == "disabled":
        logger.debug("Auth mode: disabled (no token required)")
        return None

    if mode == "static":
        return _build_static_verifier()

    if mode == "oauth":
        return _build_jwt_verifier()

    # Unreachable, but satisfies static analysis
    return None  # pragma: no cover


def _build_static_verifier():
    """Build a StaticTokenVerifier from env vars.

    Required:
        TOOROW_STATIC_TOKEN  - the bearer token string to accept
    Optional:
        TOOROW_STATIC_SUBJECT - the identity subject (default: "default-user")
    """
    from fastmcp.server.auth import StaticTokenVerifier

    token = os.environ.get("TOOROW_STATIC_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "TOOROW_AUTH_MODE=static requires TOOROW_STATIC_TOKEN to be set to a non-empty string."
        )

    subject = os.environ.get("TOOROW_STATIC_SUBJECT", "default-user").strip()
    if not subject:
        subject = "default-user"

    logger.info("Auth mode: static (subject=%s)", subject)
    return StaticTokenVerifier(tokens={token: {"client_id": subject, "sub": subject, "scopes": []}})


def _build_jwt_verifier():
    """Build a JWTVerifier from env vars.

    Exactly one of the following must be set:
        TOOROW_JWT_PUBLIC_KEY  - PEM-encoded public key string (literal \\n supported)
        TOOROW_JWKS_URI        - URL to fetch JWKS from

    Required:
        TOOROW_JWT_AUDIENCE  - expected audience claim (replay guard)
        TOOROW_JWT_ISSUER    - expected issuer claim
    """
    from fastmcp.server.auth import JWTVerifier

    raw_key = os.environ.get("TOOROW_JWT_PUBLIC_KEY", "").strip()
    jwks_uri = os.environ.get("TOOROW_JWKS_URI", "").strip()

    if raw_key and jwks_uri:
        raise ValueError(
            "TOOROW_AUTH_MODE=oauth: set exactly one of "
            "TOOROW_JWT_PUBLIC_KEY or TOOROW_JWKS_URI, not both."
        )

    if not raw_key and not jwks_uri:
        raise ValueError(
            "TOOROW_AUTH_MODE=oauth requires either TOOROW_JWT_PUBLIC_KEY "
            "(PEM string) or TOOROW_JWKS_URI to be set."
        )

    # Env files may encode newlines as literal \n — normalise them.
    public_key: str | None = None
    if raw_key:
        public_key = raw_key.replace("\\n", "\n")

    issuer = os.environ.get("TOOROW_JWT_ISSUER", "").strip() or None
    audience = os.environ.get("TOOROW_JWT_AUDIENCE", "").strip() or None

    # review-2-3 F: without a pinned audience, any JWT minted by the same
    # issuer for ANOTHER service validates here (cross-service token replay).
    # oauth mode therefore refuses to start without an explicit audience.
    if audience is None:
        raise ValueError(
            "TOOROW_AUTH_MODE=oauth requires TOOROW_JWT_AUDIENCE (cross-service token replay guard)"
        )

    # Pinning the issuer is the other half of the JWT trust boundary. Without
    # it, a key set shared across tenants/issuers can authenticate foreign JWTs.
    if issuer is None:
        raise ValueError(
            "TOOROW_AUTH_MODE=oauth requires TOOROW_JWT_ISSUER (token issuer trust boundary)"
        )

    logger.info(
        "Auth mode: oauth (issuer=%s, audience=%s, jwks_uri=%s)",
        issuer,
        audience,
        jwks_uri or "n/a",
    )

    if public_key:
        return JWTVerifier(
            public_key=public_key,
            issuer=issuer,
            audience=audience,
            algorithm="RS256",
        )

    return JWTVerifier(
        jwks_uri=jwks_uri,
        issuer=issuer,
        audience=audience,
        algorithm="RS256",
    )
