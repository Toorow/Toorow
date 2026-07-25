from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "cm360"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location("connector_cm360", MODULE_DIR / "connector.py")
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


def test_discovery_preserves_profile_account_subaccount_and_advertiser(connector):
    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"items": [{"profileId": "p1", "accountId": "a1", "subAccountId": "s1"}]}),
        Response(payload={"advertisers": [{"id": "adv1", "name": "Advertiser"}]}),
    ]
    result = connector.discover_accounts("conn", _client=client, _token="secret")
    assert result[0] | {} == {
        "id": "cm360_selection_1",
        "profile_id": "p1",
        "account_id": "a1",
        "subaccount_id": "s1",
        "advertiser_id": "adv1",
        "display_name": "Advertiser",
    }


def test_empty_discovery_is_actionable(connector):
    client = MagicMock()
    client.request.return_value = Response(payload={"items": []})
    with pytest.raises(connector.Cm360OnboardingError):
        connector.discover_accounts("conn", _client=client, _token="secret")


def test_explicit_incompatible_fields_fail_before_provider_call(connector):
    with pytest.raises(connector.Cm360CompatibilityError, match="average_frequency"):
        connector.build_query_request(
            "STANDARD",
            ["impressions", "average_frequency"],
            ["date"],
            "2026-07-01",
            "2026-07-02",
            "adv1",
        )


def test_query_paginates_without_duplicate_request_shape_mutation(connector, monkeypatch):
    charged = []
    monkeypatch.setattr(connector, "_consume_query_quota", charged.append)
    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"rows": [{"id": 1}], "nextPageToken": "next"}),
        Response(payload={"rows": [{"id": 2}]}),
    ]
    request = {"metricNames": ["impressions"]}
    rows = connector.query_report_data(client, "token", "p1", request)
    assert rows == [{"id": 1}, {"id": 2}]
    assert "pageToken" not in request
    assert client.request.call_count == 2
    assert client.request.call_args_list[1].kwargs["json"]["pageToken"] == "next"
    assert all(call.kwargs["timeout"] == 60 for call in client.request.call_args_list)
    assert charged == ["onboarding", "onboarding"]


def test_saved_report_statuses_and_hash_are_stable(connector):
    request_a = {"b": 2, "a": 1}
    request_b = {"a": 1, "b": 2}
    assert connector.canonical_request_hash(request_a) == connector.canonical_request_hash(
        request_b
    )
    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"id": "file1"}),
        Response(payload={"status": "PROCESSING"}),
        Response(payload={"status": "REPORT_AVAILABLE"}),
    ]
    flow = connector._SavedReportFlow(client, "token", "p1", "r1")
    assert flow.submit() == "file1"
    assert flow.poll("file1") == "processing"
    assert flow.poll("file1") == "completed"


def test_saved_report_resume_does_not_create_duplicate_report(connector):
    from core.async_reports import InMemoryReportRefStore

    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"id": "file1"}),
        Response(payload={"status": "PROCESSING"}),
        Response(payload={"status": "PROCESSING"}),
        Response(payload={"status": "REPORT_AVAILABLE"}),
        Response(
            payload={"rows": [{"metrics": {"impressions": "1"}}]},
            headers={"Content-Type": "application/json"},
        ),
    ]
    now = [0.0]

    def clock():
        return now[0]

    def sleeper(seconds):
        now[0] += seconds

    store = InMemoryReportRefStore()
    first = connector.run_saved_report(
        client,
        "token",
        "p1",
        "r1",
        {"shape": 1},
        "conn",
        store=store,
        clock=clock,
        sleeper=sleeper,
        deadline_seconds=1,
    )
    assert first["status"] == "deferred"
    second = connector.run_saved_report(
        client,
        "token",
        "p1",
        "r1",
        {"shape": 1},
        "conn",
        store=store,
        clock=clock,
        sleeper=sleeper,
        deadline_seconds=60,
    )
    assert second["status"] == "completed"
    run_calls = [call for call in client.request.call_args_list if call.args[1].endswith("/run")]
    assert len(run_calls) == 1


@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [("FAILED", "failed"), ("CANCELLED", "failed"), ("EXPIRED", "expired"), ("NEW", "pending")],
)
def test_saved_report_terminal_mapping(connector, provider_status, expected):
    client = MagicMock()
    client.request.return_value = Response(payload={"status": provider_status})
    assert connector._SavedReportFlow(client, "t", "p", "r").poll("f") == expected


