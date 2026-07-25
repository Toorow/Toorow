from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "taboola"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location("connector_taboola", MODULE_DIR / "connector.py")
    m = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(m)
    return m


class Response:
    def __init__(self, status_code=200, payload=None, headers=None, text=""):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        return self.payload


def test_account_discovery_timezone_currency(connector):
    c = MagicMock()
    c.request.return_value = Response(
        payload={
            "results": [
                {"account_id": "acct", "name": "Adv", "timezone": "Europe/Paris", "currency": "EUR"}
            ]
        }
    )
    r = connector.discover_accounts("x", _client=c, _token_value="t")[0]
    assert r["timezone"] == "Europe/Paris" and r["currency"] == "EUR"


def test_empty_account_actionable(connector):
    c = MagicMock()
    c.request.return_value = Response(payload={"results": []})
    with pytest.raises(connector.TaboolaOnboardingError):
        connector.discover_accounts("x", _client=c, _token_value="t")


def test_report_dimension_and_filter_fail_before_call(connector):
    with pytest.raises(connector.TaboolaCompatibilityError):
        connector.validate_shape("campaign_summary", "item_breakdown")
    with pytest.raises(connector.TaboolaCompatibilityError):
        connector.validate_shape("campaign_summary", "day", {"bad": 1})


def test_dynamic_metadata_must_declare_returned_columns(connector):
    assert connector.validate_response_metadata(
        [{"date": "x", "my_conversion": 1}], {"fields": [{"id": "my_conversion"}]}
    ) == ["my_conversion"]
    with pytest.raises(connector.TaboolaCompatibilityError):
        connector.validate_response_metadata([{"mystery": 1}], {"fields": []})


def test_top_content_limit_is_explicit(connector):
    c = MagicMock()
    c.request.return_value = Response(
        payload={
            "results": [{"item_id": str(i), "impressions": 1} for i in range(1000)],
            "total": 1500,
            "metadata": {"fields": []},
        }
    )
    r = connector.fetch_report(
        c, "t", "a", "top_campaign_content", "item_breakdown", page_size=2000
    )
    assert len(r["rows"]) == 1000 and r["complete"] is False and r["row_limit"] == 1000


def test_history_paginates_and_remains_record_rows(connector):
    c = MagicMock()
    c.request.side_effect = [
        Response(payload={"results": [{"record_id": "1"}], "total": 2}),
        Response(payload={"results": [{"record_id": "2"}], "total": 2}),
    ]
    r = connector.fetch_report(c, "t", "a", "campaign_history", "by_campaign", page_size=1)
    assert [x["record_id"] for x in r["rows"]] == ["1", "2"]


def test_prior_month_fifth_refresh(connector):
    assert connector.should_refresh_prior_month(date(2026, 7, 5))
    assert connector.prior_month_window(date(2026, 7, 5)) == ("2026-06-01", "2026-06-30")


def test_429_breaker(connector):
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as e:
        connector._raise_response(Response(429, {}, {"Retry-After": "8"}))
    assert e.value.retry_after == 8


def test_catalog_zero_planned_and_history_separate():
    cat = json.loads((MODULE_DIR / "api_catalog.json").read_text())
    assert not [f for f in cat["fields"] if f["exposure"] == "planned"]
    assert (
        "raw_taboola_history"
        in (MODULE_DIR / "dbt" / "staging" / "stg_taboola_history.sql").read_text()
    )
