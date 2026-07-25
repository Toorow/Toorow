"""Tests for the Shopify pull() function — Story 15.4 (Epic 15).

Uses respx to mock the Shopify Admin REST orders API. No test contacts the real API.
Real Shopify E2E is a human gate (BLOCKED Phase B, AI-08/AI-13: merchant OAuth + real data).
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

_TOOROW_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "shopify" / "connector.py"
)

_SHOP_DOMAIN = "my-store.myshopify.com"
_API_VERSION = "2024-10"
_ORDERS_URL = (
    f"https://{_SHOP_DOMAIN}/admin/api/{_API_VERSION}/orders.json"
)
_ORDERS_URL_PAGE2 = (
    f"https://{_SHOP_DOMAIN}/admin/api/{_API_VERSION}/orders.json?page_info=NEXTCURSOR&limit=250"
)


def _order(order_id: str, created_at: str, total_price: str, *, txn_id: str | None = None,
           refunds=None, currency: str = "EUR") -> dict:
    """Build a mock Admin REST order payload (AI-53: field shapes to confirm live)."""
    o: dict = {
        "id": int(order_id),
        "created_at": created_at,
        "total_price": total_price,
        "currency": currency,
    }
    if txn_id is not None:
        o["transactions"] = [{"id": int(txn_id), "kind": "sale", "amount": total_price}]
    if refunds is not None:
        o["refunds"] = refunds
    return o


_ORDERS_RESPONSE = {
    "orders": [
        _order("1000000001", "2026-07-01T10:15:00-04:00", "128.50", txn_id="7000000010"),
        _order("1000000002", "2026-07-01T18:40:00-04:00", "64.00", txn_id="7000000017"),
        _order(
            "1000000003", "2026-07-02T09:05:00-04:00", "210.90", txn_id="7000000024",
            refunds=[{"transactions": [{"amount": "42.18"}]}],
        ),
    ]
}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_shopify_pull", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@respx.mock
def test_pull_calls_orders_endpoint_with_window(connector, tmp_path, monkeypatch):
    """pull hits the Admin REST orders endpoint with the created_at window + limit."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "shop.duckdb"))
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    route = respx.get(_ORDERS_URL).mock(
        return_value=httpx.Response(200, json=_ORDERS_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_shop",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-shop",
            pull_id="pull_shop_p1",
            shop_domain=_SHOP_DOMAIN,
        )

    assert route.called
    req = route.calls.last.request
    assert req.url.params["created_at_min"] == "2026-07-01T00:00:00Z"
    assert req.url.params["created_at_max"] == "2026-07-03T23:59:59Z"
    assert req.url.params["limit"] == "250"


@respx.mock
def test_pull_lands_rows_with_refund_dedicated_column(connector, tmp_path, monkeypatch):
    """Rows land in raw_shopify_orders; refund_amount is a dedicated positive column."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "shop_rows.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    respx.get(_ORDERS_URL).mock(return_value=httpx.Response(200, json=_ORDERS_RESPONSE))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_shop",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-shop",
            pull_id="pull_shop_p2",
            shop_domain=_SHOP_DOMAIN,
        )

    assert result["pull_id"] == "pull_shop_p2"
    assert result["row_count"] == 3

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT order_id, transaction_id, revenue, refund_amount, date "
        "FROM raw_shopify_orders WHERE pull_id = 'pull_shop_p2' ORDER BY order_id"
    ).fetchall()
    con.close()

    assert len(rows) == 3
    by_order = {r[0]: r for r in rows}
    # gross revenue preserved (refund NOT subtracted from revenue -- decision 15.4)
    assert by_order["1000000003"][2] == pytest.approx(210.90)
    assert by_order["1000000003"][3] == pytest.approx(42.18)  # refund in its own column
    # transaction_id captured as the GA4 x Shopify join key
    assert by_order["1000000001"][1] == "7000000010"
    # created_at -> day-grain date (never '')
    assert by_order["1000000001"][4] == "2026-07-01"
    assert all(r[4] != "" for r in rows)


@respx.mock
def test_pull_follows_link_header_pagination(connector, tmp_path, monkeypatch):
    """pull follows the Link rel=next cursor across multiple pages (multi-date)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "shop_page.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    page1 = {
        "orders": [
            _order("1000000001", "2026-07-01T10:00:00Z", "100.00", txn_id="7000000010"),
        ]
    }
    page2 = {
        "orders": [
            _order("1000000002", "2026-07-02T10:00:00Z", "200.00", txn_id="7000000017"),
        ]
    }
    link_next = f'<{_ORDERS_URL_PAGE2}>; rel="next"'
    # Single route + side_effect sequence: respx matches by PATH (query ignored unless
    # declared), so two routes on the same path would both hit the first one — the page-2
    # request would receive the Link header again and loop. (Exactly the scenario the
    # review-15-4 F-2 pagination guard exists for — caught live by the guard.)
    route = respx.get(_ORDERS_URL).mock(
        side_effect=[
            httpx.Response(200, json=page1, headers={"Link": link_next}),
            httpx.Response(200, json=page2),
        ]
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_shop",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-shop",
            pull_id="pull_shop_pg",
            shop_domain=_SHOP_DOMAIN,
        )

    assert route.call_count == 2, "rel=next page must be fetched exactly once"
    # The second request must target the rel=next cursor URL (page_info carried).
    assert "page_info" in str(route.calls[1].request.url), "page-2 URL must carry cursor"
    assert result["row_count"] == 2

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    dates = sorted(
        r[0] for r in con.execute(
            "SELECT DISTINCT date FROM raw_shopify_orders WHERE pull_id = 'pull_shop_pg'"
        ).fetchall()
    )
    con.close()
    assert dates == ["2026-07-01", "2026-07-02"]


