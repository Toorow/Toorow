"""monday.com OAuth 2.1 broker adapter.

The platform manifest uses the generic ``oauth2`` auth family, while this
module enforces monday's OAuth 2.1 requirements at the provider boundary.
Token storage and HTTP transport are injected so secrets never enter the
connector manifest or logs.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

AUTHORIZE_URL = "https://auth.monday.com/oauth2/authorize"
TOKEN_URL = "https://auth.monday.com/oauth_ms/oauth/token"
REVOKE_URL = "https://auth.monday.com/oauth_ms/oauth/revoke"
WELL_KNOWN_URL = "https://auth.monday.com/oauth_ms/.well-known/oauth-authorization-server"
MAX_LINEAGE_AGE = timedelta(days=183)
REFRESH_SKEW = timedelta(minutes=5)


class ReauthorizationRequired(RuntimeError):
    """The rotating-token lineage reached monday's six-month boundary."""


class ConcurrentTokenRotation(RuntimeError):
    """Another worker replaced the rotating refresh token first."""


def discover_authorization_server(transport):
    """Load and fail closed on monday's OAuth 2.1 discovery document."""
    document = transport.get(WELL_KNOWN_URL)
    expected = {
        "authorization_endpoint": AUTHORIZE_URL,
        "token_endpoint": TOKEN_URL,
        "revocation_endpoint": REVOKE_URL,
    }
    for key, value in expected.items():
        if document.get(key) != value:
            raise ValueError(f"monday OAuth discovery mismatch for {key}")
    methods = document.get("code_challenge_methods_supported") or []
    if "S256" not in methods:
        raise ValueError("monday OAuth discovery does not advertise PKCE S256")
    return document


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_verifier: str,
    scopes: list[str],
) -> str:
    if not state:
        raise ValueError("OAuth state must be non-empty")
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": code_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(code, redirect_uri, code_verifier, client_id, client_secret, transport):
    return transport.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )


def _as_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def refresh_if_needed(
    connection_id,
    client_id,
    client_secret,
    store,
    transport,
    *,
    now: datetime | None = None,
):
    now = now or datetime.now(UTC)
    current = store.read(connection_id)
    if now - _as_datetime(current["lineage_started_at"]) >= MAX_LINEAGE_AGE:
        raise ReauthorizationRequired("monday OAuth consent must be renewed after six months")
    if _as_datetime(current["expires_at"]) - now > REFRESH_SKEW:
        return current
    payload = transport.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": current["refresh_token"],
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    replacement = {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": (now + timedelta(seconds=int(payload["expires_in"]))).isoformat(),
        "lineage_started_at": current["lineage_started_at"],
    }
    if not store.compare_and_swap(connection_id, current["refresh_token"], replacement):
        raise ConcurrentTokenRotation("refresh token was concurrently rotated")
    return replacement


def revoke_token(token, client_id, client_secret, transport):
    return transport.post(
        REVOKE_URL,
        data={"token": token, "client_id": client_id, "client_secret": client_secret},
    )
