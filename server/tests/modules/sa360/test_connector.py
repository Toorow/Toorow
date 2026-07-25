from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "sa360"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location("connector_sa360", MODULE_DIR / "connector.py")
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


def test_discovery_and_manager_route_are_explicit(connector):
    client = MagicMock()
    client.request.return_value = Response(payload={"resourceNames": ["customers/123-456"]})
    selection = connector.discover_accounts("c", _client=client, _token="t")[0]
    assert selection["customer_id"] == "123456"
    assert selection["routing_mode"] == "direct"
    routed = connector.with_manager_route(selection, "999-888")
    assert routed["login_customer_id"] == "999888"
    assert routed["routing_mode"] == "manager_client"


def test_empty_discovery_is_actionable(connector):
    client = MagicMock()
    client.request.return_value = Response(payload={"resourceNames": []})
    with pytest.raises(connector.Sa360OnboardingError):
        connector.discover_accounts("c", _client=client, _token="t")


def test_saql_is_finite_deterministic_and_resource_aware(connector):
    fields = ["segments.date", "campaign.id", "metrics.impressions"]
    query = connector.build_saql("campaign_daily", fields, "2026-07-01", "2026-07-02")
    assert query == connector.build_saql("campaign_daily", fields, "2026-07-01", "2026-07-02")
    assert "FROM campaign" in query
    assert "segments.date BETWEEN '2026-07-01' AND '2026-07-02'" in query
    assert len(connector.query_hash(query)) == 64
    with pytest.raises(connector.Sa360CompatibilityError):
        connector.build_saql("campaign_daily", ["adGroup.id"], "2026-07-01", "2026-07-02")


def test_page_and_in_limits_fail_before_provider(connector):
    client = MagicMock()
    with pytest.raises(connector.Sa360CompatibilityError, match="page size"):
        connector.search(client, "t", "1", "SELECT customer.id FROM customer", "p", page_size=10001)
    with pytest.raises(connector.Sa360CompatibilityError, match="IN list"):
        connector.validate_in_values([str(i) for i in range(20001)])
    assert client.request.call_count == 0


def test_search_pages_without_recharging_followups(monkeypatch, connector):
    charges = []
    monkeypatch.setattr(connector, "_charge_query", charges.append)
    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"results": [{"id": 1}], "nextPageToken": "next"}),
        Response(payload={"results": [{"id": 2}]}, headers={"request-id": "rid"}),
    ]
    rows, request_id = connector.search(client, "t", "1", "q", "project")
    assert rows == [{"id": 1}, {"id": 2}]
    assert request_id == "rid"
    assert charges == ["project"]
    assert client.request.call_args_list[1].kwargs["json"]["pageToken"] == "next"


def test_search_stream_flattens_batches(monkeypatch, connector):
    monkeypatch.setattr(connector, "_charge_query", lambda project_id: None)
    client = MagicMock()
    client.request.return_value = Response(
        payload=[{"results": [{"id": 1}]}, {"results": [{"id": 2}]}],
        headers={"request-id": "stream-rid"},
    )
    rows, request_id = connector.search_stream(client, "t", "1", "q", "project")
    assert rows == [{"id": 1}, {"id": 2}]
    assert request_id == "stream-rid"


def test_login_customer_header_has_digits_only(connector):
    client = MagicMock()
    client.request.return_value = Response(payload={"results": []})
    connector.search(client, "t", "1", "q", "project", login_customer_id="123-456")
    assert client.request.call_args.kwargs["headers"]["login-customer-id"] == "123456"


def test_quota_and_message_size_429_are_distinct(connector):
    from core.quota import RateLimitError

    with pytest.raises(connector.Sa360MessageTooLargeError):
        connector._raise_response(
            Response(429, {"error": {"message": "Received message larger than 4 MB"}})
        )
    with pytest.raises(RateLimitError):
        connector._raise_response(Response(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}))


def test_429_without_size_markers_raises_rate_limit_not_message_too_large(connector):
    # H-2 negative test: a 429 body that does NOT contain the assumed size-heuristic substrings
    # must raise RateLimitError, not Sa360MessageTooLargeError.  This guards against the heuristic
    # accidentally swallowing generic quota exhaustion as a transport-size error.
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError):
        connector._raise_response(
            Response(429, {"error": {"message": "Quota exceeded for quota metric"}})
        )
    with pytest.raises(RateLimitError):
        connector._raise_response(
            Response(429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "rate limit hit"}})
        )


def test_cost_micros_and_non_additive_contract(connector):
    assert connector.API_VERSION == "v0"
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text())
    assert manifest["canonical_metric_mapping"]["metrics.cost_micros"] == "cost"
    assert (
        manifest["canonical_metric_mapping"]["metrics.search_impression_share"]["non_additive"]
        is True
    )
    catalog = json.loads((MODULE_DIR / "api_catalog.json").read_text())
    assert len(catalog["fields"]) == 15
    assert not [item for item in catalog["fields"] if item["exposure"] == "planned"]
