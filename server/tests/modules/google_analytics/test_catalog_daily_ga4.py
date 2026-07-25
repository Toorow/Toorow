"""Story 25.9 — catalog_driven execution tests for GA4 pull_catalog_daily.

respx-mocked (no real GA4 API — the live probe is deferred, AI-13). Mirrors the
meta-ads 25.8 reference (test_catalog_daily_meta.py) in spirit. Proves:
  - the runReport dimensions[]+metrics[] are built from the selection;
  - a selection wider than GA4's 10-metric / 9-dimension ceiling CHUNKS into >1
    runReport and merges the per-chunk rows on the dimension-key tuple;
  - a realtime-only dimension (minute) is refused BEFORE the API call
    (invalid_request — the declared incompatibility the section map can't express);
  - a None selection resolves the catalog tier-core default and still pulls;
  - AI-58: get_module_pull_fn('google-analytics', 'catalog_daily') resolves
    pull_catalog_daily;
  - AD-22: the legacy standard_daily pull lands byte-identical rows on a shared
    fixture (catalog_driven did not perturb the exact_bundle path).
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
    Path(__file__).parents[4]
    / "server"
    / "modules"
    / "google-analytics"
    / "connector.py"
)
_GA4_RUNREPORT_URL = (
    "https://analyticsdata.googleapis.com/v1beta/properties/TEST123:runReport"
)


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_ga4_catalog", _CONNECTOR_PATH)
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


# ---------------------------------------------------------------------------
# Mock helpers — one GA4 runReport response echoing the request's dims+metrics.
# ---------------------------------------------------------------------------


def _make_response(dim_names, met_names, dim_values, met_values):
    """A single-row GA4 runReport payload for the given headers/values."""
    return {
        "dimensionHeaders": [{"name": n} for n in dim_names],
        "metricHeaders": [{"name": n, "type": "TYPE_INTEGER"} for n in met_names],
        "rows": [
            {
                "dimensionValues": [{"value": v} for v in dim_values],
                "metricValues": [{"value": v} for v in met_values],
            }
        ],
    }


@respx.mock
def test_catalog_pull_builds_dimensions_and_metrics_from_selection(
    connector, catalog, tmp_path, monkeypatch
):
    """selection {sessions,conversions | date,device_category,country} builds a
    runReport carrying exactly those api-name dimensions + metrics."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat.duckdb"))

    resp = _make_response(
        ["date", "deviceCategory", "country"],
        ["sessions", "conversions"],
        ["20260701", "mobile", "France"],
        ["100", "5"],
    )
    route = respx.post(_GA4_RUNREPORT_URL).mock(
        return_value=httpx.Response(200, json=resp)
    )
    selection = _selection(
        ["sessions", "conversions"], ["date", "device_category", "country"], catalog
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_1",
            selection=selection,
            property_id="TEST123",
        )

    assert route.called
    body = json.loads(route.calls.last.request.content)
    dim_names = {d["name"] for d in body["dimensions"]}
    met_names = {m["name"] for m in body["metrics"]}
    assert dim_names == {"date", "deviceCategory", "country"}
    assert met_names == {"sessions", "conversions"}
    # date is always the time key.
    assert body["dimensions"][0]["name"] == "date"