@respx.mock
def test_pull_raises_rate_limit_error_on_429(connector, tmp_path, monkeypatch):
    """pull raises RateLimitError('shopify', ...) on a 429 (leaky-bucket overflow)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "shop_429.duckdb"))
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    respx.get(_ORDERS_URL).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "2.0"}, json={"errors": "throttled"}
        )
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull(
                connection_id="conn_shop",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-shop",
                pull_id="pull_shop_429",
                shop_domain=_SHOP_DOMAIN,
            )

    assert exc_info.value.platform == "shopify"
    assert exc_info.value.retry_after == 2


@respx.mock
def test_pull_non_429_error_raises_runtime_error(connector, tmp_path, monkeypatch):
    """pull raises ConnectorError (subclass of RuntimeError) on non-200, non-429 responses."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "shop_500.duckdb"))
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    respx.get(_ORDERS_URL).mock(return_value=httpx.Response(500, json={"errors": "boom"}))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RuntimeError) as exc_info:
            connector.pull(
                connection_id="conn_shop",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-shop",
                pull_id="pull_shop_500",
                shop_domain=_SHOP_DOMAIN,
            )
    assert "500" in str(exc_info.value)


@respx.mock
def test_pull_401_raises_auth_expired_with_payload(connector, tmp_path, monkeypatch):
    """Story 25.7 (AC3): 401 raises AuthExpiredError; provider payload preserved (AD-2)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "shop_401.duckdb"))
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    provider_body = {"errors": "Unauthorized"}
    respx.get(_ORDERS_URL).mock(
        return_value=httpx.Response(401, json=provider_body)
    )

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull(
                connection_id="conn_shop",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-shop",
                pull_id="pull_shop_401",
                shop_domain=_SHOP_DOMAIN,
            )

    err = exc_info.value
    # Provider HTTP status preserved
    assert err.provider_status == 401
    # Provider payload preserved (evidence, AD-2)
    assert err.provider_payload == provider_body
    # error_class maps to auth_expired (pure HTTP 401 classification)
    assert err.error_class == "auth_expired"
    # Not retryable — user must reconnect
    assert err.retryable is False


@respx.mock
def test_pull_shop_domain_env_fallback(connector, tmp_path, monkeypatch):
    """shop_domain resolves from SHOPIFY_SHOP_DOMAIN when not passed (queue dispatch path)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "shop_env.duckdb"))
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)
    monkeypatch.setenv("SHOPIFY_SHOP_DOMAIN", _SHOP_DOMAIN)

    route = respx.get(_ORDERS_URL).mock(
        return_value=httpx.Response(200, json={"orders": []})
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_shop",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-shop",
            pull_id="pull_shop_env",
        )
    assert route.called


