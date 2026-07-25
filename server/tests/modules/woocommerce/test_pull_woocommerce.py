"""Tests for the WooCommerce pull() function -- epic-25.

Uses respx to mock the WooCommerce REST v3 API. No test contacts a real store.
Live ratification is a human gate (a local WordPress + WooCommerce install suffices).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

from core.nango_client import BasicCredentials  # noqa: E402
from core.pull_errors import AuthExpiredError, InvalidRequestError  # noqa: E402

_CONNECTOR_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "woocommerce" / "connector.py"
)
_FIXTURES = (
    Path(__file__).parents[4] / "server" / "modules" / "woocommerce" / "tests" / "fixtures"
)

_STORE = "https://shop.example.com"
_ORDERS_URL = f"{_STORE}/wp-json/wc/v3/orders"
_REPORTS_URL = f"{_STORE}/wp-json/wc/v3/reports/sales"

_CREDS = BasicCredentials(
    username="ck_key", password="cs_secret", connection_config={"base_url": _STORE}
)


def _order(order_id, date_created, total, *, txn_id=None, refunds=None,
           currency="EUR", status="completed") -> dict:
    o: dict = {
        "id": order_id,
        "date_created": date_created,
        "total": total,
        "currency": currency,
        "status": status,
    }
    if txn_id is not None:
        o["transaction_id"] = txn_id
    if refunds is not None:
        o["refunds"] = refunds
    return o


_ORDERS_RESPONSE = [
    _order(1001, "2026-07-01T10:15:00", "128.50", txn_id="ch_0010"),
    _order(1002, "2026-07-01T18:40:00", "64.00", txn_id="ch_0017"),
    # WooCommerce reports refund totals as NEGATIVE amounts -> connector abs()es them.
    _order(1003, "2026-07-02T09:05:00", "210.90", txn_id="ch_0024",
           refunds=[{"id": 88, "total": "-42.18"}]),
]


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_woocommerce_pull", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


def test_transform_matches_expected_facts(connector):
    """transform(golden_pull) == expected_facts (canonical rename exercised)."""
    golden = json.loads((_FIXTURES / "golden_pull.json").read_text(encoding="utf-8"))
    expected = json.loads((_FIXTURES / "expected_facts.json").read_text(encoding="utf-8"))
    assert connector.transform(golden) == expected


@respx.mock
def test_pull_sends_status_and_window_params(connector, tmp_path, monkeypatch):
    """pull hits /orders with the date_created window + sale-of-record status filter."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "wc.duckdb"))

    route = respx.get(_ORDERS_URL).mock(return_value=httpx.Response(200, json=_ORDERS_RESPONSE))
    respx.get(_REPORTS_URL).mock(return_value=httpx.Response(200, json=[{"total_sales": "403.40"}]))

    with patch("core.nango_client.get_basic_credentials", return_value=_CREDS):
        connector.pull(
            connection_id="conn_wc",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-wc",
            pull_id="pull_wc_1",
        )

    assert route.called
    url = route.calls.last.request.url
    assert url.params["after"] == "2026-07-01T00:00:00"
    assert url.params["before"] == "2026-07-03T23:59:59"
    assert url.params["per_page"] == "100"
    # Status is an array param carrying both sale-of-record statuses.
    statuses = url.params.get_list("status[]")
    assert "completed" in statuses and "processing" in statuses


@respx.mock
def test_pull_lands_rows_with_positive_refund(connector, tmp_path, monkeypatch):
    """Rows land in raw_woocommerce_orders; a negative WooCommerce refund becomes positive."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "wc_rows.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_ORDERS_URL).mock(return_value=httpx.Response(200, json=_ORDERS_RESPONSE))
    respx.get(_REPORTS_URL).mock(return_value=httpx.Response(200, json=[{"total_sales": "403.40"}]))

    with patch("core.nango_client.get_basic_credentials", return_value=_CREDS):
        result = connector.pull(
            connection_id="conn_wc",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-wc",
            pull_id="pull_wc_2",
        )

    assert result["row_count"] == 3

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    refund = con.execute(
        "SELECT refund_amount FROM raw_woocommerce_orders WHERE order_id = '1003'"
    ).fetchone()[0]
    con.close()
    # abs(-42.18) -> +42.18, stored in the dedicated positive column.
    assert refund == pytest.approx(42.18)


@respx.mock
def test_pull_base_url_from_connection_config(connector, tmp_path, monkeypatch):
    """With no store_url override, the base URL is resolved from connection_config."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "wc_cfg.duckdb"))

    route = respx.get(_ORDERS_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(_REPORTS_URL).mock(return_value=httpx.Response(200, json=[{"total_sales": "0"}]))

    with patch("core.nango_client.get_basic_credentials", return_value=_CREDS):
        connector.pull(
            connection_id="conn_wc",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-wc",
            pull_id="pull_wc_cfg",
        )
    assert route.called


def test_pull_rejects_http_store(connector, monkeypatch):
    """A non-HTTPS store URL is rejected with a typed InvalidRequestError."""
    http_creds = BasicCredentials("ck", "cs", {"base_url": "http://insecure.example.com"})
    with patch("core.nango_client.get_basic_credentials", return_value=http_creds):
        with pytest.raises(InvalidRequestError):
            connector.pull(
                connection_id="conn_wc",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-wc",
                pull_id="pull_wc_http",
            )


@respx.mock
def test_pull_401_raises_auth_expired(connector, tmp_path, monkeypatch):
    """A 401 woocommerce_rest_authentication_error classifies as auth_expired."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "wc_401.duckdb"))

    respx.get(_ORDERS_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "code": "woocommerce_rest_authentication_error",
                "message": "Consumer key is invalid.",
                "data": {"status": 401},
            },
        )
    )

    with patch("core.nango_client.get_basic_credentials", return_value=_CREDS):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull(
                connection_id="conn_wc",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-wc",
                pull_id="pull_wc_401",
            )
    # Provider payload preserved as evidence.
    assert exc_info.value.error_class == "auth_expired"


@respx.mock
def test_pull_follows_link_header_pagination(connector, tmp_path, monkeypatch):
    """pull follows the WordPress rel=next Link header until absent."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "wc_page.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    page1 = httpx.Response(
        200,
        json=[_order(2001, "2026-07-01T10:00:00", "10.00", txn_id="ch_a")],
        headers={"Link": f'<{_ORDERS_URL}?page=2>; rel="next"'},
    )
    page2 = httpx.Response(
        200,
        json=[_order(2002, "2026-07-01T11:00:00", "20.00", txn_id="ch_b")],
    )
    respx.get(_ORDERS_URL).mock(side_effect=[page1, page2])
    respx.get(_REPORTS_URL).mock(return_value=httpx.Response(200, json=[{"total_sales": "30.00"}]))

    with patch("core.nango_client.get_basic_credentials", return_value=_CREDS):
        result = connector.pull(
            connection_id="conn_wc",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-wc",
            pull_id="pull_wc_page",
        )
    assert result["row_count"] == 2
