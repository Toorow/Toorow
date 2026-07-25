"""Tests for TikTok Ads catalog_driven execution — Story 25.9 (meta-ads 25.8 pattern).

Uses respx to mock the TikTok /report/integrated/get endpoint. No test contacts the
real API. Covers:
  * the ``metrics`` JSON list is built from the selection's source_fields (event-family
    expansions map DIRECTLY to their real reporting metric names — TikTok source_field
    == field_id, no lossy re-expansion);
  * chunk merge on the (date + entity id) grain key when a wide selection is split
    across metric chunks;
  * data_level routing from the selected structural dimensions
    (campaign / adgroup / ad);
  * AD-22: the legacy exact_bundle profiles stay byte-identical (unchanged request
    shape) — the catalog profile is additive;
  * AI-58: get_module_pull_fn('tiktok-ads', 'catalog_daily') resolves pull_catalog_daily.
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

_TOOROW_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "tiktok-ads" / "connector.py"
)

_API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
_REPORT_URL = f"{_API_BASE}/report/integrated/get/"
_ADVERTISER_ID = "6800000000000000001"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_tiktok_catalog", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


def _row(campaign_id: str, day: str, **metrics) -> dict:
    """Build a mock /report/integrated/get row (dimensions + metrics envelope)."""
    return {
        "dimensions": {"campaign_id": campaign_id, "stat_time_day": f"{day} 00:00:00"},
        "metrics": metrics,
    }


def _envelope(rows: list[dict], *, page: int = 1, total_page: int = 1) -> dict:
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "list": rows,
            "page_info": {"page": page, "total_page": total_page, "total_number": len(rows)},
        },
    }


# ---------------------------------------------------------------------------
# metrics param built from the selection's source_fields (event families direct).
# ---------------------------------------------------------------------------


@respx.mock
def test_catalog_metrics_param_from_selection_source_fields(connector, tmp_path, monkeypatch):
    """The metrics JSON list carries the selection's source_fields verbatim — the
    event-family expansions (conversion_add_to_cart, total_app_install, onsite_on_web_
    order) map DIRECTLY to their real reporting metric names (source_field == field_id)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_metrics.duckdb"))

    route = respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(200, json=_envelope([]))
    )

    selection = {
        "metrics": ["spend", "conversion_add_to_cart", "total_app_install",
                    "onsite_on_web_order"],
        "dimensions": ["date", "campaign_id", "campaign_name"],
        "source_fields": {
            "spend": "spend",
            "conversion_add_to_cart": "conversion_add_to_cart",
            "total_app_install": "total_app_install",
            "onsite_on_web_order": "onsite_on_web_order",
            "date": "stat_time_day",
            "campaign_id": "campaign_id",
            "campaign_name": "campaign_name",
        },
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_catalog_daily(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-03",
            project_id="jean-tt", pull_id="pull_cat_m", selection=selection,
            advertiser_id=_ADVERTISER_ID,
        )

    assert route.called
    metrics = json.loads(route.calls.last.request.url.params["metrics"])
    # event-family ids land verbatim on the wire (no re-expansion, no dotting).
    assert "conversion_add_to_cart" in metrics
    assert "total_app_install" in metrics
    assert "onsite_on_web_order" in metrics
    assert "spend" in metrics
    # grain group-by dimensions (entity id + stat_time_day), not in the metrics list.
    dims = json.loads(route.calls.last.request.url.params["dimensions"])
    assert dims == ["campaign_id", "stat_time_day"]
    assert "stat_time_day" not in metrics and "campaign_id" not in metrics
    assert route.calls.last.request.url.params["report_type"] == "BASIC"


@respx.mock
def test_catalog_default_selection_when_none(connector, tmp_path, monkeypatch):
    """A None selection resolves to the catalog tier-core default (standalone honesty)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_default.duckdb"))

    route = respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(200, json=_envelope([]))
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_catalog_daily(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_cat_def", selection=None,
            advertiser_id=_ADVERTISER_ID,
        )

    assert route.called
    # core metrics (spend/impressions/clicks/conversions) are all in the tier-core
    # default and therefore requested. The default is wide (all core-tier metrics),
    # so it spans several ~50-metric chunks — collect metrics ACROSS every request.
    all_metrics: set[str] = set()
    for call in route.calls:
        all_metrics.update(json.loads(call.request.url.params["metrics"]))
    for core_metric in ("spend", "impressions", "clicks", "conversions"):
        assert core_metric in all_metrics


# ---------------------------------------------------------------------------
# chunk merge on the (date + entity id) grain key.
# ---------------------------------------------------------------------------


@respx.mock
def test_catalog_chunks_wide_selection_and_merges_on_grain_key(
    connector, tmp_path, monkeypatch
):
    """A selection wider than the metrics batch is split into chunks; each chunk
    requests the SAME grain rows with a metric subset, merged on (date, entity id)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_chunk.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    # Shrink the batch so 5 metrics span 3 chunks (batch 2).
    monkeypatch.setattr(connector, "_CATALOG_METRICS_BATCH", 2)

    # 5 selected metrics -> 3 chunks. Each chunk echoes the same campaign+day grain,
    # carrying only its chunk's metric values (simulating TikTok returning the subset).
    def _side_effect(request: httpx.Request):
        req_metrics = json.loads(request.url.params["metrics"])
        row_metrics = {"campaign_name": "Notoriete FR"}
        # populate only the metrics this chunk requested with distinct values.
        values = {"spend": "100.0", "impressions": "5000", "clicks": "80",
                  "conversions": "7", "reach": "4200"}
        for m in req_metrics:
            if m in values:
                row_metrics[m] = values[m]
        return httpx.Response(200, json=_envelope([
            _row("1770000000000000001", "2026-07-01", **row_metrics),
        ]))

    route = respx.get(_REPORT_URL).mock(side_effect=_side_effect)

    selection = {
        "metrics": ["spend", "impressions", "clicks", "conversions", "reach"],
        "dimensions": ["date", "campaign_id", "campaign_name"],
        "source_fields": {
            m: m for m in
            ["spend", "impressions", "clicks", "conversions", "reach",
             "campaign_id", "campaign_name"]
        } | {"date": "stat_time_day"},
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_catalog_daily(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_cat_chunk", selection=selection,
            advertiser_id=_ADVERTISER_ID,
        )

    # >1 request issued (chunked), but the grain rows merge to a SINGLE landed row.
    assert route.call_count >= 3, "wide selection must be chunked across requests"
    assert result["row_count"] == 1, "chunk rows must merge on the (date, entity id) key"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    row = con.execute(
        "SELECT campaign_id, date, spend, impressions, clicks, conversions "
        "FROM raw_tiktok_ads_daily WHERE pull_id = 'pull_cat_chunk'"
    ).fetchone()
    con.close()
    # the merged row carries the covered-metric values from ACROSS the chunks.
    assert row[0] == "1770000000000000001"
    assert row[1] == "2026-07-01"
    assert row[2] == pytest.approx(100.0)   # spend (chunk 1)
    assert row[3] == 5000                    # impressions (chunk 1)
    assert row[4] == 80                      # clicks (chunk 2)
    assert row[5] == 7                       # conversions (chunk 2)


# ---------------------------------------------------------------------------
# data_level routing from the selected structural dimensions.
# ---------------------------------------------------------------------------


@respx.mock
def test_catalog_level_routes_to_campaign_by_default(connector, tmp_path, monkeypatch):
    """No structural id beyond campaign_id -> AUCTION_CAMPAIGN, grouped by campaign_id."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_lvl_c.duckdb"))
    route = respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json=_envelope([])))

    selection = {
        "metrics": ["spend"],
        "dimensions": ["date", "campaign_id", "campaign_name"],
        "source_fields": {"spend": "spend", "date": "stat_time_day",
                          "campaign_id": "campaign_id", "campaign_name": "campaign_name"},
    }
    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_catalog_daily(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_cat_c", selection=selection,
            advertiser_id=_ADVERTISER_ID,
        )
    assert route.calls.last.request.url.params["data_level"] == "AUCTION_CAMPAIGN"
    assert json.loads(route.calls.last.request.url.params["dimensions"])[0] == "campaign_id"


@respx.mock
def test_catalog_level_routes_to_adgroup(connector, tmp_path, monkeypatch):
    """adgroup_id selected (no ad_id) -> AUCTION_ADGROUP, grouped by adgroup_id."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_lvl_ag.duckdb"))
    route = respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json=_envelope([])))

    selection = {
        "metrics": ["spend"],
        "dimensions": ["date", "adgroup_id", "campaign_id"],
        "source_fields": {"spend": "spend", "date": "stat_time_day",
                          "adgroup_id": "adgroup_id", "campaign_id": "campaign_id"},
    }
    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_catalog_daily(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_cat_ag", selection=selection,
            advertiser_id=_ADVERTISER_ID,
        )
    assert route.calls.last.request.url.params["data_level"] == "AUCTION_ADGROUP"
    assert json.loads(route.calls.last.request.url.params["dimensions"])[0] == "adgroup_id"


