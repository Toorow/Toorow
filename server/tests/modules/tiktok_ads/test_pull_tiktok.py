"""Tests for the TikTok Ads pull() functions — Story 15.2 (Epic 15).

Uses respx to mock the TikTok Marketing API /report/integrated/get endpoint. No test
contacts the real API. Real TikTok E2E is a human gate (BLOCKED Phase B, AI-08/AI-13:
advertiser OAuth + real quota).
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
    Path(__file__).parents[4] / "server" / "modules" / "tiktok-ads" / "connector.py"
)

_API_BASE = "https://business-api.tiktok.com/open_api/v1.3"
_REPORT_URL = f"{_API_BASE}/report/integrated/get/"
_ADVERTISER_ID = "6800000000000000001"


def _api_row(campaign_id: str, day: str, *, spend: str, impressions: str,
             clicks: str, conversions: str, name: str = "Campagne") -> dict:
    """Build a mock /report/integrated/get row (dimensions + metrics envelope)."""
    return {
        "dimensions": {"campaign_id": campaign_id, "stat_time_day": f"{day} 00:00:00"},
        "metrics": {
            "campaign_name": name,
            "spend": spend,
            "impressions": impressions,
            "clicks": clicks,
            "conversions": conversions,
        },
    }


def _envelope(rows: list[dict], *, page: int = 1, total_page: int = 1) -> dict:
    """Wrap rows in TikTok's {code, message, data:{list, page_info}} envelope."""
    return {
        "code": 0,
        "message": "OK",
        "data": {
            "list": rows,
            "page_info": {"page": page, "total_page": total_page, "total_number": len(rows)},
        },
    }


_BASIC_RESPONSE = _envelope([
    _api_row("1770000000000000001", "2026-07-01", spend="128.50", impressions="25700",
             clicks="411", conversions="23", name="Notoriete FR"),
    _api_row("1770000000000000002", "2026-07-01", spend="64.00", impressions="9800",
             clicks="176", conversions="12", name="Conversion Web"),
    _api_row("1770000000000000003", "2026-07-02", spend="210.90", impressions="31200",
             clicks="624", conversions="41", name="Retargeting"),
])


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_tiktok_pull", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@respx.mock
def test_pull_calls_report_endpoint_with_window_and_level(connector, tmp_path, monkeypatch):
    """pull hits /report/integrated/get with BASIC + AUCTION_CAMPAIGN + date window."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt.duckdb"))

    route = respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(200, json=_BASIC_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_tt",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-tt",
            pull_id="pull_tt_p1",
            advertiser_id=_ADVERTISER_ID,
        )

    assert route.called
    req = route.calls.last.request
    assert req.url.params["advertiser_id"] == _ADVERTISER_ID
    assert req.url.params["report_type"] == "BASIC"
    assert req.url.params["data_level"] == "AUCTION_CAMPAIGN"
    assert req.url.params["start_date"] == "2026-07-01"
    assert req.url.params["end_date"] == "2026-07-03"
    # dimensions carry the entity id + stat_time_day (serialized JSON array).
    dims = json.loads(req.url.params["dimensions"])
    assert "campaign_id" in dims and "stat_time_day" in dims


@respx.mock
def test_pull_lands_rows_with_cost_rename_and_conversions(connector, tmp_path, monkeypatch):
    """Rows land in raw_tiktok_ads_daily; spend -> cost via transform, conversions kept."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "tt_rows.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json=_BASIC_RESPONSE))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_tt",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-tt",
            pull_id="pull_tt_p2",
            advertiser_id=_ADVERTISER_ID,
        )

    assert result["row_count"] == 3

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT campaign_id, date, spend, conversions FROM raw_tiktok_ads_daily "
        "WHERE pull_id = 'pull_tt_p2' ORDER BY campaign_id"
    ).fetchall()
    con.close()

    assert len(rows) == 3
    by_campaign = {r[0]: r for r in rows}
    # spend renamed to canonical 'cost' -> lands in the 'spend' raw column post-transform.
    assert by_campaign["1770000000000000001"][2] == pytest.approx(128.50)
    # conversions preserved (CLAIMED metric, kept distinct from GA4/Meta).
    assert by_campaign["1770000000000000003"][3] == 41
    # stat_time_day -> day-grain date (timestamp truncated).
    assert by_campaign["1770000000000000001"][1] == "2026-07-01"


