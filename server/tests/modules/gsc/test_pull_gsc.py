"""Tests for the GSC pull() function — Story 6.2 (AC4, AC15).

Uses respx to mock the GSC Search Analytics API. No test contacts the real API.
Real GSC E2E is a human gate (HG-B: Google OAuth scope extension for webmasters.readonly).
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

_TOOROW_PATH = Path(__file__).parents[4] / "server" / "modules" / "gsc" / "connector.py"

_SITE_URL = "https://example.com/"
_SITE_URL_ENCODED = "https%3A%2F%2Fexample.com%2F"
_GSC_API_URL = (
    f"https://searchconsole.googleapis.com/webmasters/v3/sites/"
    f"{_SITE_URL_ENCODED}/searchAnalytics/query"
)

_GSC_RESPONSE = {
    "rows": [
        {
            "keys": ["https://example.com/blog/", "fra", "DESKTOP"],
            "clicks": 42,
            "impressions": 850,
            "ctr": 0.049,
            "position": 7.3,
        },
        {
            "keys": ["https://example.com/docs/", "fra", "DESKTOP"],
            "clicks": 15,
            "impressions": 200,
            "ctr": 0.075,
            "position": 3.1,
        },
        {
            "keys": ["https://example.com/pricing/", "gbr", "MOBILE"],
            "clicks": 8,
            "impressions": 180,
            "ctr": 0.044,
            "position": 5.4,
        },
    ],
    "responseAggregationType": "byPage",
}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_gsc_pull", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@respx.mock
def test_pull_calls_gsc_api_with_correct_params(connector, tmp_path, monkeypatch):
    """pull hits the searchAnalytics/query endpoint with correct URL + rowLimit."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc.duckdb"))

    route = respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json=_GSC_RESPONSE))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-gsc",
            pull_id="pull_gsc_p1",
            dimensions=["page", "country", "device"],
        )

    assert route.called, "GSC searchAnalytics/query endpoint must have been called"
    request = route.calls.last.request
    body = request.read()
    import json

    payload = json.loads(body)
    assert payload["rowLimit"] == 25000
    assert "page" in payload["dimensions"]
    assert payload["startDate"] == "2026-07-01"
    assert payload["endDate"] == "2026-07-03"


@respx.mock
def test_pull_returns_row_count(connector, tmp_path, monkeypatch):
    """pull returns row_count matching the number of GSC rows landed."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_rows.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json=_GSC_RESPONSE))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_test",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-gsc",
            pull_id="pull_gsc_p2",
            dimensions=["page", "country", "device"],
        )

    assert result["pull_id"] == "pull_gsc_p2"
    assert result["row_count"] == 3
    assert result["date_from"] == "2026-07-01"
    assert result["date_to"] == "2026-07-03"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT page, average_position, impressions FROM raw_gsc_daily "
        "WHERE pull_id = 'pull_gsc_p2' ORDER BY page"
    ).fetchall()
    con.close()
    assert len(rows) == 3
    # Verify average_position was stored (not summed)
    positions = {r[0]: r[1] for r in rows}
    assert positions["https://example.com/blog/"] == pytest.approx(7.3)
    assert positions["https://example.com/docs/"] == pytest.approx(3.1)


@respx.mock
def test_pull_regex_filter_passed_through(connector, tmp_path, monkeypatch):
    """pull passes dimension_filter as dimensionFilterGroups in API request body."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_filter.duckdb"))

    route = respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json={"rows": []}))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-gsc",
            pull_id="pull_gsc_filter",
            dimensions=["page"],
            dimension_filter={"type": "REGEX", "expression": "^/blog/"},
        )

    import json

    body = json.loads(route.calls.last.request.read())
    assert "dimensionFilterGroups" in body
    assert body["dimensionFilterGroups"][0]["filters"][0]["expression"] == "^/blog/"


@respx.mock
def test_pull_token_not_stored(connector, tmp_path, monkeypatch, caplog):
    """HG-5 / AD-3: the token must not appear in the return value or any log."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_tok.duckdb"))

    respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json=_GSC_RESPONSE))

    secret = "super-secret-gsc-token-54321"
    with caplog.at_level(logging.DEBUG):
        with patch("core.nango_client.get_fresh_token", return_value=secret):
            result = connector.pull(
                connection_id="conn_test",
                site_url=_SITE_URL,
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-gsc",
                pull_id="pull_gsc_tok",
                dimensions=["page", "country", "device"],
            )

    assert secret not in str(result)
    for record in caplog.records:
        assert secret not in record.getMessage()


@respx.mock
def test_pull_row_limit_25000(connector, tmp_path, monkeypatch):
    """AC15: rowLimit must be 25000 in all GSC API calls (connector-requirements.md)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_limit.duckdb"))

    route = respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json={"rows": []}))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-gsc",
            pull_id="pull_gsc_limit",
            dimensions=["page"],
        )

    import json

    body = json.loads(route.calls.last.request.read())
    assert body["rowLimit"] == 25000


