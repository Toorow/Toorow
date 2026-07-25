from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "brevo"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location("connector_brevo", MODULE_DIR / "connector.py")
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


def test_least_privilege_scope_union_and_invalid_scope(connector):
    scopes = connector.required_scopes(["email_campaign_daily", "contact_list_growth"])
    assert scopes == ["account:read", "campaigns.email:read", "contacts:read"]
    with pytest.raises(connector.BrevoScopeError, match="events:read"):
        connector.validate_scopes(["transactional_events"], ["account:read"])


def test_account_discovery_is_oauth_account_only(connector):
    client = MagicMock()
    client.request.return_value = Response(payload={"id": 7, "companyName": "Agency"})
    result = connector.discover_accounts("conn", _client=client, _token_value="token")
    assert result == [
        {"id": "brevo_account_selection_1", "account_id": "7", "display_name": "Agency", "plan": []}
    ]
    assert client.request.call_args.args[1].endswith("/account")


def test_campaign_offset_pagination(connector):
    client = MagicMock()
    client.request.side_effect = [
        Response(payload={"campaigns": [{"id": 1}, {"id": 2}]}),
        Response(payload={"campaigns": [{"id": 3}]}),
    ]
    rows = connector.paginate_offset(
        client, "t", "/emailCampaigns", account_id="a", item_key="campaigns", limit=2
    )
    assert [row["id"] for row in rows] == [1, 2, 3]
    assert [call.kwargs["params"]["offset"] for call in client.request.call_args_list] == [0, 2]


def test_event_window_and_page_are_bounded(connector):
    connector._validate_event_window("2026-01-01", "2026-03-31", 5000)
    with pytest.raises(ValueError, match="90 days"):
        connector._validate_event_window("2026-01-01", "2026-04-01", 5000)
    with pytest.raises(ValueError, match="5000"):
        connector._validate_event_window("2026-01-01", "2026-01-02", 5001)


def test_webhook_signature_and_immutable_dedup(connector):
    body = b'{"id":"evt-1"}'
    signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert connector.verify_webhook_signature(body, f"sha256={signature}", "secret")
    rows = connector.deduplicate_events([{"id": "evt-1"}, {"id": "evt-1"}, {"messageId": "m2"}])
    assert [row["event_id"] for row in rows] == ["evt-1", "m2"]


def test_personal_identifier_is_project_scoped_and_not_raw(connector):
    first = connector.protect_identifier("person@example.com", "project-a")
    second = connector.protect_identifier("person@example.com", "project-b")
    assert first != second
    assert "person" not in first


def test_rates_recomputed_from_counters(connector):
    assert connector.safe_rate(5, 100) == 0.05
    assert connector.safe_rate(0, 0) is None


def test_quota_headers_are_cached_per_account_endpoint(connector):
    client = MagicMock()
    client.request.return_value = Response(
        payload={},
        headers={
            "x-sib-ratelimit-limit": "10",
            "x-sib-ratelimit-remaining": "7",
            "x-sib-ratelimit-reset": "4",
        },
    )
    connector._request(client, "GET", "/contacts", "t", account_id="a")
    assert connector.quota_snapshot("a", "/contacts") == {"limit": 10, "remaining": 7, "reset": 4}


def test_429_uses_header_directed_shared_breaker(connector):
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as raised:
        connector._raise_response(Response(429, {}, {"x-sib-ratelimit-reset": "6"}))
    assert raised.value.retry_after == 6


def test_catalog_has_zero_planned_and_pii_exclusion():
    catalog = json.loads((MODULE_DIR / "api_catalog.json").read_text())
    assert not [field for field in catalog["fields"] if field["exposure"] == "planned"]
    protected = next(
        field for field in catalog["fields"] if field["field_id"] == "protected_identifier"
    )
    assert protected["exposure"] == "excluded"


def test_snapshot_and_event_profiles_remain_separate():
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text())
    reports = {report["id"]: report for report in manifest["source_capabilities"]["reports"]}
    assert reports["transactional_events"]["incremental"]["mode"] == "date_window"
    assert reports["contact_list_growth"]["incremental"]["mode"] == "full_refresh"
    assert manifest["public_catalog"]["verification"]["status"] == "blocked"