@respx.mock
def test_pull_normalizes_not_set_tokens_to_null(connector, tmp_path, monkeypatch):
    """TikTok placeholder tokens ('-', '') for a name are normalized to NULL (not '(not set)')."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "tt_notset.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    resp = _envelope([
        _api_row("1770000000000000009", "2026-07-01", spend="10.0", impressions="100",
                 clicks="2", conversions="0", name="-"),
    ])
    respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json=resp))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_tt_ns", advertiser_id=_ADVERTISER_ID,
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    name = con.execute(
        "SELECT campaign_name FROM raw_tiktok_ads_daily WHERE pull_id = 'pull_tt_ns'"
    ).fetchone()[0]
    con.close()
    assert name is None, "placeholder '-' campaign_name must become NULL"


@respx.mock
def test_pull_follows_bounded_pagination(connector, tmp_path, monkeypatch):
    """pull advances page-by-page up to total_page (multi-page, multi-date)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "tt_page.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    page1 = _envelope(
        [_api_row("1770000000000000001", "2026-07-01", spend="100.0", impressions="1000",
                  clicks="10", conversions="1")],
        page=1, total_page=2,
    )
    page2 = _envelope(
        [_api_row("1770000000000000002", "2026-07-02", spend="200.0", impressions="2000",
                  clicks="20", conversions="2")],
        page=2, total_page=2,
    )
    route = respx.get(_REPORT_URL).mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-03",
            project_id="jean-tt", pull_id="pull_tt_pg", advertiser_id=_ADVERTISER_ID,
        )

    assert route.call_count == 2, "page 2 must be fetched exactly once"
    assert result["row_count"] == 2
    # page param incremented on the second call.
    assert route.calls[1].request.url.params["page"] == "2"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    dates = sorted(
        r[0] for r in con.execute(
            "SELECT DISTINCT date FROM raw_tiktok_ads_daily WHERE pull_id = 'pull_tt_pg'"
        ).fetchall()
    )
    con.close()
    assert dates == ["2026-07-01", "2026-07-02"]


@respx.mock
def test_pull_pagination_guard_stops_on_non_progressing_total_page(
    connector, tmp_path, monkeypatch
):
    """A server claiming total_page huge but never advancing must be bounded (anti-loop).

    Here every page reports total_page=999 but the connector's own page counter advances;
    the seen-page guard + _MAX_PAGES cap mean the loop terminates -- it never hangs. We
    shrink _MAX_PAGES to keep the test fast and assert we stopped at the cap.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_loop.duckdb"))
    monkeypatch.setattr(connector, "_MAX_PAGES", 3)

    def _always_more(request: httpx.Request):
        return httpx.Response(200, json=_envelope(
            [_api_row("1770000000000000001", "2026-07-01", spend="1.0", impressions="10",
                      clicks="1", conversions="0")],
            page=1, total_page=999,
        ))

    route = respx.get(_REPORT_URL).mock(side_effect=_always_more)

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_tt_loop", advertiser_id=_ADVERTISER_ID,
        )

    # Bounded: the loop stops at _MAX_PAGES (3), never spinning forever.
    assert route.call_count == 3


@respx.mock
def test_pull_raises_rate_limit_error_on_429(connector, tmp_path, monkeypatch):
    """pull raises RateLimitError('tiktok-ads', ...) on a 429 (sliding-window overflow)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_429.duckdb"))

    respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3"}, json={"code": 40100})
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull(
                connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
                project_id="jean-tt", pull_id="pull_tt_429", advertiser_id=_ADVERTISER_ID,
            )

    assert exc_info.value.platform == "tiktok-ads"
    assert exc_info.value.retry_after == 3