def test_pull_requires_shop_domain(connector, monkeypatch):
    """pull raises a clear ValueError when neither arg nor SHOPIFY_SHOP_DOMAIN is set."""
    monkeypatch.delenv("SHOPIFY_SHOP_DOMAIN", raising=False)
    with pytest.raises(ValueError, match="SHOPIFY_SHOP_DOMAIN"):
        connector.pull(
            connection_id="conn_shop",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-shop",
            pull_id="pull_shop_nodomain",
        )


@respx.mock
def test_pull_token_not_stored(connector, tmp_path, monkeypatch, caplog):
    """AD-3: the token must not appear in the return value or any log."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "shop_tok.duckdb"))
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    respx.get(_ORDERS_URL).mock(return_value=httpx.Response(200, json=_ORDERS_RESPONSE))

    secret = "super-secret-shopify-token-98765"
    with caplog.at_level(logging.DEBUG):
        with patch("core.nango_client.get_fresh_token", return_value=secret):
            result = connector.pull(
                connection_id="conn_shop",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-shop",
                pull_id="pull_shop_tok",
                shop_domain=_SHOP_DOMAIN,
            )

    assert secret not in str(result)
    for record in caplog.records:
        assert secret not in record.getMessage()


@respx.mock
def test_pull_respects_nango_provider(connector, tmp_path, monkeypatch):
    """AD-3: token obtained via nango_client.get_fresh_token(provider='shopify')."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "shop_auth.duckdb"))
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    captured: list[dict] = []

    def _capture(request: httpx.Request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json={"orders": []})

    respx.get(_ORDERS_URL).mock(side_effect=_capture)

    with patch("core.nango_client.get_fresh_token", return_value="bearer-shop-token") as mock_nango:
        connector.pull(
            connection_id="conn_shop_auth",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-shop",
            pull_id="pull_shop_auth",
            shop_domain=_SHOP_DOMAIN,
        )
        mock_nango.assert_called_once_with("conn_shop_auth", provider="shopify")

    # Shopify uses X-Shopify-Access-Token (not a Bearer header).
    assert captured[0].get("x-shopify-access-token") == "bearer-shop-token"


def test_transform_renames_source_fields_to_canonical(connector):
    """transform() renomme les clés SOURCE de la golden fixture (total_price,
    total_refunded) vers les clés canoniques d'expected_facts (revenue, refund_amount)
    via le canonical_metric_mapping du manifest — le rename map est réellement exercé
    (review-15-4 F-1, AI-54 : la fixture est le vrai payload post-parse, clés API).

    review-15-9 F-2 (couverture _parse_order) : la golden fixture est POST-parse par
    choix (clés source total_price/total_refunded, PAS le shape API brut {id, created_at,
    transactions[], refunds[]}), donc on ne la fait PAS retransiter par _parse_order ici.
    _parse_order est genuinely exercé par le test de pull raw
    test_pull_lands_rows_with_refund_dedicated_column (lignes ~104-145) : il envoie le
    vrai payload Admin REST (_ORDERS_RESPONSE, orders API-shaped) à travers pull() ->
    _parse_order et assert les colonnes landées (revenue brut préservé, refund_amount en
    colonne dédiée, transaction_id capté, created_at -> date jour). La chaîne complète
    _parse_order -> transform est donc couverte (pull raw + ce test) sans dupliquer une
    fixture API-shaped."""
    fixtures = _TOOROW_PATH.parent / "tests" / "fixtures"
    golden = json.loads((fixtures / "golden_pull.json").read_text(encoding="utf-8"))
    expected = json.loads((fixtures / "expected_facts.json").read_text(encoding="utf-8"))
    assert connector.transform(golden) == expected


def test_dispatch_resolves_default_pull_fn():
    """AI-58: get_module_pull_fn('shopify') resolves the connector's pull()."""
    import types

    from core.main import get_module_pull_fn

    shop_mod = _import_connector()
    loaded = types.SimpleNamespace(name="shopify", connector_module=shop_mod)
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("shopify")
    assert fn is shop_mod.pull