@respx.mock
def test_pull_site_url_url_encoded(connector, tmp_path, monkeypatch):
    """pull URL-encodes the site_url in the request path (slashes → %2F)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_url.duckdb"))

    # The site_url with slashes must produce an encoded URL in the path
    route = respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json={"rows": []}))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            site_url="https://example.com/",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-gsc",
            pull_id="pull_gsc_url",
            dimensions=["page"],
        )

    assert route.called
    request_url = str(route.calls.last.request.url)
    # site_url slashes must be encoded in the path
    assert "https%3A%2F%2Fexample.com%2F" in request_url


@respx.mock
def test_pull_respects_nango_auth(connector, tmp_path, monkeypatch):
    """AD-3: token obtained via nango_client.get_fresh_token(provider='gsc')."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_auth.duckdb"))

    captured: list[dict] = []

    def _capture(request: httpx.Request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_GSC_RESPONSE)

    respx.post(_GSC_API_URL).mock(side_effect=_capture)

    with patch("core.nango_client.get_fresh_token", return_value="bearer-gsc-token") as mock_nango:
        connector.pull(
            connection_id="conn_gsc_auth",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-gsc",
            pull_id="pull_gsc_auth",
            dimensions=["page"],
        )
        # Verify provider="gsc" was used (AD-3: token obtained via Nango)
        mock_nango.assert_called_once_with("conn_gsc_auth", provider="gsc")

    assert captured[0].get("authorization") == "Bearer bearer-gsc-token"


@respx.mock
def test_pull_raises_rate_limit_error_on_429(connector, tmp_path, monkeypatch):
    """pull raises RateLimitError('gsc', ...) on a 429 response."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_429.duckdb"))

    respx.post(_GSC_API_URL).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "30"}, json={"error": {"code": 429}}
        )
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                site_url=_SITE_URL,
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-gsc",
                pull_id="pull_gsc_429",
                dimensions=["page"],
            )

    assert exc_info.value.platform == "gsc"
    assert exc_info.value.retry_after == 30


@respx.mock
def test_pull_non_429_error_raises_runtime_error(connector, tmp_path, monkeypatch):
    """pull raises RuntimeError on non-200, non-429 responses."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_500.duckdb"))

    respx.post(_GSC_API_URL).mock(return_value=httpx.Response(500, json={"error": {"code": 500}}))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RuntimeError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                site_url=_SITE_URL,
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="jean-gsc",
                pull_id="pull_gsc_500",
                dimensions=["page"],
            )
    assert "500" in str(exc_info.value)