@respx.mock
def test_pull_logical_error_code_raises_runtime_error(connector, tmp_path, monkeypatch):
    """A non-zero TikTok 'code' in a 200 body (e.g. 40002 bad params) raises RuntimeError."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_logic.duckdb"))

    respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(200, json={"code": 40002, "message": "invalid params"})
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RuntimeError, match="40002"):
            connector.pull(
                connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
                project_id="jean-tt", pull_id="pull_tt_logic", advertiser_id=_ADVERTISER_ID,
            )


@respx.mock
def test_pull_missing_code_raises_runtime_error(connector, tmp_path, monkeypatch):
    """review-15-2 F-4: a 200 body WITHOUT a 'code' field is NOT an implicit success --
    it is an unexpected/malformed response and must raise RuntimeError (no silent pass)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_nocode.duckdb"))

    # Body has data but no 'code' key -> must fail explicitly (old guard treated it as success).
    respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(200, json={"message": "OK", "data": {"list": []}})
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RuntimeError, match="missing 'code'"):
            connector.pull(
                connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
                project_id="jean-tt", pull_id="pull_tt_nocode", advertiser_id=_ADVERTISER_ID,
            )


@respx.mock
def test_pull_non_200_error_raises_runtime_error(connector, tmp_path, monkeypatch):
    """pull raises RuntimeError on non-200, non-429 responses."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_500.duckdb"))

    respx.get(_REPORT_URL).mock(return_value=httpx.Response(500, json={"msg": "boom"}))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RuntimeError, match="500"):
            connector.pull(
                connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
                project_id="jean-tt", pull_id="pull_tt_500", advertiser_id=_ADVERTISER_ID,
            )


@respx.mock
def test_pull_advertiser_id_env_fallback(connector, tmp_path, monkeypatch):
    """advertiser_id resolves from TIKTOK_ADS_ADVERTISER_ID when not passed (queue path)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_env.duckdb"))
    monkeypatch.setenv("TIKTOK_ADS_ADVERTISER_ID", _ADVERTISER_ID)

    route = respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(200, json=_envelope([]))
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_tt_env",
        )
    assert route.called
    assert route.calls.last.request.url.params["advertiser_id"] == _ADVERTISER_ID


def test_pull_requires_advertiser_id(connector, monkeypatch):
    """pull raises a clear ValueError when neither arg nor env var is set."""
    monkeypatch.delenv("TIKTOK_ADS_ADVERTISER_ID", raising=False)
    with pytest.raises(ValueError, match="TIKTOK_ADS_ADVERTISER_ID"):
        connector.pull(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-03",
            project_id="jean-tt", pull_id="pull_tt_noadv",
        )


@respx.mock
def test_pull_token_not_stored(connector, tmp_path, monkeypatch, caplog):
    """AD-3: the token must not appear in the return value or any log."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_tok.duckdb"))

    respx.get(_REPORT_URL).mock(return_value=httpx.Response(200, json=_BASIC_RESPONSE))

    secret = "super-secret-tiktok-token-98765"
    with caplog.at_level(logging.DEBUG):
        with patch("core.nango_client.get_fresh_token", return_value=secret):
            result = connector.pull(
                connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-03",
                project_id="jean-tt", pull_id="pull_tt_tok", advertiser_id=_ADVERTISER_ID,
            )

    assert secret not in str(result)
    for record in caplog.records:
        assert secret not in record.getMessage()


@respx.mock
def test_pull_respects_nango_provider_and_header(connector, tmp_path, monkeypatch):
    """AD-3: token via nango_client.get_fresh_token(provider='tiktok-ads'), Access-Token header."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_auth.duckdb"))

    captured: list[dict] = []

    def _capture(request: httpx.Request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_envelope([]))

    respx.get(_REPORT_URL).mock(side_effect=_capture)

    with patch("core.nango_client.get_fresh_token", return_value="bearer-tt-token") as mock_nango:
        connector.pull(
            connection_id="conn_tt_auth", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_tt_auth", advertiser_id=_ADVERTISER_ID,
        )
        mock_nango.assert_called_once_with("conn_tt_auth", provider="tiktok-ads")

    # TikTok uses the Access-Token header (not Bearer, not the Google direct flow AD-21).
    assert captured[0].get("access-token") == "bearer-tt-token"