# ---------------------------------------------------------------------------
# Story 25.9 — catalog_daily (projection-style) tests
# ---------------------------------------------------------------------------

# Fixture: an Admin REST order that exercises all projection paths.
_CATALOG_ORDER_RICH = {
    "id": 2000000001,
    "created_at": "2026-07-10T09:30:00Z",
    "total_price": "250.00",
    "currency": "EUR",
    "financial_status": "paid",
    "fulfillment_status": "fulfilled",
    "email": "buyer@example.com",
    "source_name": "web",
    "tags": "vip,newsletter",
    "cancel_reason": None,
    "cancelled_at": None,
    "closed_at": None,
    "confirmed": True,
    "current_subtotal_price": "230.00",
    "current_total_price": "250.00",
    "current_total_tax": "20.00",
    "name": "#2000000001",
    "note": None,
    "order_number": 2001,
    "payment_gateway_names": ["shopify_payments"],
    "phone": None,
    "presentment_currency": "EUR",
    "processed_at": "2026-07-10T09:30:00Z",
    "subtotal_price": "230.00",
    "test": False,
    "total_discounts": "10.00",
    "total_line_items_price": "240.00",
    "total_outstanding": "0.00",
    "total_tax": "20.00",
    "updated_at": "2026-07-10T10:00:00Z",
    "transactions": [{"id": 8000000001, "kind": "sale", "amount": "250.00"}],
    "refunds": [
        {
            "id": 9000000001,
            "created_at": "2026-07-10T11:00:00Z",
            "note": "customer return",
            "transactions": [{"amount": "50.00"}],
        }
    ],
    "line_items": [
        {
            "id": 3000000001,
            "product_id": 4000000001,
            "variant_id": 5000000001,
            "name": "Widget A - Large",
            "title": "Widget A",
            "sku": "WGT-A-L",
            "vendor": "ACME",
            "quantity": 2,
            "price": "100.00",
            "total_discount": "5.00",
            "gift_card": False,
            "fulfillment_status": "fulfilled",
        },
        {
            "id": 3000000002,
            "product_id": 4000000002,
            "variant_id": 5000000002,
            "name": "Widget B - Small",
            "title": "Widget B",
            "sku": "WGT-B-S",
            "vendor": "ACME",
            "quantity": 1,
            "price": "140.00",
            "total_discount": "5.00",
            "gift_card": False,
            "fulfillment_status": "fulfilled",
        },
    ],
    "customer": {
        "id": 6000000001,
        "email": "buyer@example.com",
        "first_name": "Jean",
        "last_name": "Test",
        "phone": None,
        "tags": "vip",
        "created_at": "2026-01-01T00:00:00Z",
        "orders_count": 3,
        "total_spent": "750.00",
        "accepts_marketing": True,
        "default_address": {
            "city": "Paris",
            "country": "France",
            "country_code": "FR",
        },
    },
    "billing_address": {
        "city": "Paris",
        "country": "France",
        "country_code": "FR",
        "province": "Île-de-France",
        "zip": "75001",
    },
    "shipping_address": {
        "city": "Lyon",
        "country": "France",
        "country_code": "FR",
        "province": "Auvergne-Rhône-Alpes",
        "zip": "69001",
    },
    "landing_site": "/collections/widgets",
    "referring_site": "https://google.com",
}

_CATALOG_ORDERS_RESPONSE = {"orders": [_CATALOG_ORDER_RICH]}


def test_project_order_nested_dotted_path(connector):
    """_project_order: dotted source_field (billing_address.city) accesses nested dict."""
    projection = connector._project_order(
        _CATALOG_ORDER_RICH,
        {
            "billing_address_city": "billing_address.city",
            "customer_email": "customer.email",
            "customer_city": "customer.default_address.city",
        },
    )
    assert projection["billing_address_city"] == "Paris"
    assert projection["customer_email"] == "buyer@example.com"
    assert projection["customer_city"] == "Paris"


def test_project_order_line_items_array_metric(connector):
    """_project_order: line_items[] metric (price) is summed across items (order-grain)."""
    projection = connector._project_order(
        _CATALOG_ORDER_RICH,
        {
            "line_item_price": "line_items[].price",
            "line_item_quantity": "line_items[].quantity",
        },
    )
    # 100.00 + 140.00 = 240.00
    assert projection["line_item_price"] == pytest.approx(240.00)
    # 2 + 1 = 3
    assert projection["line_item_quantity"] == pytest.approx(3.0)