@respx.mock
def test_pull_device_normalized_to_lowercase(connector, tmp_path, monkeypatch):
    """Device values from GSC (MOBILE/DESKTOP/TABLET) are normalized to lowercase."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_device.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    gsc_response_devices = {
        "rows": [
            {
                "keys": ["https://example.com/blog/", "DESKTOP"],
                "clicks": 42,
                "impressions": 850,
                "ctr": 0.049,
                "position": 7.3,
            },
            {
                "keys": ["https://example.com/blog/", "MOBILE"],
                "clicks": 28,
                "impressions": 620,
                "ctr": 0.045,
                "position": 9.2,
            },
            {
                "keys": ["https://example.com/blog/", "TABLET"],
                "clicks": 8,
                "impressions": 180,
                "ctr": 0.044,
                "position": 5.4,
            },
        ]
    }

    site_encoded = "https%3A%2F%2Fexample.com%2F"
    api_url = f"https://searchconsole.googleapis.com/webmasters/v3/sites/{site_encoded}/searchAnalytics/query"
    respx.post(api_url).mock(return_value=httpx.Response(200, json=gsc_response_devices))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            site_url="https://example.com/",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-gsc",
            pull_id="pull_gsc_device",
            dimensions=["page", "device"],
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    devices = set(
        r[0]
        for r in con.execute(
            "SELECT device FROM raw_gsc_daily WHERE pull_id = 'pull_gsc_device'"
        ).fetchall()
    )
    con.close()
    # All device values must be lowercase
    assert devices == {"desktop", "mobile", "tablet"}


# ---------------------------------------------------------------------------
# review-epic-10 CRITICAL-B: query_page_daily profile shim + dispatch resolution.
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_query_page_daily_uses_query_page_dimensions(connector, tmp_path, monkeypatch):
    """CRITICAL-B: the profile shim pins dimensions to ['date','query','page'] at 25k rows.

    Without this shim the profile-aware dispatch fell through to pull() with the default
    ['page'] dimensions, so the joint (query, page) grain cannibalisation needs was never
    ingested. 'date' MUST lead the dimensions (manifest contract): GSC only returns the
    date key when requested and _parse_gsc_row maps keys positionally — without it every
    row lands with date='' and the whole window collapses into one degenerate date bucket
    (review-10-6 F-1). site_url is resolved from the GSC_SITE_URL env fallback (the queue
    dispatch does not pass site_url).
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_qp.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    qp_response = {
        "rows": [
            {
                "keys": ["2026-07-01", "chaussures running", "https://example.com/run/"],
                "clicks": 10,
                "impressions": 200,
                "ctr": 0.05,
                "position": 4.2,
            },
            {
                "keys": ["2026-07-02", "chaussures running", "https://example.com/run/"],
                "clicks": 12,
                "impressions": 240,
                "ctr": 0.05,
                "position": 4.0,
            },
            {
                "keys": ["2026-07-02", "chaussures running", "https://example.com/trail/"],
                "clicks": 3,
                "impressions": 90,
                "ctr": 0.033,
                "position": 9.1,
            },
        ],
        "responseAggregationType": "byPage",
    }
    route = respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json=qp_response))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        # Dispatch contract: no site_url/dimensions passed (queue.py passes only these 5).
        result = connector.pull_query_page_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-gsc",
            pull_id="pull_gsc_qp",
        )

    assert route.called
    import json

    body = json.loads(route.calls.last.request.read())
    assert body["dimensions"] == ["date", "query", "page"]
    assert body["rowLimit"] == 25000
    assert result["pull_id"] == "pull_gsc_qp"
    assert result["row_count"] == 3

    # Daily grain preserved end-to-end: landed rows carry their per-day dates (never '').
    import duckdb

    con = duckdb.connect(db_path)
    landed = con.execute(
        "SELECT DISTINCT date FROM raw_gsc_daily WHERE pull_id = 'pull_gsc_qp' ORDER BY date"
    ).fetchall()
    con.close()
    assert [d[0] for d in landed] == ["2026-07-01", "2026-07-02"]


def test_pull_query_page_daily_requires_site_url(connector, monkeypatch):
    """Shim raises a clear ValueError when neither arg nor GSC_SITE_URL env is set."""
    monkeypatch.delenv("GSC_SITE_URL", raising=False)
    with pytest.raises(ValueError, match="GSC_SITE_URL"):
        connector.pull_query_page_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-gsc",
            pull_id="pull_gsc_qp_nourl",
        )


def test_query_page_daily_dispatch_resolves_profile_fn():
    """CRITICAL-B: get_module_pull_fn('gsc','query_page_daily') resolves the shim, not pull().

    The profile-aware dispatch keys off the ``pull_<profile_id>`` naming contract. This guards
    that the shim exists and is picked up (was ABSENT -> fell through to pull() with ['page']).
    """
    import types

    from core.main import get_module_pull_fn

    gsc_mod = _import_connector()
    assert callable(getattr(gsc_mod, "pull_query_page_daily", None)), (
        "pull_query_page_daily must exist on the GSC connector for dispatch"
    )

    # Simulate the loaded-module registry entry the dispatch reads (name + connector_module).
    loaded = types.SimpleNamespace(
        name="gsc",
        connector_module=gsc_mod,
        manifest=json.loads((_TOOROW_PATH.parent / "manifest.json").read_text(encoding="utf-8")),
    )
    with patch("core.main._loaded_modules", [loaded]):
        default_fn = get_module_pull_fn("gsc")
        profile_fn = get_module_pull_fn("gsc", profile_id="query_page_daily")

    assert default_fn is gsc_mod.pull
    assert profile_fn is gsc_mod.pull_query_page_daily
    assert profile_fn is not default_fn


# ---------------------------------------------------------------------------
# GSC full API coverage: pagination, type, dataState, aggregationType, filters,
# searchAppearance, profile shims + dispatch.
# ---------------------------------------------------------------------------


def _mk_row(page: str, clicks: int = 1, impressions: int = 10, position: float = 5.0):
    return {
        "keys": [page],
        "clicks": clicks,
        "impressions": impressions,
        "ctr": clicks / impressions,
        "position": position,
    }


