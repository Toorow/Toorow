"""Unit tests for core.nango_client -- Story 2.2, AC6.

All HTTP calls are mocked with respx (matching the httpx choice in AC5).
No running Nango server required -- pure unit tests.

Covers:
  - list_connections: success, empty list, HTTP 500
  - get_fresh_token: happy path, 401, 403, missing access_token
  - poll_connection_health: ok (fresh), stale (old timestamp), revoked (404)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import respx

# Import the async internals directly so we can test without asyncio.run nesting
from core.nango_client import (
    BasicCredentials,
    ConnectionHealth,
    NangoTokenError,
    get_basic_credentials,
    get_fresh_token,
    list_connections,
    oauth1_proxy_request,
    poll_connection_health,
)
from httpx import Response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_URL = "http://nango-test.local"
_SECRET_KEY = "test-secret-key"

ENV_PATCH = {
    "NANGO_BASE_URL": _BASE_URL,
    "NANGO_SECRET_KEY": _SECRET_KEY,
}


def _fresh_timestamp() -> str:
    """Return an ISO timestamp from 1 hour ago (well within the 24h stale window)."""
    dt = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    return dt.isoformat()


def _stale_timestamp() -> str:
    """Return an ISO timestamp from 48 hours ago (beyond the 24h stale threshold)."""
    dt = datetime.now(tz=timezone.utc) - timedelta(hours=48)
    return dt.isoformat()


# ---------------------------------------------------------------------------
# list_connections
# ---------------------------------------------------------------------------


class TestListConnections:
    """Tests for list_connections() -- AC6 5.3."""

    @respx.mock
    def test_list_connections_success(self):
        """list_connections returns a list of connection dicts."""
        respx.get(f"{_BASE_URL}/connection").mock(
            return_value=Response(
                200,
                json={
                    "connections": [
                        {
                            "connection_id": "conn-001",
                            "provider_config_key": "my-provider",
                            "created_at": "2026-07-01T00:00:00Z",
                        },
                        {
                            "connection_id": "conn-002",
                            "provider_config_key": "my-provider",
                            "created_at": "2026-07-02T00:00:00Z",
                        },
                    ]
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            result = list_connections()

        assert len(result) == 2
        assert result[0]["connection_id"] == "conn-001"
        assert result[1]["connection_id"] == "conn-002"

    @respx.mock
    def test_list_connections_empty(self):
        """list_connections returns empty list when no connections exist."""
        respx.get(f"{_BASE_URL}/connection").mock(
            return_value=Response(200, json={"connections": []})
        )

        with patch.dict(os.environ, ENV_PATCH):
            result = list_connections()

        assert result == []

    @respx.mock
    def test_list_connections_with_provider_filter(self):
        """provider filter is applied CLIENT-SIDE (nango-server 0.70.9 has no
        server-side filter param — any query param is rejected with 400)."""
        route = respx.get(f"{_BASE_URL}/connection").mock(
            return_value=Response(
                200,
                json={
                    "connections": [
                        {"connection_id": "c1", "provider_config_key": "my-provider"},
                        {"connection_id": "c2", "provider_config_key": "other-provider"},
                    ]
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            result = list_connections(provider="my-provider")

        # No filter param on the wire; filtering happened client-side.
        called_request = route.calls[0].request
        assert "provider" not in str(called_request.url.query)
        assert [c["connection_id"] for c in result] == ["c1"]

    @respx.mock
    def test_list_connections_http_500_raises(self):
        """list_connections raises on HTTP 500."""
        respx.get(f"{_BASE_URL}/connection").mock(
            return_value=Response(500, json={"error": "internal server error"})
        )

        with patch.dict(os.environ, ENV_PATCH):
            with pytest.raises(Exception):  # httpx.HTTPStatusError
                list_connections()

    @respx.mock
    def test_list_connections_flat_list_response(self):
        """list_connections handles flat list response (alternate Nango API shape)."""
        respx.get(f"{_BASE_URL}/connection").mock(
            return_value=Response(
                200,
                json=[
                    {
                        "connection_id": "flat-001",
                        "provider_config_key": "provider-x",
                        "created_at": "2026-07-01T00:00:00Z",
                    }
                ],
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            result = list_connections()

        assert len(result) == 1
        assert result[0]["connection_id"] == "flat-001"

    def test_list_connections_missing_secret_key(self):
        """list_connections raises EnvironmentError when NANGO_SECRET_KEY is absent."""
        env = {"NANGO_BASE_URL": _BASE_URL}
        # Ensure NANGO_SECRET_KEY is not in env
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("NANGO_SECRET_KEY", None)
            with pytest.raises(EnvironmentError, match="NANGO_SECRET_KEY"):
                list_connections()


# ---------------------------------------------------------------------------
# get_fresh_token
# ---------------------------------------------------------------------------


class TestGetFreshToken:
    """Tests for get_fresh_token() -- AC6 5.4."""

    @respx.mock
    def test_get_fresh_token_happy_path(self):
        """get_fresh_token returns access_token string on success."""
        respx.get(f"{_BASE_URL}/connection/conn-001").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {
                        "access_token": "ya29.test-token-value",
                        "expires_at": "2026-07-11T02:00:00Z",
                    },
                    "connection_id": "conn-001",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            token = get_fresh_token("conn-001", provider="prov-test")

        assert token == "ya29.test-token-value"

    @respx.mock
    def test_get_fresh_token_sends_force_refresh(self):
        """get_fresh_token sends force_refresh=true query param."""
        route = respx.get(f"{_BASE_URL}/connection/conn-001").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {"access_token": "tok"},
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            get_fresh_token("conn-001", provider="prov-test")

        called_url = str(route.calls[0].request.url)
        assert "force_refresh=true" in called_url
        # 0.70.9 REQUIRES provider_config_key on GET /connection/{id}
        assert "provider_config_key=prov-test" in called_url

    @respx.mock
    def test_get_fresh_token_401_raises_nango_token_error(self):
        """get_fresh_token raises NangoTokenError on HTTP 401."""
        respx.get(f"{_BASE_URL}/connection/conn-bad").mock(
            return_value=Response(401, json={"error": "unauthorized"})
        )

        with patch.dict(os.environ, ENV_PATCH):
            with pytest.raises(NangoTokenError):
                get_fresh_token("conn-bad", provider="prov-test")

    @respx.mock
    def test_get_fresh_token_403_raises_nango_token_error(self):
        """get_fresh_token raises NangoTokenError on HTTP 403."""
        respx.get(f"{_BASE_URL}/connection/conn-forbidden").mock(
            return_value=Response(403, json={"error": "forbidden"})
        )

        with patch.dict(os.environ, ENV_PATCH):
            with pytest.raises(NangoTokenError):
                get_fresh_token("conn-forbidden", provider="prov-test")

    @respx.mock
    def test_get_fresh_token_missing_access_token_raises(self):
        """get_fresh_token raises NangoTokenError when credentials.access_token is absent."""
        respx.get(f"{_BASE_URL}/connection/conn-empty").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {},  # no access_token
                    "connection_id": "conn-empty",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            with pytest.raises(NangoTokenError, match="access_token"):
                get_fresh_token("conn-empty", provider="prov-test")

    @respx.mock
    def test_get_fresh_token_missing_credentials_raises(self):
        """get_fresh_token raises NangoTokenError when credentials key is absent."""
        respx.get(f"{_BASE_URL}/connection/conn-nocreds").mock(
            return_value=Response(
                200,
                json={"connection_id": "conn-nocreds"},  # no credentials key
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            with pytest.raises(NangoTokenError, match="access_token"):
                get_fresh_token("conn-nocreds", provider="prov-test")

    @respx.mock
    def test_get_fresh_token_404_raises_nango_token_error(self):
        """get_fresh_token raises NangoTokenError when connection is not found (404)."""
        respx.get(f"{_BASE_URL}/connection/conn-missing").mock(
            return_value=Response(404, json={"error": "not found"})
        )

        with patch.dict(os.environ, ENV_PATCH):
            with pytest.raises(NangoTokenError):
                get_fresh_token("conn-missing", provider="prov-test")


class TestGetBasicCredentials:
    """Tests for get_basic_credentials() -- self-hosted key/secret + base URL."""

    @respx.mock
    def test_basic_credentials_happy_path(self):
        """Returns username/password + connection_config from Nango."""
        respx.get(f"{_BASE_URL}/connection/conn-store").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {
                        "type": "BASIC",
                        "username": "ck_consumer_key",
                        "password": "cs_consumer_secret",
                    },
                    "connection_config": {"base_url": "https://shop.example.com"},
                    "connection_id": "conn-store",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            creds = get_basic_credentials("conn-store", provider="prov-store")

        assert isinstance(creds, BasicCredentials)
        assert creds.username == "ck_consumer_key"
        assert creds.password == "cs_consumer_secret"
        assert creds.connection_config == {"base_url": "https://shop.example.com"}

    @respx.mock
    def test_basic_credentials_no_force_refresh(self):
        """A static key/secret pair has nothing to refresh: no force_refresh sent."""
        route = respx.get(f"{_BASE_URL}/connection/conn-store").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {"username": "u", "password": "p"},
                    "connection_config": {},
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            get_basic_credentials("conn-store", provider="prov-store")

        called_url = str(route.calls[0].request.url)
        assert "force_refresh" not in called_url
        assert "provider_config_key=prov-store" in called_url

    @respx.mock
    def test_basic_credentials_defaults_config_to_empty(self):
        """Absent connection_config yields an empty dict, not None."""
        respx.get(f"{_BASE_URL}/connection/conn-store").mock(
            return_value=Response(
                200,
                json={"credentials": {"username": "u", "password": "p"}},
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            creds = get_basic_credentials("conn-store", provider="prov-store")

        assert creds.connection_config == {}

    @respx.mock
    def test_basic_credentials_401_raises(self):
        respx.get(f"{_BASE_URL}/connection/conn-bad").mock(
            return_value=Response(401, json={"error": "unauthorized"})
        )
        with patch.dict(os.environ, ENV_PATCH):
            with pytest.raises(NangoTokenError):
                get_basic_credentials("conn-bad", provider="prov-store")

    @respx.mock
    def test_basic_credentials_404_raises(self):
        respx.get(f"{_BASE_URL}/connection/conn-missing").mock(
            return_value=Response(404, json={"error": "not found"})
        )
        with patch.dict(os.environ, ENV_PATCH):
            with pytest.raises(NangoTokenError):
                get_basic_credentials("conn-missing", provider="prov-store")

    @respx.mock
    def test_basic_credentials_missing_password_raises(self):
        """A connection with username but no password is a hard error."""
        respx.get(f"{_BASE_URL}/connection/conn-half").mock(
            return_value=Response(
                200,
                json={"credentials": {"username": "u"}},  # no password
            )
        )
        with patch.dict(os.environ, ENV_PATCH):
            with pytest.raises(NangoTokenError, match="username/password"):
                get_basic_credentials("conn-half", provider="prov-store")


class TestOAuth1Proxy:
    """OAuth 1.0a remains broker-signed; provider token secrets never enter core."""

    @respx.mock
    def test_proxy_delegates_signing_with_bounded_headers(self):
        route = respx.get(f"{_BASE_URL}/proxy/12/accounts").mock(
            return_value=Response(200, json={"data": []})
        )
        with patch.dict(os.environ, ENV_PATCH):
            response = oauth1_proxy_request(
                "conn-x",
                "x-ads",
                "GET",
                "/12/accounts",
                base_url_override="https://ads-api.x.com/",
                params={"count": 10},
            )
        assert response.status_code == 200
        request = route.calls[0].request
        assert request.headers["Connection-Id"] == "conn-x"
        assert request.headers["Provider-Config-Key"] == "x-ads"
        assert request.headers["Nango-Proxy-Base-Url-Override"] == "https://ads-api.x.com"
        assert request.headers["Authorization"] == f"Bearer {_SECRET_KEY}"
        assert "oauth_token" not in str(request.url)
        assert "oauth_signature" not in str(request.url)

    @respx.mock
    def test_proxy_preserves_provider_status_and_retry_headers(self):
        respx.post(f"{_BASE_URL}/proxy/12/stats/jobs/accounts/a1").mock(
            return_value=Response(429, headers={"x-rate-limit-reset": "200"})
        )
        with patch.dict(os.environ, ENV_PATCH):
            response = oauth1_proxy_request("conn-x", "x-ads", "POST", "/12/stats/jobs/accounts/a1")
        assert response.status_code == 429
        assert response.headers["x-rate-limit-reset"] == "200"


class TestProviderResolution:
    """When provider is omitted, the client resolves it via the list endpoint."""

    @respx.mock
    def test_token_resolves_provider_when_omitted(self):
        respx.get(f"{_BASE_URL}/connection").mock(
            return_value=Response(
                200,
                json={
                    "connections": [
                        {"connection_id": "conn-r1", "provider_config_key": "prov-resolved"}
                    ]
                },
            )
        )
        detail = respx.get(f"{_BASE_URL}/connection/conn-r1").mock(
            return_value=Response(200, json={"credentials": {"access_token": "tok-r"}})
        )

        with patch.dict(os.environ, ENV_PATCH):
            token = get_fresh_token("conn-r1")

        assert token == "tok-r"
        assert "provider_config_key=prov-resolved" in str(detail.calls[0].request.url)

    @respx.mock
    def test_token_unknown_connection_raises(self):
        respx.get(f"{_BASE_URL}/connection").mock(
            return_value=Response(200, json={"connections": []})
        )
        with patch.dict(os.environ, ENV_PATCH):
            with pytest.raises(NangoTokenError):
                get_fresh_token("conn-ghost")


# ---------------------------------------------------------------------------
# poll_connection_health
# ---------------------------------------------------------------------------


class TestPollConnectionHealth:
    """Tests for poll_connection_health() -- AC6 5.5."""

    @respx.mock
    def test_poll_health_ok_for_fresh_credentials(self):
        """poll_connection_health returns 'ok' for a connection with fresh credentials."""
        respx.get(f"{_BASE_URL}/connection/conn-fresh").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {
                        "access_token": "tok",
                        "last_fetched_at": _fresh_timestamp(),
                    },
                    "connection_id": "conn-fresh",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            health = poll_connection_health("conn-fresh", provider="prov-test")

        assert health.status == "ok"
        assert health.last_fetched_at is not None

    @respx.mock
    def test_poll_health_stale_for_old_last_fetched(self):
        """poll_connection_health returns 'stale' when last_fetched_at > 24h ago."""
        respx.get(f"{_BASE_URL}/connection/conn-stale").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {
                        "access_token": "tok",
                        "last_fetched_at": _stale_timestamp(),
                    },
                    "connection_id": "conn-stale",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            health = poll_connection_health("conn-stale", provider="prov-test")

        assert health.status == "stale"
        assert health.last_fetched_at is not None

    @respx.mock
    def test_poll_health_revoked_for_404(self):
        """poll_connection_health returns 'revoked' when connection is not found (404)."""
        respx.get(f"{_BASE_URL}/connection/conn-gone").mock(
            return_value=Response(404, json={"error": "not found"})
        )

        with patch.dict(os.environ, ENV_PATCH):
            health = poll_connection_health("conn-gone", provider="prov-test")

        assert health.status == "revoked"
        assert health.last_fetched_at is None

    @respx.mock
    def test_poll_health_revoked_for_missing_credentials(self):
        """poll_connection_health returns 'revoked' when credentials are absent."""
        respx.get(f"{_BASE_URL}/connection/conn-nocreds").mock(
            return_value=Response(
                200,
                json={"connection_id": "conn-nocreds"},  # no credentials
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            health = poll_connection_health("conn-nocreds", provider="prov-test")

        assert health.status == "revoked"

    @respx.mock
    def test_poll_health_revoked_for_empty_access_token(self):
        """poll_connection_health returns 'revoked' when access_token is empty string."""
        respx.get(f"{_BASE_URL}/connection/conn-empty").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {"access_token": ""},
                    "connection_id": "conn-empty",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            health = poll_connection_health("conn-empty", provider="prov-test")

        assert health.status == "revoked"

    @respx.mock
    def test_poll_health_ok_no_last_fetched_at(self):
        """poll_connection_health returns 'ok' when credentials exist but no last_fetched_at."""
        respx.get(f"{_BASE_URL}/connection/conn-nots").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {
                        "access_token": "tok",
                        # no last_fetched_at -- treated as ok (benefit of the doubt)
                    },
                    "connection_id": "conn-nots",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            health = poll_connection_health("conn-nots", provider="prov-test")

        # Without a timestamp, we cannot determine staleness -- default to ok
        assert health.status == "ok"
        assert health.last_fetched_at is None


# ---------------------------------------------------------------------------
# OAuth1 health polling (H-1 fix coverage)
# ---------------------------------------------------------------------------


class TestOAuth1Health:
    """H-1 fix: poll_connection_health must not falsely revoke OAuth 1.0a connections.

    Nango returns oauth_token/oauth_token_secret for OAuth 1.0a providers (e.g. x-ads).
    The check must accept either OAuth2 (access_token) or OAuth1 (oauth_token) shape.
    """

    @respx.mock
    def test_poll_health_ok_for_oauth1_credentials(self):
        """OAuth1 shape (oauth_token + oauth_token_secret) → 'ok', not 'revoked'."""
        respx.get(f"{_BASE_URL}/connection/conn-oauth1").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {
                        "oauth_token": "tok-oauth1",
                        "oauth_token_secret": "secret-oauth1",
                        "last_fetched_at": _fresh_timestamp(),
                    },
                    "connection_id": "conn-oauth1",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            health = poll_connection_health("conn-oauth1", provider="x-ads")

        assert health.status == "ok", (
            "OAuth1 connection with valid oauth_token must not be reported as revoked"
        )
        assert health.last_fetched_at is not None

    @respx.mock
    def test_poll_health_ok_for_oauth1_token_only(self):
        """OAuth1 shape with only oauth_token (no secret) is still accepted as connected."""
        respx.get(f"{_BASE_URL}/connection/conn-oauth1-tok").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {
                        "oauth_token": "tok-only",
                    },
                    "connection_id": "conn-oauth1-tok",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            health = poll_connection_health("conn-oauth1-tok", provider="x-ads")

        assert health.status == "ok"

    @respx.mock
    def test_poll_health_revoked_for_empty_oauth1_credentials(self):
        """Empty oauth_token and no access_token still returns 'revoked'."""
        respx.get(f"{_BASE_URL}/connection/conn-oauth1-empty").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {
                        "oauth_token": "",
                        "oauth_token_secret": "",
                    },
                    "connection_id": "conn-oauth1-empty",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            health = poll_connection_health("conn-oauth1-empty", provider="x-ads")

        assert health.status == "revoked"

    @respx.mock
    def test_poll_health_oauth2_shape_still_works(self):
        """Existing OAuth2 (access_token) shape is unaffected by the H-1 fix."""
        respx.get(f"{_BASE_URL}/connection/conn-oauth2-compat").mock(
            return_value=Response(
                200,
                json={
                    "credentials": {
                        "access_token": "ya29.valid-token",
                        "last_fetched_at": _fresh_timestamp(),
                    },
                    "connection_id": "conn-oauth2-compat",
                },
            )
        )

        with patch.dict(os.environ, ENV_PATCH):
            health = poll_connection_health("conn-oauth2-compat", provider="google-ads")

        assert health.status == "ok"


# ---------------------------------------------------------------------------
# ConnectionHealth dataclass
# ---------------------------------------------------------------------------


class TestConnectionHealthDataclass:
    """Basic tests for the ConnectionHealth type."""

    def test_connection_health_fields(self):
        """ConnectionHealth has status and last_fetched_at fields."""
        h = ConnectionHealth(status="ok", last_fetched_at=None)
        assert h.status == "ok"
        assert h.last_fetched_at is None

    def test_nango_token_error_is_exception(self):
        """NangoTokenError is a proper Exception subclass."""
        err = NangoTokenError("test error")
        assert isinstance(err, Exception)
        assert str(err) == "test error"
