"""Tests for the Integral Ad Science (IAS) connector.

Uses respx to mock the IAS Reporting API. No test contacts the real API; the
live contract is verified by the human-gated ratification probe (AI-13) once an
IAS Signal account connects (see server/modules/ias/ROLLOUT_NOTES.md).
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_IAS_PATH = Path(__file__).parents[4] / "server" / "modules" / "ias" / "connector.py"

# The connector builds:
# {base}/reportingservice/api/teams/{team}/platform/{plat}/campaigns/{ids}/report
_IAS_REPORT_URL = (
    "https://api.integralplatform.com/reportingservice/api/teams/T-42"
    "/platform/CM/campaigns/all/report"
)
_IAS_TEAMS_URL = "https://api.integralplatform.com/reportingservice/api/teams"

_IAS_REPORT_RESPONSE = {
    "rows": [
        {
            "date": "2026-07-01",
            "campaignId": "IAS-CMP-1001",
            "campaignName": "Q3 Brand Awareness",
            "measuredImps": "482000",
            "viewableImps": "351860",
            "eligibleImps": "500000",
            "viewableRate": "0.73",
        },
        {
            "date": "2026-07-01",
            "campaignId": "IAS-CMP-1002",
            "campaignName": "Q3 Performance Prospecting",
            "measuredImps": "120400",
            "viewableImps": "78260",
            "eligibleImps": "130000",
            "viewableRate": "0.65",
        },
    ]
}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_ias", _IAS_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# transform() — camelCase source tokens -> canonical field_ids; ratios dropped.
# ---------------------------------------------------------------------------


def test_transform_renames_and_drops_ratios(connector):
    raw = [
        {
            "date": "2026-07-01",
            "campaignId": "IAS-CMP-1",
            "campaignName": "Brand",
            "measuredImps": 100,
            "viewableImps": 70,
            "eligibleImps": 120,
            "invalidImps": 3,
            "passedImps": 95,
            "failedImps": 5,
            "viewableRate": 0.7,
        }
    ]
    out = connector.transform(raw)[0]
    assert out["measured_impressions"] == 100
    assert out["viewable_impressions"] == 70
    assert out["eligible_impressions"] == 120
    assert out["invalid_traffic_ads"] == 3
    assert out["brand_safety_passed_ads"] == 95
    assert out["brand_safety_failed_ads"] == 5
    assert out["campaign_id"] == "IAS-CMP-1"
    assert out["campaign_name"] == "Brand"
    assert out["date"] == "2026-07-01"
    # ratio metric never stored (AD-4)
    assert "viewableRate" not in out
    assert "viewability_rate" not in out


# ---------------------------------------------------------------------------
# pull() — request shape, landing, auth header, token safety.
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_calls_report_endpoint_with_metrics(connector, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ias.duckdb"))

    route = respx.get(_IAS_REPORT_URL).mock(
        return_value=httpx.Response(200, json=_IAS_REPORT_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_viewability_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-ias",
            pull_id="pull_ias_p1",
            team_id="T-42",
        )

    assert route.called
    req = route.calls.last.request
    metrics_param = req.url.params.get("metrics", "")
    assert "measuredImps" in metrics_param
    assert "viewableImps" in metrics_param
    assert req.url.params.get("startDate") == "2026-07-01"
    assert req.url.params.get("endDate") == "2026-07-01"
    assert result["row_count"] == 2
    assert result["pull_id"] == "pull_ias_p1"


@respx.mock
def test_pull_authorization_header_used(connector, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ias_auth.duckdb"))

    captured: list[dict] = []

    def _capture(request: httpx.Request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_IAS_REPORT_RESPONSE)

    respx.get(_IAS_REPORT_URL).mock(side_effect=_capture)

    with patch("core.nango_client.get_fresh_token", return_value="tok-abc"):
        connector.pull_viewability_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-ias",
            pull_id="pull_ias_auth",
            team_id="T-42",
        )

    assert captured[0].get("authorization") == "Bearer tok-abc"


@respx.mock
def test_pull_token_not_stored(connector, tmp_path, monkeypatch, caplog):
    """AD-3: the token must not appear in the return value or any log."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ias_tok.duckdb"))

    respx.get(_IAS_REPORT_URL).mock(
        return_value=httpx.Response(200, json=_IAS_REPORT_RESPONSE)
    )

    secret = "super-secret-ias-token-98765"
    with caplog.at_level(logging.DEBUG):
        with patch("core.nango_client.get_fresh_token", return_value=secret):
            result = connector.pull_viewability_daily(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-ias",
                pull_id="pull_ias_tok",
                team_id="T-42",
            )

    assert secret not in str(result)
    for record in caplog.records:
        assert secret not in record.getMessage()


