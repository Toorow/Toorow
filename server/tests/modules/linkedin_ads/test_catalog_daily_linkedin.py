"""Story 25.9 — catalog_driven execution tests for linkedin-ads pull_catalog_daily.

respx-mocked (no real LinkedIn API — real LinkedIn E2E is a human gate, AI-08 /
Phase B). Proves the 25.9 rollout for the param-style LinkedIn module:
  - the adAnalytics fields param is built from the selection (metric source_fields);
  - a very wide selection CHUNKS the fields param and MERGES elements on the
    (dateRange + pivotValues) grain key so exactly one raw row lands per grain;
  - the SINGLE pivot param is built from the selected pivot_* dimension;
  - selecting >1 pivot is refused BEFORE the API call (invalid_request);
  - a None selection resolves the catalog tier-core default and still pulls;
  - AI-58: get_module_pull_fn('linkedin-ads', 'catalog_daily') -> pull_catalog_daily;
  - AD-22: the legacy campaign_daily pull lands byte-identical on the shared fixture.
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

_CONNECTOR_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "linkedin-ads" / "connector.py"
)
_AD_ANALYTICS_URL = "https://api.linkedin.com/rest/adAnalytics"

# One campaign-grain element (source shape: costInLocalCurrency is a string, the
# pivot URN is materialised into campaign_id at parse time).
_ELEMENTS = {
    "elements": [
        {
            "costInLocalCurrency": "118.40",
            "impressions": 12040,
            "clicks": 640,
            "externalWebsiteConversions": 58,
            "leadGenerationMailContactInfoShares": 3,
            "dateRange": {
                "start": {"year": 2026, "month": 7, "day": 1},
                "end": {"year": 2026, "month": 7, "day": 1},
            },
            "pivotValues": ["urn:li:sponsoredCampaign:1234567"],
        }
    ],
    "paging": {},
}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_linkedin_catalog", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@pytest.fixture(scope="module")
def catalog():
    return json.loads(
        (_CONNECTOR_PATH.parent / "api_catalog.json").read_text(encoding="utf-8")
    )


def _selection(metrics, dimensions, catalog):
    """Build a resolved selection exactly as core would (source_fields from catalog)."""
    from core.catalog_contract import validate_selection

    resolved, issues = validate_selection(
        catalog, {"metrics": metrics, "dimensions": dimensions}
    )
    assert issues == [], issues
    return resolved


_HAS_CATALOG = _CONNECTOR_PATH.parent.joinpath("api_catalog.json").exists()
_skip_no_catalog = pytest.mark.skipif(
    not _HAS_CATALOG, reason="api_catalog.json not generated yet (orchestrator step)"
)


@_skip_no_catalog
@respx.mock
def test_catalog_pull_builds_fields_param_from_selection(
    connector, catalog, tmp_path, monkeypatch
):
    """selection {cost, impressions} -> fields param carries the SOURCE tokens
    (costInLocalCurrency, impressions) plus dateRange+pivotValues; pivot defaults
    to CAMPAIGN when no pivot_* dimension is selected."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat.duckdb"))

    route = respx.get(_AD_ANALYTICS_URL).mock(
        return_value=httpx.Response(200, json=_ELEMENTS)
    )
    selection = _selection(["cost", "impressions"], ["date", "campaign_id"], catalog)
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_1",
            selection=selection,
            account_id="502840441",
        )

    req = route.calls.last.request
    fields = req.url.params.get("fields", "")
    assert "costInLocalCurrency" in fields  # cost -> source token
    assert "impressions" in fields
    assert "dateRange" in fields and "pivotValues" in fields  # metadata always present
    assert req.url.params.get("pivot") == "CAMPAIGN"  # default pivot


@_skip_no_catalog
@respx.mock
def test_catalog_pull_uses_single_selected_pivot(
    connector, catalog, tmp_path, monkeypatch
):
    """A selected pivot_* dimension (pivot_MEMBER_COUNTRY_V2) becomes the SINGLE
    LinkedIn pivot param (MEMBER_COUNTRY_V2)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_pivot.duckdb"))

    route = respx.get(_AD_ANALYTICS_URL).mock(
        return_value=httpx.Response(200, json=_ELEMENTS)
    )
    selection = _selection(
        ["cost"], ["date", "pivot_MEMBER_COUNTRY_V2"], catalog
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_pivot",
            selection=selection,
            account_id="502840441",
        )
    assert route.calls.last.request.url.params.get("pivot") == "MEMBER_COUNTRY_V2"


@_skip_no_catalog
@respx.mock
def test_catalog_pull_chunks_wide_selection_and_merges_rows(
    connector, catalog, tmp_path, monkeypatch
):
    """A selection wider than the batch size issues >1 request; elements merge on
    the (dateRange + pivotValues) grain key so exactly one raw row lands."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_chunk.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    # Force a tiny batch so a small selection still chunks.
    monkeypatch.setattr(connector, "_CATALOG_FIELDS_BATCH", 4, raising=True)

    route = respx.get(_AD_ANALYTICS_URL).mock(
        return_value=httpx.Response(200, json=_ELEMENTS)
    )
    # 4 scalar metrics on top of the 2 structure metadata fields -> >=2 chunks at batch 4.
    selection = _selection(
        ["cost", "impressions", "clicks", "conversions"], ["date", "campaign_id"], catalog
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_chunk",
            selection=selection,
            account_id="502840441",
        )

    assert route.call_count >= 2, "wide selection must chunk into >=2 requests"
    # Despite multiple chunks, the merge collapses to ONE row per grain key.
    assert result["row_count"] == 1

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    n = con.execute(
        "SELECT COUNT(*) FROM raw_linkedin_ads_daily WHERE pull_id = 'pull_cat_chunk'"
    ).fetchone()[0]
    con.close()
    assert n == 1


