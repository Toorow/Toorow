from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "dv360"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location("connector_dv360", MODULE_DIR / "connector.py")
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


def test_versions_are_independent_and_never_v3(connector):
    assert connector.DISCOVERY_API_VERSION == "v4"
    assert connector.REPORTING_API_VERSION == "v2"
    assert "/v4" in connector.DISCOVERY_BASE
    assert "/v2" in connector.REPORTING_BASE
    assert "v3" not in connector.DISCOVERY_BASE + connector.REPORTING_BASE


def test_discovery_preserves_partner_permissions_currency_and_timezone(connector):
    client = MagicMock()
    client.request.side_effect = [
        Response(
            payload={"partners": [{"partnerId": "p1", "entityStatus": "ENTITY_STATUS_ACTIVE"}]}
        ),
        Response(
            payload={
                "advertisers": [
                    {
                        "advertiserId": "a1",
                        "displayName": "Advertiser",
                        "entityStatus": "ENTITY_STATUS_ACTIVE",
                        "generalConfig": {"currencyCode": "EUR", "timeZone": "Europe/Paris"},
                    }
                ]
            }
        ),
    ]
    result = connector.discover_accounts("conn", _client=client, _token="secret")
    assert result[0]["partner_id"] == "p1"
    assert result[0]["advertiser_id"] == "a1"
    assert result[0]["currency"] == "EUR"
    assert result[0]["timezone"] == "Europe/Paris"
    assert result[0]["partner_role"] == "ENTITY_STATUS_ACTIVE"
    assert all(call.args[0] == "GET" for call in client.request.call_args_list)


def test_empty_discovery_is_actionable(connector):
    client = MagicMock()
    client.request.return_value = Response(payload={"partners": []})
    with pytest.raises(connector.Dv360OnboardingError, match="Read only role"):
        connector.discover_accounts("conn", _client=client, _token="secret")


def test_incompatible_explicit_shape_fails_before_provider_call(connector):
    with pytest.raises(connector.Dv360CompatibilityError, match="average_frequency"):
        connector.build_query_definition(
            "standard_daily",
            ["impressions", "average_frequency"],
            ["date", "advertiser_id"],
            "2026-07-01",
            "2026-07-02",
            "a1",
        )


def test_query_definition_uses_official_enums_and_google_dates(connector):
    definition = connector.build_query_definition(
        "standard_daily",
        ["impressions"],
        ["date", "advertiser_id"],
        "2026-07-01",
        "2026-07-02",
        "a1",
    )
    assert definition["params"]["metrics"] == ["METRIC_IMPRESSIONS"]
    assert definition["params"]["groupBys"] == ["FILTER_DATE", "FILTER_ADVERTISER"]
    assert definition["metadata"]["dataRange"]["customStartDate"] == {
        "year": 2026,
        "month": 7,
        "day": 1,
    }


def test_canonical_hash_ignores_title_and_is_stable(connector):
    definition = connector.build_query_definition(
        "standard_daily",
        ["impressions"],
        ["date", "advertiser_id"],
        "2026-07-01",
        "2026-07-02",
        "a1",
    )
    first = connector.canonical_query_hash(definition)
    definition["metadata"]["title"] = "provider title"
    assert connector.canonical_query_hash(definition) == first


def test_saved_query_is_reused_without_create(monkeypatch, connector):
    definition = connector.build_query_definition(
        "standard_daily",
        ["impressions"],
        ["date", "advertiser_id"],
        "2026-07-01",
        "2026-07-01",
        "a1",
    )
    title = f"toorow-{connector.canonical_query_hash(definition)[:32]}"
    monkeypatch.setattr(connector, "_charge_bid_manager", lambda project_id: None)
    client = MagicMock()
    client.request.return_value = Response(
        payload={"queries": [{"queryId": "q1", "metadata": {"title": title}}]}
    )
    assert connector.ensure_saved_query(client, "token", definition, "project") == "q1"
    assert client.request.call_count == 1
    assert client.request.call_args.args[0] == "GET"


def test_saved_query_search_paginates_before_create(monkeypatch, connector):
    definition = connector.build_query_definition(
        "standard_daily",
        ["impressions"],
        ["date", "advertiser_id"],
        "2026-07-01",
        "2026-07-01",
        "a1",
    )
    title = f"toorow-{connector.canonical_query_hash(definition)[:32]}"
    monkeypatch.setattr(connector, "_charge_bid_manager", lambda project_id: None)
    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"queries": [], "nextPageToken": "next"}),
        Response(payload={"queries": [{"queryId": "q1", "metadata": {"title": title}}]}),
    ]
    assert connector.ensure_saved_query(client, "token", definition, "project") == "q1"
    assert client.request.call_args_list[1].kwargs["params"] == {"pageToken": "next"}


def test_saved_query_is_created_once_when_absent(monkeypatch, connector):
    definition = connector.build_query_definition(
        "standard_daily",
        ["impressions"],
        ["date", "advertiser_id"],
        "2026-07-01",
        "2026-07-01",
        "a1",
    )
    charges = []
    monkeypatch.setattr(connector, "_charge_bid_manager", charges.append)
    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"queries": []}),
        Response(payload={"queryId": "q2"}),
    ]
    assert connector.ensure_saved_query(client, "token", definition, "project") == "q2"
    assert charges == ["project", "project"]
    assert [call.args[0] for call in client.request.call_args_list] == ["GET", "POST"]