def test_google_reason_normalization_preserves_original_payload(connector):
    response = Response(
        403,
        {
            "error": {
                "status": "PERMISSION_DENIED",
                "errors": [{"reason": "insufficientPermissions"}],
            }
        },
    )
    with pytest.raises(Exception) as raised:
        connector._raise_response(response)
    assert raised.value.error_class == "permission_denied"
    assert raised.value.provider_payload["code"] == "insufficientPermissions"
    assert raised.value.provider_payload["error"]["status"] == "PERMISSION_DENIED"


def test_429_uses_shared_breaker(connector):
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as raised:
        connector._raise_response(Response(429, {}, {"Retry-After": "7"}))
    assert raised.value.retry_after == 7


def test_catalog_is_zero_planned_and_version_coupled():
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text())
    catalog = json.loads((MODULE_DIR / "api_catalog.json").read_text())
    report = json.loads((MODULE_DIR / "catalog_sources" / "fusion-report.json").read_text())
    assert manifest["provider_api_version"] == catalog["api_version"] == "dfareporting-v5"
    assert len(catalog["fields"]) == 14
    assert all(field["exposure"] in {"exposed", "excluded"} for field in catalog["fields"])
    assert not [field for field in catalog["fields"] if field["exposure"] == "planned"]
    assert report["drift_ids"] == []


def test_reach_and_frequency_are_declared_non_additive():
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text())
    mappings = manifest["canonical_metric_mapping"]
    assert mappings["unique_reach"]["non_additive"] is True
    assert mappings["average_frequency"]["aggregation_rule"] == "impression_weighted"
    fields = {item["field_id"]: item for item in manifest["source_capabilities"]["fields"]}
    assert fields["unique_reach"]["aggregation"] == "max"
    assert fields["average_frequency"]["non_additive"] is True


def test_saved_report_csv_is_normalized_to_query_row_contract(connector):
    request = {
        "metricNames": ["impressions", "clicks"],
        "dimensions": [{"name": "date"}, {"name": "campaign_id"}],
    }
    rows = connector._normalize_saved_report_rows(
        "date,campaign_id,impressions,clicks\n2026-07-01,c1,10,2\n", request
    )
    assert rows == [
        {
            "dimensions": {"date": "2026-07-01", "campaign_id": "c1"},
            "metrics": {"impressions": "10", "clicks": "2"},
        }
    ]


def test_saved_report_selection_uses_async_path_and_lands(monkeypatch, connector):
    monkeypatch.setattr(
        connector,
        "run_saved_report",
        lambda *args, **kwargs: {
            "status": "completed",
            "rows": "date,campaign_id,impressions\n2026-07-01,c1,10\n",
        },
    )
    landed = {}
    monkeypatch.setattr(
        connector,
        "_land",
        lambda rows, context: landed.update(rows=rows, context=context) or 1,
    )
    result = connector._pull_profile(
        "conn",
        "2026-07-01",
        "2026-07-01",
        "project",
        "pull",
        "standard_daily",
        {
            "profile_id": "p1",
            "advertiser_id": "adv1",
            "saved_report_id": "report1",
        },
        _client=MagicMock(),
        _token="token",
    )
    assert result["row_count"] == 1
    assert landed["rows"][0]["metrics"]["impressions"] == "10"


def test_profile_dispatch_uses_queue_signature(monkeypatch, connector):
    calls = []
    monkeypatch.setattr(
        connector,
        "_pull_profile",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {"row_count": 0},
    )
    selection = {"profile_id": "p", "advertiser_id": "a"}
    connector.pull_standard_daily("c", "from", "to", "project", "pull", selection)
    connector.pull_floodlight_daily("c", "from", "to", "project", "pull", selection)
    connector.pull_reach("c", "from", "to", "project", "pull", selection)
    assert [call[0][5] for call in calls] == [
        "standard_daily",
        "floodlight_daily",
        "reach",
    ]
    assert all(call[0][6] is selection for call in calls)


def test_daily_project_quota_fails_closed(connector):
    now = connector.datetime(2026, 7, 1, tzinfo=connector.UTC)
    connector._daily_query_usage[("project", "2026-07-01")] = connector.QUERY_DAILY_PROJECT_LIMIT
    with pytest.raises(Exception, match="daily project query budget exhausted"):
        connector._consume_query_quota("project", now=now)


def test_declared_cm360_query_quota_boundaries(connector):
    assert connector.QUERY_PER_MINUTE_USER_LIMIT == 120
    assert connector.QUERY_DAILY_PROJECT_LIMIT == 10_000


def test_provider_sentinel_is_preserved_by_transform(connector):
    row = {"date": "2026-07-01", "placement_id": "(not set)", "impressions": "5"}
    assert connector.transform([row])[0]["placement_id"] == "(not set)"
