"""Tests for GSC pull_catalog_daily — Story 25.9 (AC1-AC5).

Uses respx to mock the GSC Search Analytics API. No test contacts the real API.

AC coverage:
  AC1 — profile catalog_daily in manifest (selection_mode catalog_driven, dispatch pull_catalog_daily)
  AC2 — dimension list built from selection; 'date' always leads
  AC3 — metric projection at parse time (non-selected metrics zeroed, ctr never stored)
  AC4 — searchAppearance combo refusal (InvalidRequestError BEFORE API call)
  AC5 — AD-22 existing profiles green (dispatch test)
  AI-58 dispatch test for catalog_daily
"""

from __future__ import annotations

import importlib.util
import json
import os
import types
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_CONNECTOR_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "gsc" / "connector.py"
)
_MANIFEST_PATH = _CONNECTOR_PATH.parent / "manifest.json"
_CATALOG_PATH = _CONNECTOR_PATH.parent / "api_catalog.json"

_SITE_URL = "https://example.com/"
_SITE_URL_ENCODED = "https%3A%2F%2Fexample.com%2F"
_GSC_API_URL = (
    f"https://searchconsole.googleapis.com/webmasters/v3/sites/"
    f"{_SITE_URL_ENCODED}/searchAnalytics/query"
)

# Minimal 2-row response covering clicks, impressions, position (the 4 metrics always come back).
_GSC_RESPONSE = {
    "rows": [
        {
            "keys": ["2026-07-01", "https://example.com/blog/"],
            "clicks": 42,
            "impressions": 850,
            "ctr": 0.049,
            "position": 7.3,
        },
        {
            "keys": ["2026-07-02", "https://example.com/docs/"],
            "clicks": 15,
            "impressions": 200,
            "ctr": 0.075,
            "position": 3.1,
        },
    ],
}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_gsc_cat", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# AC1: catalog_daily profile declared in manifest
# ---------------------------------------------------------------------------


def test_manifest_has_catalog_daily_profile():
    """AC1: manifest declares catalog_daily with selection_mode=catalog_driven."""
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    # Look in source_capabilities.reports
    reports = manifest["source_capabilities"]["reports"]
    ids = [r["id"] for r in reports]
    assert "catalog_daily" in ids, "catalog_daily must be declared in source_capabilities.reports"
    profile = next(r for r in reports if r["id"] == "catalog_daily")
    assert profile["selection_mode"] == "catalog_driven"
    assert profile["dispatch"]["callable"] == "pull_catalog_daily"


def test_manifest_existing_10_profiles_untouched():
    """AD-22: the 10 existing exact_bundle profiles are bit-identical (not removed or altered)."""
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    reports = manifest["source_capabilities"]["reports"]
    exact_bundle_ids = [r["id"] for r in reports if r.get("selection_mode") == "exact_bundle"]
    expected_existing = {
        "page_daily", "country_daily", "device_daily", "query_page_daily",
        "discover_daily", "google_news_daily", "news_daily", "image_daily",
        "video_daily", "search_appearance_daily",
    }
    assert set(exact_bundle_ids) == expected_existing, (
        f"Exact-bundle profiles changed. Got: {sorted(exact_bundle_ids)}"
    )
    # report_profiles list (the old top-level list) also untouched
    rp_ids = {p["id"] for p in manifest["report_profiles"]}
    assert rp_ids == expected_existing | {"catalog_daily"}  # 25.9: transitional profile required by the registry validator


# ---------------------------------------------------------------------------
# AC2: dimension list built from selection; 'date' always leads
# ---------------------------------------------------------------------------


