from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse


class TokenStore:
    def __init__(self, token):
        self.token = token

    def read(self, connection_id):
        return self.token

    def compare_and_swap(self, connection_id, expected_refresh_token, replacement):
        if self.token["refresh_token"] != expected_refresh_token:
            return False
        self.token = replacement
        return True


class Transport:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.payload


def test_pkce_is_s256_and_authorization_keeps_state(oauth):
    verifier = "v" * 64
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    url = oauth.build_authorization_url(
        client_id="client",
        redirect_uri="https://app.test/callback",
        state="opaque-state",
        code_verifier=verifier,
        scopes=["me:read", "boards:read"],
    )
    params = parse_qs(urlparse(url).query)
    assert params["state"] == ["opaque-state"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == [expected]


def test_refresh_rotates_token_atomically_and_enforces_six_month_lineage(oauth):
    now = datetime(2026, 7, 22, tzinfo=UTC)
    current = {
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "expires_at": (now - timedelta(seconds=1)).isoformat(),
        "lineage_started_at": (now - timedelta(days=30)).isoformat(),
    }
    store = TokenStore(current)
    transport = Transport(
        {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
    )
    token = oauth.refresh_if_needed(
        connection_id="cxn",
        client_id="client",
        client_secret="secret",
        store=store,
        transport=transport,
        now=now,
    )
    assert token["access_token"] == "new-access"
    assert store.token["refresh_token"] == "new-refresh"
    assert store.token["lineage_started_at"] == current["lineage_started_at"]
    expired = dict(store.token, lineage_started_at=(now - timedelta(days=184)).isoformat())
    store.token = expired
    try:
        oauth.refresh_if_needed("cxn", "client", "secret", store, transport, now=now)
    except oauth.ReauthorizationRequired:
        pass
    else:
        raise AssertionError("six-month token lineage must require reauthorization")


def test_discovery_and_revoke_use_monday_oauth21_endpoints(oauth):
    assert oauth.WELL_KNOWN_URL.endswith("/oauth_ms/.well-known/oauth-authorization-server")
    transport = Transport({"ok": True})
    oauth.revoke_token("refresh", "client", "secret", transport)
    assert transport.calls[0][0].endswith("/oauth_ms/oauth/revoke")
