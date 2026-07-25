"""Unit + integration tests for core.airbyte_client and extraction_path routing.

Story 3.1, AC6, AC7 -- all unit tests use respx mocks (no live Airbyte needed).

Integration test (test_live_trigger_sync_integration) is guarded with
@airbyte_available and skips when no local Airbyte is reachable.

See infra/airbyte/SETUP.md for local Airbyte setup instructions.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from core.airbyte_client import AirbyteSyncError, get_sync_status, trigger_sync

# ---------------------------------------------------------------------------
# Skip-if-unreachable guard for integration tests (AC6, HG-2)
# ---------------------------------------------------------------------------

AIRBYTE_BASE_URL = os.environ.get("AIRBYTE_BASE_URL", "http://localhost:8000")


def _airbyte_reachable(url: str) -> bool:
    """Return True if local Airbyte health endpoint is reachable."""
    try:
        httpx.get(url + "/api/v1/health", timeout=3).raise_for_status()
        return True
    except Exception:
        return False


airbyte_available = pytest.mark.skipif(
    not _airbyte_reachable(AIRBYTE_BASE_URL),
    reason="Local Airbyte not running -- set AIRBYTE_BASE_URL or run abctl",
)

# ---------------------------------------------------------------------------
# Env-var patch for unit tests
# ---------------------------------------------------------------------------

_BASE_URL = "http://airbyte-test.local"
_ENV_PATCH = {"AIRBYTE_BASE_URL": _BASE_URL, "AIRBYTE_API_KEY": ""}


# ---------------------------------------------------------------------------
# T7.1 -- trigger_sync returns job_id on success
# ---------------------------------------------------------------------------


@respx.mock
def test_trigger_sync_returns_job_id():
    """trigger_sync: mocked 200 response returns job_id, status, and pull_id."""
    respx.post(f"{_BASE_URL}/api/v1/connections/sync").mock(
        return_value=httpx.Response(
            200, json={"job": {"id": 123, "status": "pending"}}
        )
    )

    with patch.dict(os.environ, _ENV_PATCH):
        result = trigger_sync("conn-abc-123")

    assert result["job_id"] == "123"
    assert result["status"] == "pending"
    # pull_id defaults to None when not supplied
    assert result["pull_id"] is None


@respx.mock
def test_trigger_sync_threads_pull_id():
    """trigger_sync: pull_id is included in payload and echoed in the result."""
    route = respx.post(f"{_BASE_URL}/api/v1/connections/sync").mock(
        return_value=httpx.Response(
            200, json={"job": {"id": 456, "status": "pending"}}
        )
    )

    with patch.dict(os.environ, _ENV_PATCH):
        result = trigger_sync("conn-abc-999", pull_id="pull_TESTULID123")

    assert result["pull_id"] == "pull_TESTULID123"
    assert result["job_id"] == "456"
    # Verify the pull_id was sent in the request payload
    sent_body = route.calls[0].request.content
    import json
    payload = json.loads(sent_body)
    assert payload.get("connectorMetadata", {}).get("pull_id") == "pull_TESTULID123"


# ---------------------------------------------------------------------------
# T7.2 -- trigger_sync raises AirbyteSyncError on non-200
# ---------------------------------------------------------------------------


@respx.mock
def test_trigger_sync_raises_on_error():
    """trigger_sync: 500 response raises AirbyteSyncError."""
    respx.post(f"{_BASE_URL}/api/v1/connections/sync").mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    with patch.dict(os.environ, _ENV_PATCH):
        with pytest.raises(AirbyteSyncError) as exc_info:
            trigger_sync("conn-abc-123")

    assert "500" in str(exc_info.value)


@respx.mock
def test_trigger_sync_raises_on_403():
    """trigger_sync: 403 response raises AirbyteSyncError."""
    respx.post(f"{_BASE_URL}/api/v1/connections/sync").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )

    with patch.dict(os.environ, _ENV_PATCH):
        with pytest.raises(AirbyteSyncError) as exc_info:
            trigger_sync("conn-abc-123")

    assert "403" in str(exc_info.value)


# ---------------------------------------------------------------------------
# T7.3 -- get_sync_status returns dict with status field
# ---------------------------------------------------------------------------


@respx.mock
def test_get_sync_status_returns_dict():
    """get_sync_status: mocked 200 response returns dict with status and job_id."""
    respx.get(f"{_BASE_URL}/api/v1/jobs/123").mock(
        return_value=httpx.Response(
            200,
            json={
                "job": {
                    "id": 123,
                    "status": "succeeded",
                    "createdAt": "2026-07-10T00:00:00Z",
                    "endedAt": "2026-07-10T00:05:00Z",
                }
            },
        )
    )

    with patch.dict(os.environ, _ENV_PATCH):
        result = get_sync_status("123")

    assert "status" in result
    assert result["status"] == "succeeded"
    assert result["job_id"] == "123"
    assert result["created_at"] == "2026-07-10T00:00:00Z"
    assert result["ended_at"] == "2026-07-10T00:05:00Z"


@respx.mock
def test_get_sync_status_raises_on_error():
    """get_sync_status: 500 response raises AirbyteSyncError."""
    respx.get(f"{_BASE_URL}/api/v1/jobs/999").mock(
        return_value=httpx.Response(500, text="Server Error")
    )

    with patch.dict(os.environ, _ENV_PATCH):
        with pytest.raises(AirbyteSyncError) as exc_info:
            get_sync_status("999")

    assert "500" in str(exc_info.value)


# ---------------------------------------------------------------------------
# T7.4 -- extraction_path routing: "custom_pull" calls module pull()
# ---------------------------------------------------------------------------


def test_extraction_path_routing_custom_pull():
    """dispatch_pull: extraction_path='custom_pull' calls module pull() function."""
    from core.loader import dispatch_pull

    mock_pull = MagicMock(return_value={"row_count": 5, "status": "done"})
    mock_connector = SimpleNamespace(pull=mock_pull)
    mock_manifest = {
        "report_profiles": [
            {"id": "standard_daily", "extraction_path": "custom_pull"}
        ]
    }
    loaded = SimpleNamespace(
        name="google-analytics",
        manifest=mock_manifest,
        connector_module=mock_connector,
    )

    result = dispatch_pull(
        module_name="google-analytics",
        profile_id="standard_daily",
        loaded_modules=[loaded],
        connection_id="nango-conn-1",
        date_from="2026-01-01",
        date_to="2026-01-01",
        project_id="proj-1",
        pull_id="pull_ABC",
    )

    mock_pull.assert_called_once_with(
        connection_id="nango-conn-1",
        date_from="2026-01-01",
        date_to="2026-01-01",
        project_id="proj-1",
        pull_id="pull_ABC",
    )
    assert result["row_count"] == 5


def test_extraction_path_routing_absent_defaults_to_custom_pull():
    """dispatch_pull: absent extraction_path defaults to custom_pull."""
    from core.loader import dispatch_pull

    mock_pull = MagicMock(return_value={"row_count": 3, "status": "done"})
    mock_connector = SimpleNamespace(pull=mock_pull)
    # No extraction_path field in this profile
    mock_manifest = {
        "report_profiles": [
            {"id": "standard_daily"}
        ]
    }
    loaded = SimpleNamespace(
        name="google-analytics",
        manifest=mock_manifest,
        connector_module=mock_connector,
    )

    result = dispatch_pull(
        module_name="google-analytics",
        profile_id="standard_daily",
        loaded_modules=[loaded],
        connection_id="nango-conn-2",
        date_from="2026-01-02",
        date_to="2026-01-02",
        project_id="proj-2",
        pull_id="pull_DEF",
    )

    mock_pull.assert_called_once()
    assert result["row_count"] == 3


# ---------------------------------------------------------------------------
# T7.5 -- extraction_path routing: "airbyte_sync" does NOT call pull()
# ---------------------------------------------------------------------------


@respx.mock
def test_extraction_path_routing_airbyte_sync():
    """dispatch_pull: extraction_path='airbyte_sync' triggers AirbyteClient, not pull()."""
    from core.loader import dispatch_pull

    # Mock the Airbyte API endpoint
    respx.post(f"{_BASE_URL}/api/v1/connections/sync").mock(
        return_value=httpx.Response(
            200, json={"job": {"id": 456, "status": "pending"}}
        )
    )

    mock_pull = MagicMock()
    mock_connector = SimpleNamespace(pull=mock_pull)
    mock_manifest = {
        "report_profiles": [
            {"id": "standard_daily", "extraction_path": "airbyte_sync"}
        ]
    }
    loaded = SimpleNamespace(
        name="google-analytics",
        manifest=mock_manifest,
        connector_module=mock_connector,
    )

    with patch.dict(os.environ, _ENV_PATCH):
        result = dispatch_pull(
            module_name="google-analytics",
            profile_id="standard_daily",
            loaded_modules=[loaded],
            connection_id="nango-conn-3",
            airbyte_connection_id="airbyte-conn-uuid",
            date_from="2026-01-03",
            date_to="2026-01-03",
            project_id="proj-3",
            pull_id="pull_GHI",
        )

    # pull() must NOT have been called
    mock_pull.assert_not_called()

    # Result must be the dispatched stub with airbyte_<job_id> pull_id surrogate
    assert result["status"] == "dispatched"
    assert result["pull_id"] == "airbyte_456"
    assert result["row_count"] is None


def test_extraction_path_routing_airbyte_sync_missing_connection_id():
    """dispatch_pull: airbyte_sync without airbyte_connection_id raises ValueError."""
    from core.loader import dispatch_pull

    mock_connector = SimpleNamespace(pull=MagicMock())
    mock_manifest = {
        "report_profiles": [
            {"id": "standard_daily", "extraction_path": "airbyte_sync"}
        ]
    }
    loaded = SimpleNamespace(
        name="google-analytics",
        manifest=mock_manifest,
        connector_module=mock_connector,
    )

    with pytest.raises(ValueError, match="airbyte_connection_id"):
        dispatch_pull(
            module_name="google-analytics",
            profile_id="standard_daily",
            loaded_modules=[loaded],
            connection_id="nango-conn-4",
            # airbyte_connection_id intentionally omitted (defaults to None)
            date_from="2026-01-04",
            date_to="2026-01-04",
            project_id="proj-4",
            pull_id="pull_JKL",
        )


# ---------------------------------------------------------------------------
# Helper: _get_extraction_path unit tests
# ---------------------------------------------------------------------------


def test_get_extraction_path_returns_value_when_present():
    """_get_extraction_path: returns extraction_path when present in profile."""
    from core.loader import _get_extraction_path

    manifest = {
        "report_profiles": [
            {"id": "standard_daily", "extraction_path": "airbyte_sync"},
            {"id": "other", "extraction_path": "custom_pull"},
        ]
    }
    assert _get_extraction_path(manifest, "standard_daily") == "airbyte_sync"
    assert _get_extraction_path(manifest, "other") == "custom_pull"


def test_get_extraction_path_defaults_to_custom_pull():
    """_get_extraction_path: returns 'custom_pull' when field is absent."""
    from core.loader import _get_extraction_path

    manifest = {
        "report_profiles": [
            {"id": "standard_daily"}
        ]
    }
    assert _get_extraction_path(manifest, "standard_daily") == "custom_pull"


def test_get_extraction_path_unknown_profile_defaults():
    """_get_extraction_path: unknown profile ID defaults to 'custom_pull'."""
    from core.loader import _get_extraction_path

    manifest = {
        "report_profiles": [
            {"id": "standard_daily", "extraction_path": "airbyte_sync"}
        ]
    }
    assert _get_extraction_path(manifest, "nonexistent") == "custom_pull"


# ---------------------------------------------------------------------------
# T7.6 -- Integration test (guarded -- skipped unless local Airbyte is running)
# ---------------------------------------------------------------------------


@airbyte_available
def test_live_trigger_sync_integration():
    """Live integration test: trigger a real sync against local Airbyte.

    BLOCKED: Requires local Airbyte running (abctl or docker-compose).
    Skipped automatically when AIRBYTE_BASE_URL is unreachable.
    See infra/airbyte/SETUP.md for bring-up instructions.

    AI-13: Live integration pass to be completed when local Airbyte is available.
    This test will run automatically once Airbyte is reachable.
    """
    # This test requires a real Airbyte connection ID to be configured.
    # Set AIRBYTE_TEST_CONNECTION_ID env var to a valid Airbyte connection UUID.
    test_connection_id = os.environ.get("AIRBYTE_TEST_CONNECTION_ID", "")
    if not test_connection_id:
        pytest.skip(
            "Set AIRBYTE_TEST_CONNECTION_ID env var to a valid Airbyte connection UUID"
        )

    result = trigger_sync(test_connection_id)
    assert "job_id" in result
    assert result["job_id"]
    assert result.get("status") in ("pending", "running", "succeeded", "failed")