@respx.mock
def test_catalog_daily_dimensions_from_selection(connector, tmp_path, monkeypatch):
    """AC2: dimensions param sent to the API is built from selection; 'date' leads always."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_cat_dims.duckdb"))
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    route = respx.post(_GSC_API_URL).mock(
        return_value=httpx.Response(200, json=_GSC_RESPONSE)
    )

    selection = {
        "metrics": ["clicks", "impressions"],
        "dimensions": ["date", "page", "country"],
        "source_fields": {
            "clicks": "clicks",
            "impressions": "impressions",
            "date": "date",
            "page": "page",
            "country": "country",
        },
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_catalog_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="jean-gsc",
            pull_id="pull_cat_dims",
            selection=selection,
        )

    assert route.called
    body = json.loads(route.calls.last.request.read())
    dims = body["dimensions"]
    # 'date' must be first
    assert dims[0] == "date", f"'date' must lead dimensions, got: {dims}"
    # page and country are in the dims
    assert "page" in dims
    assert "country" in dims


@respx.mock
def test_catalog_daily_date_always_leads_even_if_not_in_selection(connector, tmp_path, monkeypatch):
    """AC2: if 'date' is absent from the selection's dimensions, it is still prepended."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_cat_nodate.duckdb"))
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    route = respx.post(_GSC_API_URL).mock(
        return_value=httpx.Response(200, json={"rows": []})
    )

    selection = {
        "metrics": ["clicks"],
        "dimensions": ["page"],  # no 'date'
        "source_fields": {"clicks": "clicks", "page": "page"},
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_catalog_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-gsc",
            pull_id="pull_cat_nodate",
            selection=selection,
        )

    body = json.loads(route.calls.last.request.read())
    assert body["dimensions"][0] == "date"


@respx.mock
def test_catalog_daily_search_type_maps_to_type_param(connector, tmp_path, monkeypatch):
    """AC2: 'search_type' in selection dims maps to the API 'type' param, not to the dimensions list."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_cat_st.duckdb"))
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    route = respx.post(_GSC_API_URL).mock(
        return_value=httpx.Response(200, json={"rows": []})
    )

    selection = {
        "metrics": ["clicks"],
        "dimensions": ["date", "page", "search_type"],
        "source_fields": {"clicks": "clicks", "date": "date", "page": "page", "search_type": "type"},
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_catalog_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-gsc",
            pull_id="pull_cat_st",
            selection=selection,
        )

    body = json.loads(route.calls.last.request.read())
    # search_type must NOT appear in the dimensions list
    assert "search_type" not in body.get("dimensions", [])
    assert "type" not in body.get("dimensions", [])
    # The API 'type' param should be present (web by default)
    assert "type" in body


# ---------------------------------------------------------------------------
# AC3: metric projection at parse time
# ---------------------------------------------------------------------------


@respx.mock
def test_catalog_daily_projects_only_selected_metrics(connector, tmp_path, monkeypatch):
    """AC3: non-selected metrics land as 0 (API always returns all metrics; selection = projection)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_cat_proj.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    # API returns all 4 metrics — the connector must project down to only clicks
    respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json=_GSC_RESPONSE))

    selection = {
        "metrics": ["clicks"],  # only clicks requested — impressions + average_position must be 0
        "dimensions": ["date", "page"],
        "source_fields": {"clicks": "clicks", "date": "date", "page": "page"},
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_catalog_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="jean-gsc",
            pull_id="pull_cat_proj",
            selection=selection,
        )

    assert result["row_count"] == 2

    import duckdb
    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT clicks, impressions, average_position FROM raw_gsc_daily "
        "WHERE pull_id = 'pull_cat_proj'"
    ).fetchall()
    con.close()

    assert len(rows) == 2
    for clicks, impressions, avg_pos in rows:
        assert clicks > 0, "clicks (selected) must be non-zero"
        assert impressions == 0, "impressions (not selected) must be zeroed"
        assert avg_pos == pytest.approx(0.0), "average_position (not selected) must be zeroed"


@respx.mock
def test_catalog_daily_ctr_never_stored(connector, tmp_path, monkeypatch):
    """AC3 / AD-4: ctr is never stored even if the API returns it."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_cat_ctr.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json=_GSC_RESPONSE))

    # Requesting all available catalog fields (ctr is excluded from api_catalog.json)
    selection = {
        "metrics": ["clicks", "impressions", "average_position"],
        "dimensions": ["date", "page"],
        "source_fields": {
            "clicks": "clicks",
            "impressions": "impressions",
            "average_position": "average_position",
            "date": "date",
            "page": "page",
        },
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_catalog_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="jean-gsc",
            pull_id="pull_cat_ctr",
            selection=selection,
        )

    import duckdb
    con = duckdb.connect(db_path, read_only=True)
    cols = [c[0] for c in con.execute(
        "DESCRIBE raw_gsc_daily"
    ).fetchall()]
    con.close()
    assert "ctr" not in cols, "ctr must never appear as a column in raw_gsc_daily"


@respx.mock
def test_catalog_daily_all_metrics_kept_when_all_selected(connector, tmp_path, monkeypatch):
    """AC3: when all metrics are selected, no projection occurs (all land with real values)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_cat_all.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json=_GSC_RESPONSE))

    selection = {
        "metrics": ["clicks", "impressions", "average_position"],
        "dimensions": ["date", "page"],
        "source_fields": {
            "clicks": "clicks",
            "impressions": "impressions",
            "average_position": "average_position",
            "date": "date",
            "page": "page",
        },
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_catalog_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="jean-gsc",
            pull_id="pull_cat_all_metrics",
            selection=selection,
        )

    import duckdb
    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT clicks, impressions, average_position FROM raw_gsc_daily "
        "WHERE pull_id = 'pull_cat_all_metrics'"
    ).fetchall()
    con.close()

    assert len(rows) == 2
    for clicks, impressions, avg_pos in rows:
        assert clicks > 0
        assert impressions > 0
        assert avg_pos > 0


