from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "adobe-analytics"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_adobe_analytics", MODULE_DIR / "connector.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self.payload


def test_discovery_preserves_org_company_suite_timezone_and_api_key(connector):
    client = MagicMock()
    client.request.side_effect = [
        Response(
            payload={"imsOrgs": [{"imsOrgId": "org1", "companies": [{"globalCompanyId": "co1"}]}]}
        ),
        Response(
            payload={
                "content": [
                    {
                        "rsid": "suite1",
                        "name": "Main",
                        "timezoneZoneinfo": "Europe/Paris",
                        "currency": "EUR",
                    }
                ]
            }
        ),
    ]
    result = connector.discover_accounts(
        "conn", _client=client, _token="secret", _api_key_value="client-id"
    )
    assert result[0]["global_company_id"] == "co1"
    assert result[0]["rsid"] == "suite1"
    assert result[0]["timezone"] == "Europe/Paris"
    assert client.request.call_args_list[0].kwargs["headers"] == {
        "Authorization": "Bearer secret",
        "x-api-key": "client-id",
    }


def test_empty_discovery_is_actionable(connector):
    client = MagicMock()
    client.request.return_value = Response(payload={"imsOrgs": []})
    with pytest.raises(connector.AdobeAnalyticsOnboardingError, match="product profile"):
        connector.discover_accounts("conn", _client=client, _token="token", _api_key_value="key")


def test_dynamic_snapshot_is_rsid_scoped_sorted_and_zero_planned(connector):
    client = MagicMock()
    client.request.side_effect = [
        Response(
            payload={
                "content": [
                    {"id": "variables/evar2", "name": "B"},
                    {"id": "variables/evar1", "name": "A"},
                ]
            }
        ),
        Response(payload={"content": [{"id": "metrics/pageviews", "name": "Page Views"}]}),
        Response(payload={"content": [{"id": "cm1", "name": "Ratio"}]}),
        Response(payload={"content": [{"id": "s1", "name": "Customers"}]}),
    ]
    snapshot = connector.snapshot_catalog(
        "conn", "co1", "suite1", _client=client, _token="token", _api_key_value="key"
    )
    assert snapshot["rsid"] == "suite1"
    assert snapshot["planned"] == []
    assert [(item["kind"], item["id"]) for item in snapshot["components"]] == sorted(
        (item["kind"], item["id"]) for item in snapshot["components"]
    )
    assert all(call.kwargs["params"]["rsid"] == "suite1" for call in client.request.call_args_list)


def test_cross_suite_component_fails_before_reporting(connector):
    snapshot = {
        "rsid": "suite-a",
        "components": [
            {"kind": "metric", "id": "metrics/pageviews", "reportable": True},
            {"kind": "dimension", "id": "variables/evar1", "reportable": True},
        ],
    }
    with pytest.raises(connector.AdobeAnalyticsCompatibilityError, match="evar2"):
        connector.validate_components(snapshot, ["metrics/pageviews"], ["variables/evar2"])


def test_report_paginates_from_zero_without_mutating_request(connector):
    client = MagicMock()
    client.request.side_effect = [
        Response(
            payload={"rows": [{"itemId": "a", "data": [1]}], "totalPages": 2, "lastPage": False}
        ),
        Response(
            payload={"rows": [{"itemId": "b", "data": [2]}], "totalPages": 2, "lastPage": True}
        ),
    ]
    request = {"rsid": "suite1", "settings": {"page": 99}}
    result = connector.run_report(client, "token", "key", "co1", request)
    assert [row["page"] for row in result["rows"]] == [0, 1]
    assert request["settings"]["page"] == 99
    assert [call.kwargs["json"]["settings"]["page"] for call in client.request.call_args_list] == [
        0,
        1,
    ]
    assert all(call.kwargs["timeout"] == 60 for call in client.request.call_args_list)


def test_http_206_strict_fails_and_proven_mode_never_zero_fills(connector):
    payload = {
        "rows": [{"itemId": "a", "data": [7, None]}],
        "columnErrors": [{"columnId": "1", "errorCode": "unauthorized_metric"}],
        "lastPage": True,
    }
    strict_client = MagicMock()
    strict_client.request.return_value = Response(206, payload)
    with pytest.raises(connector.AdobeAnalyticsPartialResponseError):
        connector.run_report(strict_client, "t", "k", "co", {"settings": {}}, completeness="strict")
    proven_client = MagicMock()
    proven_client.request.return_value = Response(206, payload)
    result = connector.run_report(
        proven_client, "t", "k", "co", {"settings": {}}, completeness="proven_columns"
    )
    assert result["complete"] is False
    assert result["rows"][0]["data"] == [7, None]
    assert result["partial_errors"][0]["columnId"] == "1"


def test_request_supports_totals_segments_and_stable_breakdown_parent(connector):
    totals = connector.build_report_request("suite", ["metrics/visits"], "2026-07-01", "2026-07-02")
    assert "dimension" not in totals
    breakdown = connector.build_report_request(
        "suite",
        ["metrics/pageviews"],
        "2026-07-01",
        "2026-07-02",
        dimension="variables/evar1",
        segments=["s1"],
        parent_item_id="parent-9",
    )
    assert breakdown["rowContainer"]["rowId"] == "parent-9"
    assert {
        item.get("segmentId") for item in breakdown["globalFilters"] if item["type"] == "segment"
    } == {"s1"}


def test_quota_429050_uses_shared_breaker(connector):
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as raised:
        connector._raise_response(Response(429, {"error_code": "429050"}, {"Retry-After": "8"}))
    assert raised.value.retry_after == 8


def test_auth_401_routes_to_auth_expired(connector):
    """error_map entry '401:401013' must route to AuthExpiredError (HIGH-1)."""
    from core.pull_errors import AuthExpiredError

    with pytest.raises(AuthExpiredError):
        connector._raise_response(
            Response(401, {"error_code": "401013", "message": "token expired"})
        )


def test_non_additive_semantics_and_dynamic_manifest_contract():
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text())
    assert manifest["provider_api_version"] == "analytics-2.0"
    assert manifest["auth"]["default_flow"] == "oauth_server_to_server"
    assert manifest["source_capabilities"]["field_discovery"]["mode"] == "runtime"
    fields = {item["field_id"]: item for item in manifest["source_capabilities"]["fields"]}
    assert fields["visits"]["non_additive"] is True
    assert fields["bounce_rate"]["aggregation"] == "none"
    assert all(
        report["availability"]["status"] == "selectable"
        for report in manifest["source_capabilities"]["reports"]
    )


def test_hash_is_canonical(connector):
    assert connector.canonical_request_hash({"b": 2, "a": 1}) == connector.canonical_request_hash(
        {"a": 1, "b": 2}
    )
