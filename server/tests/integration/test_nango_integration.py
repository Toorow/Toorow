"""Integration tests for core.nango_client -- Story 2.2, AC7.

These tests require a running Nango instance (docker-compose up) and
NANGO_INTEGRATION_TESTS=1 to be set. They skip automatically otherwise.

Run locally:
    docker compose -f infra/nango/docker-compose.yml up -d
    NANGO_INTEGRATION_TESTS=1 NANGO_BASE_URL=http://localhost:3003 \\
    NANGO_SECRET_KEY=<from-nango-ui> \\
    uv run pytest server/tests/integration/test_nango_integration.py -v

These tests do NOT depend on GOOGLE_CLIENT_ID or GOOGLE_CLIENT_SECRET --
they verify the Nango API surface, not the OAuth flow itself (AC7).
"""

from __future__ import annotations

import os

import httpx
import pytest
from core.nango_client import (
    ConnectionHealth,
    list_connections,
    poll_connection_health,
)

# ---------------------------------------------------------------------------
# Skip logic: skip all tests if Nango is not reachable or flag not set
# ---------------------------------------------------------------------------


def _nango_is_reachable() -> bool:
    """Return True if Nango's healthcheck endpoint responds."""
    base_url = os.environ.get("NANGO_BASE_URL", "http://localhost:3003")
    try:
        resp = httpx.get(f"{base_url}/healthcheck", timeout=3.0)
        return resp.status_code < 500
    except Exception:
        return False


def _integration_tests_enabled() -> bool:
    return os.environ.get("NANGO_INTEGRATION_TESTS", "") == "1"


def _skip_reason() -> str:
    if not _integration_tests_enabled():
        return "NANGO_INTEGRATION_TESTS != 1 -- set it to run integration tests"
    if not _nango_is_reachable():
        base_url = os.environ.get("NANGO_BASE_URL", "http://localhost:3003")
        return (
            f"Nango not reachable at {base_url} -- "
            "run: docker compose -f infra/nango/docker-compose.yml up -d"
        )
    return ""


def _should_skip() -> tuple[bool, str]:
    reason = _skip_reason()
    return bool(reason), reason


# Apply skip at collection time (not just at run time)
_skip, _reason = _should_skip()
pytestmark = pytest.mark.skipif(_skip, reason=_reason)


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestNangoIntegration:
    """Integration tests against a live Nango instance."""

    def test_list_connections_returns_list(self):
        """list_connections returns a list (empty is OK on a fresh instance).

        AC7: 'list_connections returns a valid (possibly empty) list without error'
        """
        result = list_connections()

        assert isinstance(result, list), (
            f"Expected list, got {type(result)}: {result}"
        )

        # Each item in the list must have the required fields
        for conn in result:
            assert "connection_id" in conn, f"Missing connection_id in: {conn}"
            assert "provider" in conn, f"Missing provider in: {conn}"

    def test_list_connections_with_nonexistent_provider(self):
        """list_connections with a nonexistent provider returns an empty list."""
        result = list_connections(provider="provider-that-does-not-exist-xyz")
        assert isinstance(result, list)
        assert result == []

    def test_poll_connection_health_nonexistent_connection(self):
        """poll_connection_health on a non-existent connection_id returns revoked.

        AC7: 'poll_connection_health on a non-existent connection_id: returns
        revoked status (or a documented error)'

        A non-existent connection returns 404 from Nango, which maps to revoked.
        """
        health = poll_connection_health("conn_this_does_not_exist_xyz_story_2_2")

        assert isinstance(health, ConnectionHealth)
        assert health.status == "revoked", (
            f"Expected 'revoked' for non-existent connection, got '{health.status}'"
        )
        assert health.last_fetched_at is None

    def test_nango_healthcheck_endpoint(self):
        """Nango healthcheck responds (sanity check for the integration test setup)."""
        base_url = os.environ.get("NANGO_BASE_URL", "http://localhost:3003")
        resp = httpx.get(f"{base_url}/healthcheck", timeout=5.0)
        assert resp.status_code < 500, (
            f"Nango healthcheck returned unexpected status {resp.status_code}"
        )