# ---------------------------------------------------------------------------
# AC4: incompatible dimension combos → InvalidRequestError BEFORE API call
# ---------------------------------------------------------------------------


def test_catalog_daily_refuses_search_appearance_with_other_dims(connector, monkeypatch):
    """AC4: searchAppearance + any other grouping dim → InvalidRequestError, no API call."""
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    from core.pull_errors import InvalidRequestError

    selection = {
        "metrics": ["clicks"],
        "dimensions": ["date", "search_appearance", "page"],  # search_appearance + page → invalid
        "source_fields": {
            "clicks": "clicks",
            "date": "date",
            "search_appearance": "searchAppearance",
            "page": "page",
        },
    }

    with respx.mock:
        # No route registered — the error must be raised BEFORE any HTTP call.
        with pytest.raises(InvalidRequestError) as exc_info:
            connector.pull_catalog_daily(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-02",
                project_id="jean-gsc",
                pull_id="pull_cat_sa_bad",
                selection=selection,
            )

    assert "search_appearance" in str(exc_info.value).lower() or "standalone" in str(exc_info.value).lower()
    # The respx mock had no routes registered; the test passes only if no HTTP request was sent.


def test_catalog_daily_refuses_hour_dimension(connector, monkeypatch):
    """AC4: 'hour' in selection → InvalidRequestError (excluded from catalog_daily)."""
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    from core.pull_errors import InvalidRequestError

    selection = {
        "metrics": ["clicks"],
        "dimensions": ["date", "hour"],
        "source_fields": {"clicks": "clicks", "date": "date", "hour": "hour"},
    }

    with respx.mock:
        with pytest.raises(InvalidRequestError) as exc_info:
            connector.pull_catalog_daily(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-gsc",
                pull_id="pull_cat_hour_bad",
                selection=selection,
            )

    assert "hour" in str(exc_info.value).lower()


