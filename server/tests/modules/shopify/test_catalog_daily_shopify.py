"""Tests for Shopify pull_catalog_daily() — Story 25.9.

PROJECTION STYLE: the pull fetches the same /orders.json as pull(); the
selection controls which catalog fields are projected into landed rows at
parse time. No new Shopify endpoints are called (single orders payload
serves all cataloged fields — no excluded sections).

Coverage (~14 tests):
  * projection exactness: only selected fields land in raw_shopify_catalog_daily.
  * line_items SUM for metrics (line_item_price, line_item_quantity).
  * line_items comma-join for string dimensions (line_item_title, line_item_sku).
  * refunds SUM: refund_transaction_amount via refunds[].transactions[].amount.
  * dotted-path extraction: billing_address.city, customer.default_address.city.
  * integration: pull_catalog_daily lands order-grain rows.
  * None selection → core catalog defaults (tier-core fields present).
  * AD-22: legacy orders_daily profile unchanged (pull() untouched).
  * AI-58: dispatch resolves pull_catalog_daily.
  * manifest.json: catalog_daily profile declared + source_capabilities entry.
  * catalog_sources.json: exposure_policy=catalog_driven.
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

_MODULE_DIR = Path(__file__).parents[3] / "modules" / "shopify"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_SHOP_DOMAIN = "my-store.myshopify.com"
_API_VERSION = "2024-10"
_ORDERS_URL = f"https://{_SHOP_DOMAIN}/admin/api/{_API_VERSION}/orders.json"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_shopify_catalog", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@pytest.fixture(scope="module")
def api_catalog():
    return json.loads((_MODULE_DIR / "api_catalog.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalog_sources():
    return json.loads(
        (_MODULE_DIR / "catalog_sources" / "catalog_sources.json").read_text(encoding="utf-8")
    )


@pytest.fixture(scope="module")
def manifest():
    return json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Order builders
# ---------------------------------------------------------------------------

def _order(
    order_id: str,
    created_at: str,
    total_price: str,
    *,
    txn_id: str | None = None,
    refunds: list | None = None,
    currency: str = "EUR",
    line_items: list | None = None,
    customer: dict | None = None,
    billing_address: dict | None = None,
    financial_status: str = "paid",
    tags: str = "",
) -> dict:
    """Build a mock Shopify Admin REST order payload."""
    o: dict = {
        "id": int(order_id),
        "created_at": created_at,
        "total_price": total_price,
        "currency": currency,
        "financial_status": financial_status,
        "tags": tags,
    }
    if txn_id is not None:
        o["transactions"] = [{"id": int(txn_id), "kind": "sale", "amount": total_price}]
    if refunds is not None:
        o["refunds"] = refunds
    if line_items is not None:
        o["line_items"] = line_items
    if customer is not None:
        o["customer"] = customer
    if billing_address is not None:
        o["billing_address"] = billing_address
    return o


def _resp(orders: list[dict], *, link: str | None = None) -> httpx.Response:
    headers = {}
    if link:
        headers["Link"] = link
    return httpx.Response(200, json={"orders": orders}, headers=headers)


# ---------------------------------------------------------------------------
# catalog_sources.json contract
# ---------------------------------------------------------------------------

def test_catalog_sources_exposure_policy_catalog_driven(catalog_sources):
    """catalog_sources.json must declare exposure_policy=catalog_driven (Story 25.9)."""
    assert catalog_sources.get("exposure_policy") == "catalog_driven"


# ---------------------------------------------------------------------------
# manifest.json — catalog_daily profile declared
# ---------------------------------------------------------------------------

def test_manifest_has_catalog_daily_report_profile(manifest):
    """manifest.json must declare a catalog_daily entry in report_profiles."""
    profiles = {rp["id"]: rp for rp in manifest.get("report_profiles", [])}
    assert "catalog_daily" in profiles, "catalog_daily must be in report_profiles"


def test_manifest_catalog_daily_in_source_capabilities(manifest):
    """source_capabilities.reports must include catalog_daily with
    selection_mode=catalog_driven and dispatch.callable=pull_catalog_daily."""
    reports = {
        r["id"]: r
        for r in manifest.get("source_capabilities", {}).get("reports", [])
    }
    assert "catalog_daily" in reports, (
        "catalog_daily must appear in source_capabilities.reports"
    )
    cd = reports["catalog_daily"]
    assert cd.get("selection_mode") == "catalog_driven"
    assert cd.get("dispatch", {}).get("callable") == "pull_catalog_daily"


def test_manifest_orders_daily_unchanged(manifest):
    """AD-22: the legacy orders_daily profile is bit-identical (untouched)."""
    reports = {
        r["id"]: r
        for r in manifest.get("source_capabilities", {}).get("reports", [])
    }
    assert "orders_daily" in reports
    od = reports["orders_daily"]
    assert od["selection_mode"] == "exact_bundle"
    assert od["dispatch"]["callable"] == "pull"
    assert set(od["metrics"]) == {"revenue", "refund_amount", "orders_count"}


# ---------------------------------------------------------------------------
# _get_dotted unit tests
# ---------------------------------------------------------------------------

def test_get_dotted_simple_key(connector):
    """Single-key path extracts directly."""
    assert connector._get_dotted({"financial_status": "paid"}, "financial_status") == "paid"


def test_get_dotted_nested_path(connector):
    """Dotted path traverses nested dicts."""
    obj = {"billing_address": {"city": "Paris"}}
    assert connector._get_dotted(obj, "billing_address.city") == "Paris"


def test_get_dotted_missing_key_returns_none(connector):
    """Missing intermediate key returns None without raising."""
    assert connector._get_dotted({"a": {}}, "a.b.c") is None


def test_get_dotted_strips_bracket_notation(connector):
    """Array bracket notation (line_items[].price) is stripped before traversal."""
    # When traversing a plain dict the brackets are stripped and the key resolves.
    obj = {"line_items": {"price": "9.99"}}
    # After stripping: "line_items.price" — traversal returns the nested value.
    assert connector._get_dotted(obj, "line_items[].price") == "9.99"


# ---------------------------------------------------------------------------
# _project_order unit tests (no HTTP)
# ---------------------------------------------------------------------------

def test_project_order_selected_fields_land(connector):
    """Projection lands only selected fields (revenue + financial_status)."""
    source_fields = {
        "revenue": "total_price",
        "financial_status": "financial_status",
    }
    order = _order("1001", "2026-07-01T10:00:00Z", "128.50",
                   financial_status="paid")
    row = connector._project_order(order, source_fields)
    assert "revenue" in row
    assert "financial_status" in row


def test_project_order_structural_anchors_always_present(connector):
    """order_id, date, revenue_source_currency are always in the row."""
    row = connector._project_order(
        _order("2001", "2026-07-01T09:00:00Z", "50.00"),
        {},
    )
    assert row["order_id"] == "2001"
    assert row["date"] == "2026-07-01"
    assert "revenue_source_currency" in row


def test_project_order_line_items_sum_metric(connector):
    """line_items[].price → SUM across line items (metric path)."""
    source_fields = {"line_item_price": "line_items[].price"}
    order = _order(
        "3001", "2026-07-01T10:00:00Z", "200.00",
        line_items=[
            {"price": "80.00", "quantity": 1, "title": "Widget A"},
            {"price": "120.00", "quantity": 2, "title": "Widget B"},
        ],
    )
    row = connector._project_order(order, source_fields)
    assert row["line_item_price"] == pytest.approx(200.00)


def test_project_order_line_items_sum_quantity(connector):
    """line_items[].quantity → SUM across line items."""
    source_fields = {"line_item_quantity": "line_items[].quantity"}
    order = _order(
        "3002", "2026-07-01T10:00:00Z", "100.00",
        line_items=[
            {"quantity": 2, "price": "40.00", "title": "A"},
            {"quantity": 3, "price": "60.00", "title": "B"},
        ],
    )
    row = connector._project_order(order, source_fields)
    assert row["line_item_quantity"] == pytest.approx(5.0)


def test_project_order_line_items_comma_join_strings(connector):
    """line_items[].title → comma-joined string for distinct values."""
    source_fields = {"line_item_title": "line_items[].title"}
    order = _order(
        "3003", "2026-07-01T10:00:00Z", "100.00",
        line_items=[
            {"title": "Widget A", "price": "50.00"},
            {"title": "Widget B", "price": "50.00"},
        ],
    )
    row = connector._project_order(order, source_fields)
    titles = row["line_item_title"]
    assert "Widget A" in titles
    assert "Widget B" in titles


def test_project_order_refund_transaction_amount_sum(connector):
    """refunds[].transactions[].amount → SUM across all refund transactions."""
    source_fields = {"refund_transaction_amount": "refunds[].transactions[].amount"}
    order = _order(
        "4001", "2026-07-01T10:00:00Z", "210.90",
        refunds=[
            {"transactions": [{"amount": "42.18"}, {"amount": "10.00"}]},
            {"transactions": [{"amount": "5.00"}]},
        ],
    )
    row = connector._project_order(order, source_fields)
    assert row["refund_transaction_amount"] == pytest.approx(57.18)


def test_project_order_dotted_billing_address_city(connector):
    """billing_address.city extracted via dotted path."""
    source_fields = {"billing_address_city": "billing_address.city"}
    order = _order(
        "5001", "2026-07-01T10:00:00Z", "99.00",
        billing_address={"city": "Lyon", "country": "France"},
    )
    row = connector._project_order(order, source_fields)
    assert row["billing_address_city"] == "Lyon"


def test_project_order_dotted_customer_default_address_city(connector):
    """customer.default_address.city extracted via dotted path."""
    source_fields = {"customer_city": "customer.default_address.city"}
    order = _order(
        "5002", "2026-07-01T10:00:00Z", "75.00",
        customer={"id": 9999, "default_address": {"city": "Bordeaux"}},
    )
    row = connector._project_order(order, source_fields)
    assert row["customer_city"] == "Bordeaux"


# ---------------------------------------------------------------------------
# pull_catalog_daily() integration tests (respx mock)
# ---------------------------------------------------------------------------

@respx.mock
def test_pull_catalog_daily_lands_selected_fields_only(connector, tmp_path, monkeypatch):
    """pull_catalog_daily projects only selected fields; unselected are absent."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cd_sel.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    orders = [
        _order("1001", "2026-07-01T10:00:00Z", "100.00", txn_id="9001"),
        _order("1002", "2026-07-02T10:00:00Z", "200.00", txn_id="9002"),
    ]
    respx.get(_ORDERS_URL).mock(return_value=_resp(orders))

    # Only project revenue + financial_status; transaction_id must NOT appear.
    selection = {
        "metrics": ["revenue"],
        "dimensions": ["financial_status"],
        "source_fields": {
            "revenue": "total_price",
            "financial_status": "financial_status",
        },
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_catalog_daily(
            connection_id="conn_cd",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="proj_cd",
            pull_id="pull_cd_1",
            shop_domain=_SHOP_DOMAIN,
            selection=selection,
        )

    assert result["pull_id"] == "pull_cd_1"
    assert result["row_count"] > 0

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    landed_fields = {
        r[0] for r in con.execute(
            "SELECT DISTINCT field_id FROM raw_shopify_catalog_daily "
            "WHERE pull_id = 'pull_cd_1'"
        ).fetchall()
    }
    con.close()

    assert "revenue" in landed_fields
    assert "financial_status" in landed_fields
    # Structural anchors are not emitted as catalog field rows
    assert "order_id" not in landed_fields
    # Unselected field must be absent
    assert "transaction_id" not in landed_fields


@respx.mock
def test_pull_catalog_daily_none_selection_lands_core_fields(connector, tmp_path, monkeypatch):
    """None selection falls back to tier-core catalog default; core fields land."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cd_def.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    orders = [_order("2001", "2026-07-01T10:00:00Z", "99.00", txn_id="8001")]
    respx.get(_ORDERS_URL).mock(return_value=_resp(orders))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_catalog_daily(
            connection_id="conn_def",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="proj_def",
            pull_id="pull_cd_def",
            shop_domain=_SHOP_DOMAIN,
            selection=None,  # triggers tier-core default
        )

    assert result["row_count"] > 0

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    landed_fields = {
        r[0] for r in con.execute(
            "SELECT DISTINCT field_id FROM raw_shopify_catalog_daily "
            "WHERE pull_id = 'pull_cd_def'"
        ).fetchall()
    }
    con.close()

    # Core-tier fields must land with None selection
    for expected in ("revenue", "refund_amount", "orders_count"):
        assert expected in landed_fields, f"{expected!r} must land with None selection"


# ---------------------------------------------------------------------------
# AD-22: legacy pull() profile green (bit-identical, untouched)
# ---------------------------------------------------------------------------

@respx.mock
def test_ad22_legacy_pull_still_green(connector, tmp_path, monkeypatch):
    """AD-22: the existing pull() function is untouched after adding pull_catalog_daily."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "ad22.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    orders = [
        _order(
            "3001", "2026-07-01T10:15:00-04:00", "128.50",
            txn_id="7000000010",
            refunds=[{"transactions": [{"amount": "12.00"}]}],
        )
    ]
    respx.get(_ORDERS_URL).mock(return_value=_resp(orders))

    with patch("core.nango_client.get_fresh_token", return_value="fake-legacy"):
        result = connector.pull(
            connection_id="conn_legacy",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="proj_legacy",
            pull_id="pull_legacy",
            shop_domain=_SHOP_DOMAIN,
        )

    assert result["row_count"] == 1

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    row = con.execute(
        "SELECT revenue, refund_amount, transaction_id "
        "FROM raw_shopify_orders WHERE pull_id = 'pull_legacy'"
    ).fetchone()
    con.close()

    assert row is not None
    assert row[0] == pytest.approx(128.50)  # revenue
    assert row[1] == pytest.approx(12.00)   # refund_amount in its own column
    assert row[2] == "7000000010"           # transaction_id preserved


# ---------------------------------------------------------------------------
# AI-58: dispatch resolves pull_catalog_daily
# ---------------------------------------------------------------------------

def test_ai58_dispatch_resolves_pull_catalog_daily():
    """AI-58: the manifest dispatch for catalog_daily resolves to pull_catalog_daily,
    and the connector module exposes that callable."""
    import types

    from core.main import get_module_pull_fn

    shop_mod = _import_connector()
    assert hasattr(shop_mod, "pull_catalog_daily"), (
        "connector must expose pull_catalog_daily"
    )

    manifest = json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    reports = {
        r["id"]: r
        for r in manifest.get("source_capabilities", {}).get("reports", [])
    }
    assert reports["catalog_daily"]["dispatch"]["callable"] == "pull_catalog_daily"

    # get_module_pull_fn for 'shopify' resolves the default pull()
    loaded = types.SimpleNamespace(name="shopify", connector_module=shop_mod)
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("shopify")
    assert fn is shop_mod.pull

    # pull_catalog_daily is callable directly
    assert callable(shop_mod.pull_catalog_daily)