@_skip_no_catalog
def test_catalog_pull_refuses_multiple_pivots_before_api(
    connector, catalog, monkeypatch
):
    """Selecting 2 pivot_* dimensions is refused with an invalid_request-class error
    BEFORE any HTTP call (LinkedIn adAnalytics is single-pivot)."""
    monkeypatch.setenv("LINKEDIN_ADS_ACCOUNT_ID", "502840441")
    from core.pull_errors import InvalidRequestError

    selection = _selection(
        ["cost"], ["pivot_MEMBER_COUNTRY_V2", "pivot_MEMBER_SENIORITY"], catalog
    )
    # No respx.mock: if the code hit the API this would raise ConnectError, not
    # InvalidRequestError — proving the refusal is pre-call.
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(InvalidRequestError) as exc:
            connector.pull_catalog_daily(
                connection_id="c",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="p",
                pull_id="pull_cat_bad",
                selection=selection,
                account_id="502840441",
            )
    assert exc.value.error_class == "invalid_request"
    msg = str(exc.value)
    assert "pivot_MEMBER_COUNTRY_V2" in msg and "pivot_MEMBER_SENIORITY" in msg


@_skip_no_catalog
@respx.mock
def test_catalog_pull_none_selection_uses_tier_core_default(
    connector, tmp_path, monkeypatch
):
    """A None selection resolves the catalog tier-core default (single CAMPAIGN
    pivot — no pivot explosion) and still pulls."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_def.duckdb"))

    route = respx.get(_AD_ANALYTICS_URL).mock(
        return_value=httpx.Response(200, json=_ELEMENTS)
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_def",
            selection=None,
            account_id="502840441",
        )
    assert route.called
    # The tier-core default carries at most one pivot -> every request uses CAMPAIGN
    # (no pivot explosion despite many core metrics fanning across chunks).
    assert all(
        call.request.url.params.get("pivot") == "CAMPAIGN" for call in route.calls
    )
    # cost is a tier-core scalar metric -> its source token appears in some chunk.
    all_fields = ",".join(call.request.url.params.get("fields", "") for call in route.calls)
    assert "costInLocalCurrency" in all_fields
    assert result["row_count"] == 1


@_skip_no_catalog
@respx.mock
def test_catalog_pull_lands_campaign_grain_row(
    connector, catalog, tmp_path, monkeypatch
):
    """The merged element lands the campaign grain: CAMPAIGN data_level, campaign_id
    materialised from the pivot URN, source metrics landed."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_land.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_AD_ANALYTICS_URL).mock(return_value=httpx.Response(200, json=_ELEMENTS))
    selection = _selection(
        ["cost", "impressions", "clicks", "conversions"], ["date", "campaign_id"], catalog
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_land",
            selection=selection,
            account_id="502840441",
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    row = con.execute(
        "SELECT data_level, campaign_id, costInLocalCurrency, impressions, clicks, "
        "externalWebsiteConversions FROM raw_linkedin_ads_daily "
        "WHERE pull_id = 'pull_cat_land'"
    ).fetchone()
    con.close()
    assert row == ("CAMPAIGN", "1234567", 118.40, 12040, 640, 58)


def test_catalog_daily_dispatch_resolves_pull_catalog_daily():
    """AI-58: get_module_pull_fn('linkedin-ads', 'catalog_daily') -> pull_catalog_daily."""
    import types

    from core.main import get_module_pull_fn

    li_mod = _import_connector()
    loaded = types.SimpleNamespace(
        name="linkedin-ads",
        connector_module=li_mod,
        manifest=json.loads(
            (_CONNECTOR_PATH.parent / "manifest.json").read_text(encoding="utf-8")
        ),
    )
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("linkedin-ads", "catalog_daily")
    assert fn is li_mod.pull_catalog_daily


@respx.mock
def test_ad22_legacy_campaign_pull_byte_identical(connector, tmp_path, monkeypatch):
    """AD-22: the legacy campaign_daily pull lands byte-identical rows on the shared
    fixture — catalog_driven did not perturb the exact_bundle path."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_ad22.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_AD_ANALYTICS_URL).mock(return_value=httpx.Response(200, json=_ELEMENTS))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_campaign_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_legacy",
            account_id="502840441",
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    row = con.execute(
        "SELECT data_level, campaign_id, costInLocalCurrency, impressions, clicks, "
        "externalWebsiteConversions, leadGenerationMailContactInfoShares "
        "FROM raw_linkedin_ads_daily WHERE pull_id = 'pull_legacy'"
    ).fetchone()
    con.close()
    # Legacy campaign grain: CAMPAIGN data_level, campaign_id from pivot URN.
    assert row == ("CAMPAIGN", "1234567", 118.40, 12040, 640, 58, 3)
