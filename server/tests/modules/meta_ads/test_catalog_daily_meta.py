"""Story 25.8 — catalog_driven execution tests for meta-ads pull_catalog_daily.

respx-mocked (no real Meta API — the live probe is deferred to the 25.6 agent,
AI-13). Proves AC4:
  - the Insights fields+breakdowns params are built from the selection;
  - a very wide selection chunks the fields param and merges rows on the grain key;
  - an incompatible breakdown pair is refused BEFORE the API call (invalid_request);
  - action-family selections parse from the base arrays;
  - AD-22: the legacy campaign_daily pull is byte-identical on the shared fixture.
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

_CONNECTOR_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "meta-ads" / "connector.py"
)
_META_INSIGHTS_URL = "https://graph.facebook.com/v20.0/act_1234567890/insights"

_ROWS = {
    "data": [
        {
            "date_start": "2026-07-01",
            "campaign_id": "23851234500010002",
            "campaign_name": "Summer Sale - Retargeting",
            "spend": "118.40",
            "impressions": "12040",
            "clicks": "640",
            "actions": [
                {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "58"},
                {"action_type": "link_click", "value": "640"},
            ],
        }
    ],
    "paging": {},
}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_meta_catalog", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


def _selection(metrics, dimensions, catalog):
    """Build a resolved selection exactly as core would (source_fields from catalog)."""
    from core.catalog_contract import validate_selection

    resolved, issues = validate_selection(
        catalog, {"metrics": metrics, "dimensions": dimensions}
    )
    assert issues == [], issues
    return resolved


@pytest.fixture(scope="module")
def catalog():
    return json.loads(
        (_CONNECTOR_PATH.parent / "api_catalog.json").read_text(encoding="utf-8")
    )


@respx.mock
def test_catalog_pull_builds_fields_and_breakdowns_params(
    connector, catalog, tmp_path, monkeypatch
):
    """selection {spend, actions_link_click, dim age} -> fields incl spend+actions,
    breakdowns=age (age lands in the breakdowns param, NOT fields)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat.duckdb"))

    route = respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_ROWS)
    )
    selection = _selection(
        ["spend", "actions_link_click"], ["date", "campaign_id", "age"], catalog
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_1",
            selection=selection,
            ad_account_id="1234567890",
        )

    req = route.calls.last.request
    fields = req.url.params.get("fields", "")
    assert "spend" in fields
    assert "actions" in fields  # actions_link_click collapses to the base array
    assert "campaign_id" in fields
    assert req.url.params.get("breakdowns") == "age"
    assert "age" not in fields.split(",")  # age is a breakdown, not a field


@respx.mock
def test_catalog_pull_chunks_wide_selection_and_merges_rows(
    connector, catalog, tmp_path, monkeypatch
):
    """A selection wider than the batch size issues >1 request; rows merge on the
    grain key so exactly one raw row lands per (date, campaign) despite the chunks."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_chunk.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    # Force a tiny batch so a small selection still chunks.
    monkeypatch.setattr(connector, "_CATALOG_FIELDS_BATCH", 9, raising=True)

    route = respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_ROWS)
    )
    # 4 scalar metrics on top of the 7 structure fields -> forces >=2 chunks at batch 9.
    selection = _selection(
        ["spend", "impressions", "clicks", "reach"], ["date", "campaign_id"], catalog
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_chunk",
            selection=selection,
            ad_account_id="1234567890",
        )

    assert route.call_count >= 2, "wide selection must chunk into >=2 requests"
    # Despite multiple chunks, the merge collapses to ONE row per grain key.
    assert result["row_count"] == 1

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    n = con.execute(
        "SELECT COUNT(*) FROM raw_meta_ads_daily WHERE pull_id = 'pull_cat_chunk'"
    ).fetchone()[0]
    con.close()
    assert n == 1


def test_catalog_pull_refuses_incompatible_breakdown_pair_before_api(
    connector, catalog, monkeypatch
):
    """An incompatible breakdown pair (age + country) is refused with an
    invalid_request-class error BEFORE any HTTP call (breakdown_compatibility)."""
    monkeypatch.setenv("META_ADS_AD_ACCOUNT_ID", "1234567890")
    from core.pull_errors import InvalidRequestError

    selection = _selection(["spend"], ["age", "country"], catalog)
    # No respx.mock here: if the code called the API this would raise ConnectError,
    # not InvalidRequestError — proving the refusal is pre-call.
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(InvalidRequestError) as exc:
            connector.pull_catalog_daily(
                connection_id="c",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="p",
                pull_id="pull_cat_bad",
                selection=selection,
                ad_account_id="1234567890",
            )
    assert exc.value.error_class == "invalid_request"
    assert "age" in str(exc.value) and "country" in str(exc.value)


@respx.mock
def test_catalog_pull_parses_action_family_selection(
    connector, catalog, tmp_path, monkeypatch
):
    """actions_offsite_conversion_fb_pixel_purchase parses the pixel-purchase value
    (58) out of the base actions[] array into the landed conversions column."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_action.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_META_INSIGHTS_URL).mock(return_value=httpx.Response(200, json=_ROWS))
    selection = _selection(
        ["spend", "actions_offsite_conversion_fb_pixel_purchase"],
        ["date", "campaign_id"],
        catalog,
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_act",
            selection=selection,
            ad_account_id="1234567890",
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    conv = con.execute(
        "SELECT conversions FROM raw_meta_ads_daily WHERE pull_id = 'pull_cat_act'"
    ).fetchone()[0]
    con.close()
    assert conv == 58


@respx.mock
def test_catalog_pull_none_selection_uses_tier_core_default(
    connector, tmp_path, monkeypatch
):
    """A None selection resolves the catalog tier-core default and still pulls."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_def.duckdb"))

    route = respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_ROWS)
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_def",
            selection=None,
            ad_account_id="1234567890",
        )
    assert route.called
    fields = route.calls.last.request.url.params.get("fields", "")
    assert "spend" in fields  # spend is a tier-core scalar metric
    assert result["row_count"] == 1


def test_catalog_daily_dispatch_resolves_pull_catalog_daily():
    """AI-58: get_module_pull_fn('meta-ads', 'catalog_daily') -> pull_catalog_daily."""
    import types

    from core.main import get_module_pull_fn

    meta_mod = _import_connector()
    loaded = types.SimpleNamespace(
        name="meta-ads",
        connector_module=meta_mod,
        manifest=json.loads(
            (_CONNECTOR_PATH.parent / "manifest.json").read_text(encoding="utf-8")
        ),
    )
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("meta-ads", "catalog_daily")
    assert fn is meta_mod.pull_catalog_daily


@respx.mock
def test_ad22_legacy_campaign_pull_byte_identical(connector, tmp_path, monkeypatch):
    """AD-22: the legacy campaign_daily pull lands byte-identical rows on the shared
    fixture — catalog_driven did not perturb the exact_bundle path."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_ad22.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_META_INSIGHTS_URL).mock(return_value=httpx.Response(200, json=_ROWS))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_campaign_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_legacy",
            ad_account_id="1234567890",
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    row = con.execute(
        "SELECT data_level, campaign_id, spend, impressions, clicks, conversions "
        "FROM raw_meta_ads_daily WHERE pull_id = 'pull_legacy'"
    ).fetchone()
    con.close()
    # Legacy campaign grain: CAMPAIGN data_level, conversions summed from actions[] (58).
    assert row == ("CAMPAIGN", "23851234500010002", 118.40, 12040, 640, 58)