def test_project_order_line_items_array_dimension(connector):
    """_project_order: line_items[] dimension (sku) produces joined string (order-grain)."""
    projection = connector._project_order(
        _CATALOG_ORDER_RICH,
        {"line_item_sku": "line_items[].sku"},
    )
    # Two SKUs joined — order matters (list order)
    assert "WGT-A-L" in projection["line_item_sku"]
    assert "WGT-B-S" in projection["line_item_sku"]


def test_project_order_refunds_array_nested(connector):
    """_project_order: refunds[].transactions[].amount is summed correctly."""
    projection = connector._project_order(
        _CATALOG_ORDER_RICH,
        {"refund_transaction_amount": "refunds[].transactions[].amount"},
    )
    assert projection["refund_transaction_amount"] == pytest.approx(50.00)


def test_project_order_refund_note_dimension(connector):
    """_project_order: refunds[].note (scalar refund sub-field) is extracted correctly."""
    projection = connector._project_order(
        _CATALOG_ORDER_RICH,
        {"refund_note": "refunds[].note"},
    )
    assert projection["refund_note"] == "customer return"


def test_project_order_special_computed_date(connector):
    """_project_order: created_at source_field projects to day-grain date."""
    projection = connector._project_order(
        _CATALOG_ORDER_RICH,
        {"date": "created_at"},
    )
    assert projection["date"] == "2026-07-10"


def test_project_order_special_orders_count(connector):
    """_project_order: 'order' source_field always projects 1 (one order per row)."""
    projection = connector._project_order(
        _CATALOG_ORDER_RICH,
        {"orders_count": "order"},
    )
    assert projection["orders_count"] == 1


def test_project_order_missing_nested_path_returns_none(connector):
    """_project_order: absent nested path returns None (not KeyError)."""
    order_without_billing = {**_CATALOG_ORDER_RICH}
    order_without_billing.pop("billing_address", None)
    projection = connector._project_order(
        order_without_billing,
        {"billing_address_city": "billing_address.city"},
    )
    assert projection["billing_address_city"] is None


def test_project_order_missing_line_items_returns_none(connector):
    """_project_order: line_items[] path on order with no items returns None."""
    order_no_items = {**_CATALOG_ORDER_RICH, "line_items": []}
    projection = connector._project_order(
        order_no_items,
        {"line_item_price": "line_items[].price"},
    )
    assert projection["line_item_price"] is None


@respx.mock
def test_pull_orders_daily_profile_unchanged_ad22(connector, tmp_path, monkeypatch):
    """AD-22: the legacy orders_daily profile (pull()) behavior is bit-identical to pre-25.9."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "shop_ad22.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("SHOPIFY_API_VERSION", _API_VERSION)

    respx.get(_ORDERS_URL).mock(return_value=httpx.Response(200, json=_ORDERS_RESPONSE))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_shop",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-shop",
            pull_id="pull_ad22",
            shop_domain=_SHOP_DOMAIN,
        )

    # AD-22: unchanged — same row count, same columns, no regression from 25.9.
    assert result["row_count"] == 3

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT order_id, revenue, refund_amount FROM raw_shopify_orders "
        "WHERE pull_id = 'pull_ad22' ORDER BY order_id"
    ).fetchall()
    con.close()

    assert len(rows) == 3
    by_order = {r[0]: r for r in rows}
    assert by_order["1000000003"][1] == pytest.approx(210.90)  # revenue untouched
    assert by_order["1000000003"][2] == pytest.approx(42.18)   # refund_amount in own column


# ---------------------------------------------------------------------------
# Real Shopify E2E — BLOCKED Phase B (AI-08/AI-13): merchant OAuth + live data.
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Real Shopify E2E — BLOCKED Phase B (merchant OAuth, AI-08/AI-13)")
def test_pull_e2e_with_real_shopify():  # pragma: no cover
    """Placeholder for the live Shopify Admin API smoke test (Phase B)."""