def test_golden_pull_through_parse_and_transform(connector):
    """review-15-2 F-2 (AI-54 honest): golden_pull.json is the REAL API payload shape
    ({dimensions, metrics} like /report/integrated/get/). The conformance path runs it
    through connector._parse_report_row (structural extraction + data_level stamp) THEN
    connector.transform (spend -> cost rename from the manifest) and asserts it produces
    expected_facts.json. This genuinely exercises _parse_report_row (which the respx mocks
    also exercise) AND the rename map -- the fixture is NOT hand-written post-parse."""
    fixtures = _TOOROW_PATH.parent / "tests" / "fixtures"
    golden = json.loads((fixtures / "golden_pull.json").read_text(encoding="utf-8"))
    expected = json.loads((fixtures / "expected_facts.json").read_text(encoding="utf-8"))
    # API-shaped rows -> parse (source names + data_level) -> transform (canonical names).
    parsed = [connector._parse_report_row(row, "campaign_daily") for row in golden]
    assert connector.transform(parsed) == expected
    # The placeholder '-' campaign_name became NULL during parse (AI-05 (not set)).
    assert parsed[-1]["campaign_name"] is None


@respx.mock
def test_ai58_dispatch_adgroup_uses_adgroup_data_level(connector, tmp_path, monkeypatch):
    """AI-58: pull_adgroup_daily requests data_level=AUCTION_ADGROUP."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_ag.duckdb"))

    route = respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(200, json=_envelope([]))
    )
    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_adgroup_daily(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_tt_ag", advertiser_id=_ADVERTISER_ID,
        )
    assert route.calls.last.request.url.params["data_level"] == "AUCTION_ADGROUP"
    dims = json.loads(route.calls.last.request.url.params["dimensions"])
    assert "adgroup_id" in dims


@respx.mock
def test_ai58_dispatch_ad_uses_ad_data_level(connector, tmp_path, monkeypatch):
    """AI-58: pull_ad_daily requests data_level=AUCTION_AD."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_ad.duckdb"))

    route = respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(200, json=_envelope([]))
    )
    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_ad_daily(
            connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
            project_id="jean-tt", pull_id="pull_tt_ad", advertiser_id=_ADVERTISER_ID,
        )
    assert route.calls.last.request.url.params["data_level"] == "AUCTION_AD"


def test_dispatch_resolves_per_profile_pull_fn():
    """AI-58: get_module_pull_fn('tiktok-ads', 'adgroup_daily') resolves pull_adgroup_daily."""
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
        default_fn = get_module_pull_fn("tiktok-ads")
        ag_fn = get_module_pull_fn("tiktok-ads", "adgroup_daily")
        ad_fn = get_module_pull_fn("tiktok-ads", "ad_daily")
    assert default_fn is tt_mod.pull
    assert ag_fn is tt_mod.pull_adgroup_daily
    assert ad_fn is tt_mod.pull_ad_daily


# ---------------------------------------------------------------------------
# Story 25.7 (AC3): typed errors via the manifest error_map (HTTP raise site).
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_401_code_40105_raises_auth_expired_with_payload_preserved(
    connector, tmp_path, monkeypatch
):
    """Story 25.7: a 401 with TikTok logical code 40105 (token expired/incorrect)
    routes through classify_http_error + the manifest error_map to auth_expired,
    with the provider payload (log_id etc.) preserved as evidence.

    Proves the connector loads and forwards the manifest error_map at the HTTP
    raise site -- not just the pure-HTTP class (which would already be auth_expired
    for a 401, but this asserts the code 40105 discriminator + payload survival).
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_401.duckdb"))

    tt_error = {
        "code": 40105,
        "message": "Access token is incorrect or has been revoked.",
        "request_id": "REQ_401_TT",
    }
    respx.get(_REPORT_URL).mock(return_value=httpx.Response(401, json=tt_error))

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull(
                connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
                project_id="jean-tt", pull_id="pull_tt_401", advertiser_id=_ADVERTISER_ID,
            )

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    # Parsed TikTok payload preserved intact (request_id is the evidence we keep).
    assert err.provider_payload["request_id"] == "REQ_401_TT"
    assert err.provider_payload["code"] == 40105


@respx.mock
def test_pull_403_code_40300_raises_permission_denied_via_error_map(
    connector, tmp_path, monkeypatch
):
    """Story 25.7: a 403 with TikTok logical code 40300 (no permission) routes to
    permission_denied via the manifest error_map."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "tt_403.duckdb"))

    respx.get(_REPORT_URL).mock(
        return_value=httpx.Response(403, json={"code": 40300, "message": "no permission"})
    )

    from core.pull_errors import PermissionDeniedError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(PermissionDeniedError) as exc_info:
            connector.pull(
                connection_id="conn_tt", date_from="2026-07-01", date_to="2026-07-01",
                project_id="jean-tt", pull_id="pull_tt_403", advertiser_id=_ADVERTISER_ID,
            )
    assert exc_info.value.error_class == "permission_denied"
    assert exc_info.value.provider_payload["code"] == 40300


