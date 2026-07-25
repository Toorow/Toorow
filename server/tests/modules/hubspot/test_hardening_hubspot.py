"""Focused ratification tests for HubSpot Story 33.2."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "hubspot"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location(
        "connector_hubspot_hardening", MODULE_DIR / "connector.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status_code: int, payload, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_version_builder_keeps_each_pull_on_one_api_family(connector):
    assert connector._crm_search_path("contacts", "v3") == "/crm/v3/objects/contacts/search"
    assert connector._crm_search_path("deals", "2026-03") == "/crm/objects/2026-03/deals/search"
    assert connector._CONTACTS_SEARCH_PATH.startswith("/crm/v3/")
    with pytest.raises(ValueError):
        connector._crm_search_path("tickets")


def test_account_metadata_resolves_portal_configuration(connector):
    client = MagicMock()
    client.get.return_value = Response(
        200,
        {
            "portalId": 42,
            "timeZone": "Europe/Paris",
            "companyCurrency": "EUR",
            "additionalCurrencies": ["USD"],
            "dataHostingLocation": "eu1",
        },
    )
    config = connector.discover_account_metadata("conn", _client=client, _token="secret")
    assert config == {
        "portal_id": "42",
        "timezone": "Europe/Paris",
        "company_currency": "EUR",
        "additional_currencies": ["USD"],
        "data_hosting_location": "eu1",
    }
    assert client.get.call_args.args[0].endswith("/account-info/2026-03/details")


def test_half_open_window_is_timezone_and_dst_correct(connector):
    group = connector._build_half_open_date_filter(
        "createdate", "2026-03-29", timezone_name="Europe/Paris"
    )
    filters = group["filters"]
    assert [item["operator"] for item in filters] == ["GTE", "LT"]
    start, end = (int(item["value"]) for item in filters)
    assert end - start == 23 * 60 * 60 * 1000


def test_overflow_splits_half_open_window_and_deduplicates(connector, monkeypatch):
    calls = []

    def fake_window(_client, _endpoint, groups, _properties, _headers):
        window = connector._time_window(groups)
        calls.append(window[2:])
        start, end = window[2:]
        if end - start == 10000:
            return [{"id": "floor"}], True
        if start == 0:
            return [{"id": "a"}, {"id": "shared"}], False
        return [{"id": "shared"}, {"id": "b"}], False

    monkeypatch.setattr(connector, "_search_crm_window", fake_window)
    groups = [
        {
            "filters": [
                {"propertyName": "createdate", "operator": "GTE", "value": "0"},
                {"propertyName": "createdate", "operator": "LT", "value": "10000"},
            ]
        }
    ]
    rows, truncated = connector._search_crm_objects(None, "/search", groups, [], {})
    assert {row["id"] for row in rows} == {"a", "shared", "b"}
    assert truncated is False
    assert calls == [(0, 10000), (0, 5000), (5000, 10000)]


def test_unsplittable_overflow_fails_instead_of_publishing_floor(connector, monkeypatch):
    monkeypatch.setattr(
        connector, "_search_crm_window", lambda *_args, **_kwargs: ([{"id": "floor"}], True)
    )
    groups = [
        {
            "filters": [
                {"propertyName": "closedate", "operator": "GTE", "value": "0"},
                {"propertyName": "closedate", "operator": "LT", "value": "1000"},
            ]
        }
    ]
    with pytest.raises(connector.IncompleteSearchError):
        connector._search_crm_objects(None, "/search", groups, [], {})


def test_deals_use_closed_won_property_and_company_currency(connector, monkeypatch):
    calls = []

    def fake_search(_client, _endpoint, groups, properties, _headers):
        calls.append((groups, properties))
        if len(calls) == 1:
            return ([{"id": "created"}], False)
        return ([{"id": "won", "properties": {"amount_in_home_currency": "12.50"}}], False)

    monkeypatch.setattr(connector, "_search_crm_objects", fake_search)
    row = connector._pull_deals_daily(
        None, {}, "2026-07-21", "project", "pull", "Europe/Paris", "EUR"
    )
    closed_filters = calls[1][0][0]["filters"]
    assert {item["propertyName"] for item in closed_filters} == {"closedate", "hs_is_closed_won"}
    assert "amount_in_home_currency" in calls[1][1]
    assert row["deal_amount"] == 12.5
    assert row["currency"] == "EUR"


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (403, {"category": "BANNED", "correlationId": "safe"}, "auth_revoked"),
        (423, {"message": "locked"}, "provider_transient"),
        (477, {"message": "migration"}, "provider_transient"),
        (503, ValueError("not-json"), "provider_transient"),
        # Four manifest error_map entries that were previously untested (M-1):
        (401, {"category": "EXPIRED_AUTHENTICATION", "message": "token expired"}, "auth_expired"),
        (403, {"category": "MISSING_SCOPES", "message": "no scopes"}, "permission_denied"),
        (400, {"category": "VALIDATION_ERROR", "message": "bad input"}, "invalid_request"),
        (403, {"category": "PORTAL_AUTHENTICATION_FAILED", "message": "x"},
         "permission_denied"),
    ],
)
def test_category_normalization_and_status_fallback(connector, status, payload, expected):
    with pytest.raises(Exception) as raised:
        connector._raise_hubspot_error(Response(status, payload, text="sanitized"))
    assert raised.value.error_class == expected
    if isinstance(payload, dict) and "category" in payload:
        assert raised.value.provider_payload["category"] == payload["category"]
        assert raised.value.provider_payload["code"] == payload["category"]


def test_manifest_pins_version_complete_search_and_refetch_ladder():
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["provider_api_version"] == "v3"
    assert manifest["refetch"] == {"nightly_days": 3, "weekly_days": 14, "monthly_days": 45}
    assert all(
        report["pagination"]["completeness"] == "complete"
        and report["pagination"]["truncation_signal"] == "none"
        for report in manifest["source_capabilities"]["reports"]
    )


def test_raw_and_staging_currency_contract_is_explicit(connector):
    assert "currency       VARCHAR" in connector._RAW_DEALS_DDL
    assert "deal_amount, currency" in connector._RAW_DEALS_INSERT
    staging = (MODULE_DIR / "dbt" / "staging" / "stg_hubspot_deals_daily.sql").read_text()
    assert "raw.currency" in staging


def test_default_pull_does_not_log_selected_contact_or_deal_properties(connector):
    source = (MODULE_DIR / "connector.py").read_text(encoding="utf-8")
    completion_log = source[source.index("hubspot_pull_completed") - 100 :]
    assert "properties=%s" not in completion_log