@respx.mock
def test_catalog_level_routes_to_ad_when_ad_id_selected(connector, tmp_path, monkeypatch):
    """ad_id selected -> AUCTION_AD (deepest structural id wins), grouped by ad_id."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_lvl_ad.duckdb"))
    route = respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json=_envelope([])))

    selection = {
        "metrics": ["spend"],
        "dimensions": ["date", "ad_id", "adgroup_id", "campaign_id"],
        "source_fields": {"spend": "spend", "date": "stat_time_day", "ad_id": "ad_id",
                          "adgroup_id": "adgroup_id", "campaign_id": "campaign_id"},
    }
    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_catalog_daily(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_cat_ad", selection=selection,
            advertiser_id=_ADVERTISER_ID,
        )
    assert route.calls.last.request.url.params["data_level"] == "AUCTION_AD"
    assert json.loads(route.calls.last.request.url.params["dimensions"])[0] == "ad_id"


# ---------------------------------------------------------------------------
# AD-22: legacy exact_bundle profiles are unchanged by the additive catalog profile.
# ---------------------------------------------------------------------------


@respx.mock
def test_ad22_legacy_campaign_profile_request_unchanged(connector, tmp_path, monkeypatch):
    """AD-22: pull_campaign_daily still issues the fixed BASIC request (data_level +
    fixed metrics/dimensions) — the catalog profile did not perturb it."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ad22.duckdb"))
    route = respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json=_envelope([])))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_campaign_daily(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_ad22", advertiser_id=_ADVERTISER_ID,
        )

    req = route.calls.last.request
    assert req.url.params["data_level"] == "AUCTION_CAMPAIGN"
    assert req.url.params["report_type"] == "BASIC"
    # fixed exact_bundle metrics for campaign_daily (unchanged contract).
    metrics = json.loads(req.url.params["metrics"])
    assert metrics == connector._metrics_for_profile("campaign_daily")
    dims = json.loads(req.url.params["dimensions"])
    assert dims == connector._dimensions_for_profile("campaign_daily")