def test_manifest_error_map_shape():
    """Story 25.7 (AC3): manifest.json carries a well-formed TikTok error_map.

    Keys are "<status>:<code>"; values are canonical pull_errors classes.
    """
    from core.pull_errors import ERROR_CLASSES

    manifest = json.loads(
        (_TOOROW_PATH.parent / "manifest.json").read_text(encoding="utf-8")
    )
    error_map = manifest.get("error_map")
    assert isinstance(error_map, dict) and error_map, "manifest must declare error_map"

    for key, cls in error_map.items():
        status, _, code = key.partition(":")
        assert status.isdigit() and code, f"malformed error_map key: {key!r}"
        assert cls in ERROR_CLASSES, f"{cls!r} is not a canonical error class"

    # The story's mandated minimum coverage.
    assert error_map["401:40105"] == "auth_expired"
    assert error_map["401:40001"] == "auth_revoked"
    assert error_map["403:40300"] == "permission_denied"
    assert error_map["400:40002"] == "invalid_request"


# ---------------------------------------------------------------------------
# Story 25.7 stitch: discover_accounts (oauth2/advertiser/get, typed errors)
# ---------------------------------------------------------------------------

_ADVERTISER_LIST_URL = f"{_API_BASE}/oauth2/advertiser/get/"


@respx.mock
def test_discover_accounts_single_level_hierarchy(connector):
    """discover_accounts returns the flat advertiser list as [{id, label}]."""
    respx.get(_ADVERTISER_LIST_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "code": 0,
                "message": "OK",
                "data": {
                    "list": [
                        {"advertiser_id": "6800000000000000001", "advertiser_name": "Acme FR"},
                        {"advertiser_id": "6800000000000000002", "advertiser_name": ""},
                    ]
                },
            },
        )
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        accounts = connector.discover_accounts("conn_tt_disc")

    assert accounts == [
        {"id": "6800000000000000001", "label": "Acme FR"},
        # empty name falls back to the id
        {"id": "6800000000000000002", "label": "6800000000000000002"},
    ]


@respx.mock
def test_discover_accounts_401_code_40105_raises_auth_expired(connector):
    """A 401 from oauth2/advertiser/get raises the typed auth_expired via error_map,
    payload preserved."""
    respx.get(_ADVERTISER_LIST_URL).mock(
        return_value=httpx.Response(
            401, json={"code": 40105, "message": "token revoked", "request_id": "DISC401"}
        )
    )

    from core.pull_errors import ConnectorError

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(ConnectorError) as excinfo:
            connector.discover_accounts("conn_tt_disc")

    assert excinfo.value.error_class == "auth_expired"
    assert excinfo.value.provider_payload["request_id"] == "DISC401"


@respx.mock
def test_discover_accounts_429_raises_rate_limit_error(connector):
    """A 429 from oauth2/advertiser/get raises RateLimitError('tiktok-ads')."""
    respx.get(_ADVERTISER_LIST_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"}, json={"code": 40100})
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.discover_accounts("conn_tt_disc")

    assert exc_info.value.platform == "tiktok-ads"
    assert exc_info.value.retry_after == 7


# ---------------------------------------------------------------------------
# Real TikTok E2E — BLOCKED Phase B (AI-08/AI-13): advertiser OAuth + live quota.
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Real TikTok E2E — BLOCKED Phase B (advertiser OAuth, AI-08/AI-13)")
def test_pull_e2e_with_real_tiktok():  # pragma: no cover
    """Placeholder for the live TikTok Marketing API smoke test (Phase B)."""