@respx.mock
def test_catalog_pull_chunks_wide_selection_and_merges_rows(
    connector, catalog, tmp_path, monkeypatch
):
    """A selection wider than the 10-metric ceiling issues >1 runReport; the
    per-chunk rows MERGE on the (date, breakdown-value) grain key so the SAME
    grain's metrics from different chunks collapse into one series set."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_chunk.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    # 12 metrics -> 2 metric chunks (>10 ceiling). Single breakdown -> 1 dim chunk.
    metrics = [
        "sessions", "active_users", "conversions", "screen_page_views",
        "totalUsers", "newUsers", "engagedSessions", "eventCount",
        "bounceRate", "engagementRate", "totalRevenue", "transactions",
    ]
    selection = _selection(metrics, ["date", "device_category"], catalog)

    # Echo each chunk's requested dims+metrics so both chunks carry the SAME grain
    # (date=20260701, deviceCategory=mobile) -> they must merge to ONE grain row.
    def _echo(request: httpx.Request):
        body = json.loads(request.content)
        dim_names = [d["name"] for d in body["dimensions"]]
        met_names = [m["name"] for m in body["metrics"]]
        dim_values = ["20260701" if n == "date" else "mobile" for n in dim_names]
        met_values = ["7"] * len(met_names)
        return httpx.Response(
            200, json=_make_response(dim_names, met_names, dim_values, met_values)
        )

    route = respx.post(_GA4_RUNREPORT_URL).mock(side_effect=_echo)

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_chunk",
            selection=selection,
            property_id="TEST123",
        )

    assert route.call_count >= 2, "wide selection (>10 metrics) must chunk into >=2 runReports"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    # One LONG row per (grain) x (selected metric) x (breakdown). One grain, one
    # breakdown (device_category), 12 metrics -> exactly 12 long rows, all merged
    # onto the single (2026-07-01, mobile) grain.
    n = con.execute(
        "SELECT COUNT(*) FROM raw_ga4_catalog_daily WHERE pull_id = 'pull_cat_chunk'"
    ).fetchone()[0]
    distinct_grain = con.execute(
        "SELECT COUNT(DISTINCT (date || '|' || breakdown_value)) "
        "FROM raw_ga4_catalog_daily WHERE pull_id = 'pull_cat_chunk'"
    ).fetchone()[0]
    n_metrics = con.execute(
        "SELECT COUNT(DISTINCT metric_name) FROM raw_ga4_catalog_daily "
        "WHERE pull_id = 'pull_cat_chunk'"
    ).fetchone()[0]
    con.close()
    assert result["row_count"] == n
    assert distinct_grain == 1, "chunk rows must merge onto ONE (date, breakdown) grain"
    assert n_metrics == 12, "all 12 selected metrics land despite the chunking"


def test_catalog_pull_refuses_realtime_dimension_before_api(
    connector, catalog, monkeypatch
):
    """A realtime-only dimension (minute) is refused with invalid_request BEFORE any
    HTTP call — the declared incompatibility the TIME section map cannot express."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    from core.pull_errors import InvalidRequestError

    selection = _selection(["sessions"], ["date", "minute"], catalog)
    # No respx.mock: had the code called the API this would raise ConnectError,
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
                property_id="TEST123",
            )
    assert exc.value.error_class == "invalid_request"
    assert "minute" in str(exc.value)


@respx.mock
def test_catalog_pull_none_selection_uses_tier_core_default(
    connector, tmp_path, monkeypatch
):
    """A None selection resolves the catalog tier-core default and still pulls
    (the default is wide -> it chunks; we only assert it runs and lands rows)."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_def.duckdb"))

    def _echo(request: httpx.Request):
        body = json.loads(request.content)
        dim_names = [d["name"] for d in body["dimensions"]]
        met_names = [m["name"] for m in body["metrics"]]
        dim_values = ["20260701" if n == "date" else "x" for n in dim_names]
        met_values = ["1"] * len(met_names)
        return httpx.Response(
            200, json=_make_response(dim_names, met_names, dim_values, met_values)
        )

    route = respx.post(_GA4_RUNREPORT_URL).mock(side_effect=_echo)
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_def",
            selection=None,
            property_id="TEST123",
        )
    assert route.called
    # sessions (a tier-core scalar metric) must be requested somewhere.
    all_metric_names = set()
    for call in route.calls:
        for m in json.loads(call.request.content)["metrics"]:
            all_metric_names.add(m["name"])
    assert "sessions" in all_metric_names
    assert result["row_count"] > 0


def test_catalog_daily_dispatch_resolves_pull_catalog_daily():
    """AI-58: get_module_pull_fn('google-analytics','catalog_daily') ->
    pull_catalog_daily."""
    import types

    from core.main import get_module_pull_fn

    ga4_mod = _import_connector()
    loaded = types.SimpleNamespace(
        name="google-analytics",
        connector_module=ga4_mod,
        manifest=json.loads(
            (_CONNECTOR_PATH.parent / "manifest.json").read_text(encoding="utf-8")
        ),
    )
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("google-analytics", "catalog_daily")
    assert fn is ga4_mod.pull_catalog_daily


@respx.mock
def test_ad22_legacy_standard_pull_byte_identical(connector, tmp_path, monkeypatch):
    """AD-22: the legacy standard_daily pull lands byte-identical rows on a shared
    fixture — catalog_driven did not perturb the exact_bundle path or its raw table."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_ad22.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    resp = _make_response(
        ["date", "deviceCategory", "country"],
        ["sessions", "activeUsers", "conversions"],
        ["20260701", "mobile", "France"],
        ["100", "80", "5"],
    )
    respx.post(_GA4_RUNREPORT_URL).mock(return_value=httpx.Response(200, json=resp))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_legacy",
            property_id="TEST123",
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    row = con.execute(
        "SELECT date, device_category, country, sessions, active_users, conversions "
        "FROM raw_ga4_standard_daily WHERE pull_id = 'pull_legacy'"
    ).fetchone()
    # The catalog raw table must NOT exist / carry rows for the legacy pull.
    catalog_tables = con.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_name = 'raw_ga4_catalog_daily'"
    ).fetchone()[0]
    con.close()
    # France -> FR via the dim_country alias map (transform), same as pre-25.9.
    assert row == ("2026-07-01", "mobile", "FR", 100, 80, 5)
    assert catalog_tables == 0, "legacy pull must not create the catalog raw table"
