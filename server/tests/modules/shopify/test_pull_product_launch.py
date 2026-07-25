"""Tests for the Shopify MIXED connector event path (Epic 31.6).

shopify becomes a MIXED connector: kpi profiles (orders/catalog daily ->
fact_daily_kpi) AND an event profile (product_launch -> context_events). This
module covers the event path, generalising the YouTube 31.3 reference:

  (a) transform_events(golden_events) == expected_events -- the pure canonical
      event-mapping contract, plus the H1 date-window filter and the M1 skip of
      an unpublished product (null published_at).
  (b) pull_product_launch dispatch: httpx (Admin REST /products.json, Link cursor
      pagination) mocked via respx; asserts the exact kwargs handed to
      persist_context_event (canonical product_launch type, event_date, label,
      platform='shopify', source='shopify', value=None) and that idempotence
      (delete-by-source-window) runs before the inserts (H1).

No test contacts the real API (respx) or a real DB (persist/delete mocked).
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
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "shopify"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_FIXTURES_DIR = _MODULE_DIR / "tests" / "fixtures"

_SHOP = "toorow-demo.myshopify.com"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_shopify_events", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


def _products_url(connector) -> str:
    return f"https://{_SHOP}/admin/api/{connector.SHOPIFY_API_VERSION}/products.json"


def _load_fixture(name: str):
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# (a) transform_events() -- pure canonical event mapping
# ---------------------------------------------------------------------------


def test_transform_events_matches_expected_events(connector):
    """transform_events(golden_events) == expected_events (golden replay).

    The golden fixture carries an unpublished product (published_at null); the
    windowless replay must drop it, so expected has 2 events for 3 products.
    """
    golden = _load_fixture("golden_events.json")
    expected = _load_fixture("expected_events.json")

    assert connector.transform_events(golden) == expected


def test_transform_events_skips_unpublished_product(connector):
    """M1: a null/empty/short published_at product emits no launch event."""
    rows = [
        {"title": "null published", "published_at": None},
        {"title": "empty published", "published_at": ""},
        {"title": "too short", "published_at": "2026-0"},
        {"title": "published", "published_at": "2026-07-05T00:00:00-04:00"},
    ]
    out = connector.transform_events(rows)
    assert len(out) == 1
    assert out[0]["event_date"] == "2026-07-05"
    assert out[0]["label"] == "published"


def test_transform_events_date_window_filters_out_of_range(connector):
    """H1: products outside [date_from, date_to] are dropped (whole catalogue)."""
    golden = _load_fixture("golden_events.json")  # 2026-07-03 and 2026-07-18

    windowed = connector.transform_events(
        golden, date_from="2026-07-01", date_to="2026-07-10"
    )
    assert [e["event_date"] for e in windowed] == ["2026-07-03"]

    assert connector.transform_events(
        golden, date_from="2026-08-01", date_to="2026-08-31"
    ) == []


def test_transform_events_stamps_canonical_identity(connector):
    """Every event carries the canonical platform/source/type stamps (AD-2)."""
    for ev in connector.transform_events(_load_fixture("golden_events.json")):
        assert ev["event_type"] == "product_launch"
        assert ev["platform"] == "shopify"
        assert ev["source"] == "shopify"


# ---------------------------------------------------------------------------
# (b) pull_product_launch() -- Admin REST products dispatch + persist kwargs
# ---------------------------------------------------------------------------


def _products_payload() -> dict:
    """Two published products + one draft, single page (no Link header)."""
    return {"products": _load_fixture("golden_events.json")}


@respx.mock
def test_pull_product_launch_persists_canonical_events(connector):
    """pull_product_launch -> persist_context_event with canonical kwargs."""
    respx.get(_products_url(connector)).mock(
        return_value=httpx.Response(200, json=_products_payload())
    )

    persisted: list[dict] = []
    deleted: list[dict] = []

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with patch(
            "core.context_events.persist_context_event",
            side_effect=lambda **kw: persisted.append(kw) or "evt_stub",
        ):
            with patch(
                "core.context_events.delete_connector_events_in_window",
                side_effect=lambda **kw: deleted.append(kw) or 0,
            ):
                result = connector.pull_product_launch(
                    connection_id="conn_test",
                    date_from="2026-07-01",
                    date_to="2026-07-31",
                    project_id="proj-test",
                    pull_id="pull_shopify_events_001",
                    shop_domain=_SHOP,
                )

    # 2 published products in window (draft skipped) -> 2 persisted events.
    assert result["event_count"] == 2
    assert len(persisted) == 2

    assert len(deleted) == 1
    d = deleted[0]
    assert d["project_id"] == "proj-test"
    assert d["source"] == "shopify"
    assert d["event_type"] == "product_launch"
    assert d["date_from"] == "2026-07-01"
    assert d["date_to"] == "2026-07-31"

    first = persisted[0]
    assert first["type"] == "product_launch"
    assert first["event_date"] == "2026-07-03"
    assert first["label"] == "Aurora Down Jacket"
    assert first["platform"] == "shopify"
    assert first["source"] == "shopify"
    assert first["value"] is None
    assert first["project_id"] == "proj-test"
    assert first["created_by"] == "shopify_pull:pull_shopify_events_001"


@respx.mock
def test_pull_product_launch_applies_date_window(connector):
    """H1: products are filtered to the requested window at pull time."""
    respx.get(_products_url(connector)).mock(
        return_value=httpx.Response(200, json=_products_payload())
    )

    persisted: list[dict] = []

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with patch(
            "core.context_events.persist_context_event",
            side_effect=lambda **kw: persisted.append(kw) or "evt_stub",
        ):
            with patch(
                "core.context_events.delete_connector_events_in_window", return_value=0
            ):
                result = connector.pull_product_launch(
                    connection_id="conn_test",
                    date_from="2026-07-01",
                    date_to="2026-07-10",  # excludes the 2026-07-18 product
                    project_id="proj-test",
                    pull_id="pull_shopify_events_002",
                    shop_domain=_SHOP,
                )

    assert result["event_count"] == 1
    assert [p["event_date"] for p in persisted] == ["2026-07-03"]