@respx.mock
def test_pull_paginates_until_short_page(connector, tmp_path, monkeypatch):
    """Completeness: pull loops startRow until the API returns a short page.

    The API caps each response at rowLimit and signals NOTHING on truncation —
    a single request silently drops every row past the limit. With row_limit=2
    and 5 rows server-side, pull must issue 3 requests (startRow 0, 2, 4) and
    land all 5 rows.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_pages.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    pages = [
        [_mk_row("https://example.com/a/"), _mk_row("https://example.com/b/")],
        [_mk_row("https://example.com/c/"), _mk_row("https://example.com/d/")],
        [_mk_row("https://example.com/e/")],
    ]
    bodies: list[dict] = []

    def _paged(request: httpx.Request):
        body = json.loads(request.read())
        bodies.append(body)
        page_idx = len(bodies) - 1
        return httpx.Response(200, json={"rows": pages[page_idx]})

    respx.post(_GSC_API_URL).mock(side_effect=_paged)

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_test",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-gsc",
            pull_id="pull_gsc_paged",
            dimensions=["page"],
            row_limit=2,
        )

    assert [b["startRow"] for b in bodies] == [0, 2, 4]
    assert result["row_count"] == 5
    assert result["pages"] == 3
    assert result["truncated"] is False

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    landed = con.execute(
        "SELECT COUNT(*) FROM raw_gsc_daily WHERE pull_id = 'pull_gsc_paged'"
    ).fetchone()[0]
    con.close()
    assert landed == 5


@respx.mock
def test_pull_sends_type_web_by_default_and_stamps_search_type(connector, tmp_path, monkeypatch):
    """The 'type' parameter is sent explicitly (web default) and stamped on raw rows."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_type.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    route = respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json=_GSC_RESPONSE))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-gsc",
            pull_id="pull_gsc_type",
            dimensions=["page", "country", "device"],
        )

    body = json.loads(route.calls.last.request.read())
    assert body["type"] == "web"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    types = set(
        r[0]
        for r in con.execute(
            "SELECT DISTINCT search_type FROM raw_gsc_daily WHERE pull_id = 'pull_gsc_type'"
        ).fetchall()
    )
    con.close()
    assert types == {"web"}


@respx.mock
def test_pull_discover_daily_shim(connector, tmp_path, monkeypatch):
    """Discover data is only reachable via type=discover — the shim pins it."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_disc.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    discover_response = {
        "rows": [
            {
                "keys": ["2026-07-01", "https://example.com/article/"],
                "clicks": 120,
                "impressions": 4000,
                "ctr": 0.03,
                "position": 1.0,
            },
        ]
    }
    route = respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json=discover_response))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_discover_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-gsc",
            pull_id="pull_gsc_disc",
        )

    body = json.loads(route.calls.last.request.read())
    assert body["type"] == "discover"
    assert body["dimensions"] == ["date", "page"]
    assert result["row_count"] == 1

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    row = con.execute(
        "SELECT search_type, date, page FROM raw_gsc_daily WHERE pull_id = 'pull_gsc_disc'"
    ).fetchone()
    con.close()
    assert row == ("discover", "2026-07-01", "https://example.com/article/")


@respx.mock
def test_pull_data_state_and_metadata(connector, tmp_path, monkeypatch):
    """dataState is passed through; response metadata (fresh-data boundary) is returned."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_ds.duckdb"))

    route = respx.post(_GSC_API_URL).mock(
        return_value=httpx.Response(
            200,
            json={"rows": [], "metadata": {"first_incomplete_date": "2026-07-20"}},
        )
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_test",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-21",
            project_id="jean-gsc",
            pull_id="pull_gsc_ds",
            dimensions=["date", "page"],
            data_state="all",
        )

    body = json.loads(route.calls.last.request.read())
    assert body["dataState"] == "all"
    assert result["metadata"] == {"first_incomplete_date": "2026-07-20"}


@respx.mock
def test_pull_aggregation_type_passthrough(connector, tmp_path, monkeypatch):
    """aggregationType is passed through when provided (absent otherwise = API auto)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_agg.duckdb"))

    route = respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json={"rows": []}))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-gsc",
            pull_id="pull_gsc_agg",
            dimensions=["page"],
            aggregation_type="byPage",
        )
        connector.pull(
            connection_id="conn_test",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-gsc",
            pull_id="pull_gsc_agg2",
            dimensions=["page"],
        )

    first = json.loads(route.calls[0].request.read())
    second = json.loads(route.calls[1].request.read())
    assert first["aggregationType"] == "byPage"
    assert "aggregationType" not in second


@respx.mock
def test_pull_multiple_filters_single_group(connector, tmp_path, monkeypatch):
    """A list of filters lands in ONE dimensionFilterGroups group (AND semantics)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gsc_flt.duckdb"))

    route = respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json={"rows": []}))

    filters = [
        {"dimension": "page", "operator": "includingRegex", "expression": "^/blog/"},
        {"dimension": "country", "operator": "equals", "expression": "fra"},
    ]
    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            site_url=_SITE_URL,
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-gsc",
            pull_id="pull_gsc_flt",
            dimensions=["page"],
            dimension_filter=filters,
        )

    body = json.loads(route.calls.last.request.read())
    assert len(body["dimensionFilterGroups"]) == 1
    assert body["dimensionFilterGroups"][0]["filters"] == filters