@pytest.mark.parametrize(
    ("provider_state", "expected"),
    [("QUEUED", "pending"), ("RUNNING", "processing"), ("DONE", "completed"), ("FAILED", "failed")],
)
def test_async_state_mapping(monkeypatch, connector, provider_state, expected):
    monkeypatch.setattr(connector, "_charge_bid_manager", lambda project_id: None)
    client = MagicMock()
    client.request.return_value = Response(
        payload={
            "metadata": {
                "status": {"state": provider_state},
                "googleCloudStoragePath": "https://storage/report.csv",
            }
        }
    )
    flow = connector._BidManagerReportFlow(client, "token", "q1", "project")
    assert flow.poll("r1") == expected
    if provider_state == "DONE":
        assert flow.artifact_path == "https://storage/report.csv"


def test_async_resume_does_not_run_duplicate_report(monkeypatch, connector):
    from core.async_reports import InMemoryReportRefStore

    monkeypatch.setattr(connector, "_charge_bid_manager", lambda project_id: None)
    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"reportId": "r1"}),
        Response(payload={"metadata": {"status": {"state": "RUNNING"}}}),
        Response(payload={"metadata": {"status": {"state": "RUNNING"}}}),
        Response(
            payload={
                "metadata": {
                    "status": {"state": "DONE"},
                    "googleCloudStoragePath": "https://storage/report.csv",
                }
            }
        ),
        Response(text="date,advertiser_id,impressions\n2026-07-01,a1,10\n"),
    ]
    now = [0.0]
    store = InMemoryReportRefStore(clock=lambda: now[0])

    def sleep(seconds):
        now[0] += seconds

    definition = {"params": {"metrics": ["impressions"]}}
    first = connector.run_saved_query(
        client,
        "token",
        "q1",
        definition,
        "conn",
        "project",
        store=store,
        clock=lambda: now[0],
        sleeper=sleep,
        deadline_seconds=1,
    )
    second = connector.run_saved_query(
        client,
        "token",
        "q1",
        definition,
        "conn",
        "project",
        store=store,
        clock=lambda: now[0],
        sleeper=sleep,
        deadline_seconds=60,
    )
    assert first["status"] == "deferred"
    assert second["status"] == "completed"
    assert (
        len([call for call in client.request.call_args_list if call.args[1].endswith(":run")]) == 1
    )


def test_gcs_csv_parser_preserves_full_dimensions(connector):
    rows = connector._parse_artifact(
        "date,advertiser_id,campaign_id,impressions\n2026-07-01,a1,c1,10\n",
        ["impressions"],
        ["date", "advertiser_id", "campaign_id"],
    )
    assert rows[0]["dimensions"]["campaign_id"] == "c1"
    assert rows[0]["metrics"]["impressions"] == "10"


def test_micros_conversion_occurs_exactly_once(connector):
    assert connector._convert_metric("media_cost_micros", "14500000") == 14.5
    assert connector._convert_metric("cost", "14.5") == 14.5


def test_daily_bid_manager_quota_fails_closed(connector):
    now = connector.datetime(2026, 7, 1, tzinfo=connector.UTC)
    connector._bid_daily_usage[("project", "2026-07-01")] = connector.BID_MANAGER_PER_DAY_PROJECT
    with pytest.raises(Exception, match="daily budget exhausted"):
        connector._charge_bid_manager("project", now=now)


def test_bid_manager_four_qps_limit_fails_closed(connector):
    now = connector.datetime(2026, 7, 1, 12, 0, 0, tzinfo=connector.UTC)
    for _ in range(connector.BID_MANAGER_QPS_PROJECT):
        connector._charge_bid_manager("project", now=now)
    with pytest.raises(Exception, match="QPS budget exhausted"):
        connector._charge_bid_manager("project", now=now)


def test_v4_project_and_advertiser_domains_are_independent(connector):
    now = connector.datetime(2026, 7, 1, 12, 0, tzinfo=connector.UTC)
    minute = "2026-07-01T12:00"
    connector._v4_advertiser_minute_usage[("project", "a1", minute)] = (
        connector.V4_ADVERTISER_PER_MINUTE
    )
    with pytest.raises(Exception, match="advertiser minute budget exhausted"):
        connector._charge_v4("project", "a1", now=now)
    connector._v4_minute_usage[("other-project", minute)] = connector.V4_PROJECT_PER_MINUTE
    with pytest.raises(Exception, match="project minute budget exhausted"):
        connector._charge_v4("other-project", now=now)
    assert connector.V4_PROJECT_WRITES_PER_MINUTE == 700
    assert connector.V4_ADVERTISER_WRITES_PER_MINUTE == 150


def test_resource_exhausted_uses_shared_breaker(connector):
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as raised:
        connector._raise_response(
            Response(403, {"error": {"status": "RESOURCE_EXHAUSTED"}}), "dv360-bid-manager"
        )
    assert raised.value.platform == "dv360-bid-manager"


def test_non_additive_semantics_and_catalog_are_finished():
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text())
    catalog = json.loads((MODULE_DIR / "api_catalog.json").read_text())
    assert manifest["canonical_metric_mapping"]["unique_reach"]["non_additive"] is True
    assert (
        manifest["canonical_metric_mapping"]["viewability_rate"]["aggregation_rule"]
        == "impression_weighted"
    )
    assert len(catalog["fields"]) == 19
    assert not [field for field in catalog["fields"] if field["exposure"] == "planned"]