def test_pull_missing_team_id_raises(connector, monkeypatch):
    monkeypatch.delenv("IAS_TEAM_ID", raising=False)
    with pytest.raises(ValueError, match="IAS team id"):
        connector.pull_viewability_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-ias",
            pull_id="pull_ias_noteam",
            team_id=None,
        )


# ---------------------------------------------------------------------------
# Error taxonomy (Story 25.2).
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_401_raises_auth_expired_with_payload_preserved(connector, tmp_path, monkeypatch):
    """A 401 (OAuth invalid_token / expired) routes to auth_expired, payload preserved."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ias_401.duckdb"))

    oauth_error = {"error": "invalid_token", "error_description": "Access token expired"}
    respx.get(_IAS_REPORT_URL).mock(return_value=httpx.Response(401, json=oauth_error))

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull_viewability_daily(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-ias",
                pull_id="pull_ias_401",
                team_id="T-42",
            )

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    assert err.provider_payload["error"] == "invalid_token"


@respx.mock
def test_pull_403_raises_permission_denied(connector, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ias_403.duckdb"))

    respx.get(_IAS_REPORT_URL).mock(
        return_value=httpx.Response(403, json={"error": "access_denied"})
    )

    from core.pull_errors import PermissionDeniedError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(PermissionDeniedError) as exc_info:
            connector.pull_viewability_daily(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-ias",
                pull_id="pull_ias_403",
                team_id="T-42",
            )
    assert exc_info.value.error_class == "permission_denied"


@respx.mock
def test_pull_raises_rate_limit_error_on_429(connector, tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ias_429.duckdb"))

    respx.get(_IAS_REPORT_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"}, json={})
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull_viewability_daily(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-ias",
                pull_id="pull_ias_429",
                team_id="T-42",
            )

    assert exc_info.value.platform == "ias"
    assert exc_info.value.retry_after == 30


# ---------------------------------------------------------------------------
# Topology discovery.
# ---------------------------------------------------------------------------


@respx.mock
def test_discover_accounts_lists_teams(connector):
    respx.get(_IAS_TEAMS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "teams": [
                    {"id": "T-42", "name": "Acme Media"},
                    {"id": "T-99", "name": ""},
                ]
            },
        )
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        accounts = connector.discover_accounts("conn_ias_1")

    assert accounts == [
        {"id": "T-42", "label": "Acme Media"},
        {"id": "T-99", "label": "T-99"},  # empty name falls back to id
    ]


@respx.mock
def test_discover_accounts_401_raises_auth_expired(connector):
    respx.get(_IAS_TEAMS_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_token"})
    )

    from core.pull_errors import ConnectorError

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(ConnectorError) as exc_info:
            connector.discover_accounts("conn_ias_1")

    assert exc_info.value.error_class == "auth_expired"


# ---------------------------------------------------------------------------
# error_map contract: IAS declares no numeric codes -> empty map + note.
# ---------------------------------------------------------------------------


def test_manifest_error_map_empty_with_note():
    manifest = json.loads((_IAS_PATH.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("error_map") == {}
    assert manifest.get("_error_map_note"), "empty error_map must carry a justifying note"


# ---------------------------------------------------------------------------
# Real IAS E2E — human gate (AI-13). Skipped until an IAS account connects.
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Real IAS Signal E2E — human gate per AI-13 (no test account)")
def test_pull_e2e_with_real_ias():  # pragma: no cover
    """Placeholder for the live IAS Reporting API smoke test."""