@respx.mock
def test_pull_search_appearance_daily_per_day_loop(connector, tmp_path, monkeypatch):
    """searchAppearance cannot combine with other dims: one single-day query per date,
    date stamped on each landed row via static_fields."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gsc_sa.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    bodies: list[dict] = []

    def _sa_response(request: httpx.Request):
        bodies.append(json.loads(request.read()))
        return httpx.Response(
            200,
            json={
                "rows": [
                    {
                        "keys": ["RICHRESULT"],
                        "clicks": 5,
                        "impressions": 100,
                        "ctr": 0.05,
                        "position": 3.0,
                    }
                ]
            },
        )

    respx.post(_GSC_API_URL).mock(side_effect=_sa_response)

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_search_appearance_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="jean-gsc",
            pull_id="pull_gsc_sa",
        )

    # One API call per day, searchAppearance alone, single-day window.
    assert len(bodies) == 2
    for body, day in zip(bodies, ["2026-07-01", "2026-07-02"]):
        assert body["dimensions"] == ["searchAppearance"]
        assert body["startDate"] == day
        assert body["endDate"] == day
    assert result["row_count"] == 2

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT date, search_appearance FROM raw_gsc_daily "
        "WHERE pull_id = 'pull_gsc_sa' ORDER BY date"
    ).fetchall()
    con.close()
    assert rows == [("2026-07-01", "RICHRESULT"), ("2026-07-02", "RICHRESULT")]


@respx.mock
@pytest.mark.parametrize(
    ("shim_name", "expected_dims"),
    [
        ("pull_page_daily", ["date", "page"]),
        ("pull_country_daily", ["date", "country"]),
        ("pull_device_daily", ["date", "device"]),
    ],
)
def test_daily_profile_shims_pin_date_grain(
    connector, tmp_path, monkeypatch, shim_name, expected_dims
):
    """review-10-6 F-1 closed for ALL daily profiles: 'date' leads the dimensions.

    Without these shims the dispatch fell through to pull() with ['page'] — no date
    key, so every row landed with date='' and the window collapsed into one bucket.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / f"gsc_{shim_name}.duckdb"))
    monkeypatch.setenv("GSC_SITE_URL", _SITE_URL)

    route = respx.post(_GSC_API_URL).mock(return_value=httpx.Response(200, json={"rows": []}))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        getattr(connector, shim_name)(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-gsc",
            pull_id=f"pull_{shim_name}",
        )

    body = json.loads(route.calls.last.request.read())
    assert body["dimensions"] == expected_dims
    assert body["type"] == "web"


def test_all_manifest_profiles_have_dispatch_shims():
    """Every profile declared in the manifest resolves to its pull_<profile_id> shim
    via the dispatch, so no profile can silently fall through to the default pull()."""
    import types

    from core.main import get_module_pull_fn

    gsc_mod = _import_connector()
    manifest = json.loads((_TOOROW_PATH.parent / "manifest.json").read_text(encoding="utf-8"))
    profile_ids = [p["id"] for p in manifest["report_profiles"]]
    assert (
        len(profile_ids) == 11
    )  # 10 exact_bundle (searchanalytics coverage) + catalog_daily (25.9)

    loaded = types.SimpleNamespace(name="gsc", connector_module=gsc_mod, manifest=manifest)
    with patch("core.main._loaded_modules", [loaded]):
        for pid in profile_ids:
            shim = getattr(gsc_mod, f"pull_{pid}", None)
            assert callable(shim), f"missing shim pull_{pid}"
            resolved = get_module_pull_fn("gsc", profile_id=pid)
            assert resolved is shim, f"dispatch for {pid} did not resolve pull_{pid}"
            assert resolved is not gsc_mod.pull


# ---------------------------------------------------------------------------
# Real GSC E2E — human gate (HG-B). Skipped unless credentials are provided.
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Real GSC E2E — human gate HG-B (webmasters.readonly scope)")
def test_pull_e2e_with_real_gsc():  # pragma: no cover
    """Placeholder for the live GSC Search Analytics API smoke test."""