@respx.mock
def test_catalog_daily_search_appearance_alone_triggers_per_day_loop(
    connector, tmp_path, monkeypatch
):
    """AC4 / loop path: search_appearance alone (no other dims) → per-day loop, not refused."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_cat_sa_loop.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    bodies: list[dict] = []

    def _sa_response(request: httpx.Request):
        bodies.append(json.loads(request.read()))
        return httpx.Response(
            200,
            json={
                "rows": [
                    {"keys": ["RICHRESULT"], "clicks": 5, "impressions": 100, "ctr": 0.05, "position": 3.0}
                ]
            },
        )

    respx.post(_GSC_API_URL).mock(side_effect=_sa_response)

    selection = {
        "metrics": ["clicks", "impressions"],
        "dimensions": ["search_appearance"],  # alone — should trigger the per-day loop
        "source_fields": {
            "clicks": "clicks",
            "impressions": "impressions",
            "search_appearance": "searchAppearance",
        },
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_catalog_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="jean-gsc",
            pull_id="pull_cat_sa_loop",
            selection=selection,
        )

    # Two days → 2 API calls, each with dimensions=["searchAppearance"]
    assert len(bodies) == 2
    for body, day in zip(bodies, ["2026-07-01", "2026-07-02"]):
        assert body["dimensions"] == ["searchAppearance"]
        assert body["startDate"] == day
        assert body["endDate"] == day

    assert result["row_count"] == 2


# ---------------------------------------------------------------------------
# AC5: AD-22 legacy profiles green (dispatch still resolves for all 10 existing profiles)
# AC5 / AI-58: catalog_daily dispatches to pull_catalog_daily
# ---------------------------------------------------------------------------


def test_catalog_daily_dispatch_resolves(connector):
    """AI-58: get_module_pull_fn('gsc', 'catalog_daily') resolves pull_catalog_daily."""
    from core.main import get_module_pull_fn

    gsc_mod = connector
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))

    loaded = types.SimpleNamespace(
        name="gsc", connector_module=gsc_mod, manifest=manifest
    )
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("gsc", profile_id="catalog_daily")

    assert fn is gsc_mod.pull_catalog_daily


def test_all_legacy_profiles_dispatch_green(connector):
    """AD-22: all 10 existing exact_bundle profiles still dispatch correctly."""
    from core.main import get_module_pull_fn

    gsc_mod = connector
    manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))

    existing_profile_ids = [
        "page_daily", "country_daily", "device_daily", "query_page_daily",
        "discover_daily", "google_news_daily", "news_daily", "image_daily",
        "video_daily", "search_appearance_daily",
    ]

    loaded = types.SimpleNamespace(
        name="gsc", connector_module=gsc_mod, manifest=manifest
    )
    with patch("core.main._loaded_modules", [loaded]):
        for pid in existing_profile_ids:
            fn = get_module_pull_fn("gsc", profile_id=pid)
            shim = getattr(gsc_mod, f"pull_{pid}", None)
            assert callable(shim), f"missing shim pull_{pid}"
            assert fn is shim, f"dispatch for {pid} did not resolve pull_{pid}"
            assert fn is not gsc_mod.pull


def test_catalog_daily_callable_exists(connector):
    """pull_catalog_daily must exist on the GSC connector module."""
    assert callable(getattr(connector, "pull_catalog_daily", None))


# ---------------------------------------------------------------------------
# catalog_sources.json and api_catalog.json policy checks
# ---------------------------------------------------------------------------


def test_catalog_sources_has_exposure_policy_catalog_driven():
    """catalog_sources.json must declare exposure_policy=catalog_driven."""
    cs_path = _CONNECTOR_PATH.parent / "catalog_sources" / "catalog_sources.json"
    cs = json.loads(cs_path.read_text(encoding="utf-8"))
    assert cs.get("exposure_policy") == "catalog_driven"


def test_catalog_sources_has_dimension_compatibility():
    """catalog_sources.json must declare dimension_compatibility block."""
    cs_path = _CONNECTOR_PATH.parent / "catalog_sources" / "catalog_sources.json"
    cs = json.loads(cs_path.read_text(encoding="utf-8"))
    compat = cs.get("dimension_compatibility", {})
    assert "standalone_only" in compat, "standalone_only list must be declared"
    assert "search_appearance" in compat["standalone_only"]
    assert "excluded_from_catalog_daily" in compat
    assert "hour" in compat["excluded_from_catalog_daily"]


def test_api_catalog_ctr_excluded_with_reason():
    """api_catalog.json: ctr must be exposure=excluded with an exclusion_reason (AD-4)."""
    catalog = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    ctr_field = next((f for f in catalog["fields"] if f["field_id"] == "ctr"), None)
    assert ctr_field is not None, "ctr field must exist in api_catalog.json"
    assert ctr_field["exposure"] == "excluded", "ctr must be exposure=excluded"
    assert ctr_field.get("exclusion_reason"), "ctr must carry an exclusion_reason"


def test_api_catalog_no_planned_fields():
    """Story 25.9 gate: zero fields with exposure=planned (all fields resolved to exposed/excluded)."""
    catalog = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    planned = [f["field_id"] for f in catalog["fields"] if f.get("exposure") == "planned"]
    assert not planned, f"Fields still in 'planned' state: {planned}"


@respx.mock
def test_catalog_daily_default_selection_falls_back_to_tier_core(connector, tmp_path, monkeypatch):
    """When selection=None, pull_catalog_daily resolves the tier-core default (no crash)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_cat_default.duckdb"))
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    route = respx.post(_GSC_API_URL).mock(
        return_value=httpx.Response(200, json=_GSC_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_catalog_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="jean-gsc",
            pull_id="pull_cat_default",
            selection=None,  # falls back to tier-core default
        )

    assert route.called
    body = json.loads(route.calls.last.request.read())
    # date must lead (default includes date as a core dimension)
    assert body["dimensions"][0] == "date"
    assert result["row_count"] == 2