def test_ad22_catalog_profile_is_additive_in_manifest(connector):
    """AD-22: the manifest still declares the three legacy exact_bundle profiles plus
    the new catalog_driven one — legacy reports untouched."""
    manifest = json.loads(
        (_TOOROW_PATH.parent / "manifest.json").read_text(encoding="utf-8")
    )
    reports = {r["id"]: r for r in manifest["source_capabilities"]["reports"]}
    assert reports["campaign_daily"]["selection_mode"] == "exact_bundle"
    assert reports["adgroup_daily"]["selection_mode"] == "exact_bundle"
    assert reports["ad_daily"]["selection_mode"] == "exact_bundle"
    assert reports["catalog_daily"]["selection_mode"] == "catalog_driven"
    assert reports["catalog_daily"]["dispatch"]["callable"] == "pull_catalog_daily"


# ---------------------------------------------------------------------------
# AI-58: catalog_daily dispatch resolution.
# ---------------------------------------------------------------------------


def test_ai58_dispatch_resolves_catalog_daily_pull_fn():
    """AI-58: get_module_pull_fn('tiktok-ads', 'catalog_daily') -> pull_catalog_daily."""
    import types

    from core.main import get_module_pull_fn

    tt_mod = _import_connector()
    loaded = types.SimpleNamespace(
        name="tiktok-ads",
        connector_module=tt_mod,
        manifest=json.loads(
            (_TOOROW_PATH.parent / "manifest.json").read_text(encoding="utf-8")
        ),
    )
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("tiktok-ads", "catalog_daily")
    assert fn is tt_mod.pull_catalog_daily


@respx.mock
def test_catalog_lands_rows_with_cost_rename(connector, tmp_path, monkeypatch):
    """Catalog pull lands rows in the SAME wide raw_tiktok_ads_daily; spend survives to
    the raw 'spend' column (transform renames spend -> cost downstream)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_land.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    resp = _envelope([
        _row("1770000000000000001", "2026-07-01", campaign_name="Notoriete FR",
             spend="128.50", impressions="25700", clicks="411", conversions="23"),
    ])
    respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json=resp))

    selection = {
        "metrics": ["spend", "impressions", "clicks", "conversions"],
        "dimensions": ["date", "campaign_id", "campaign_name"],
        "source_fields": {m: m for m in
                          ["spend", "impressions", "clicks", "conversions",
                           "campaign_id", "campaign_name"]} | {"date": "stat_time_day"},
    }
    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_catalog_daily(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_cat_land", selection=selection,
            advertiser_id=_ADVERTISER_ID,
        )
    assert result["row_count"] == 1

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    row = con.execute(
        "SELECT campaign_id, spend, conversions, data_level FROM raw_tiktok_ads_daily "
        "WHERE pull_id = 'pull_cat_land'"
    ).fetchone()
    con.close()
    assert row[0] == "1770000000000000001"
    assert row[1] == pytest.approx(128.50)
    assert row[2] == 23
    # catalog campaign-grain pull stamps AUCTION_CAMPAIGN (default level).
    assert row[3] == "AUCTION_CAMPAIGN"
