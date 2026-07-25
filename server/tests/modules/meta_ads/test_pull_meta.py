"""Tests for the Meta Ads pull() function — Story 3.6 (AC4, AC5, HG-3, HG-5).

Uses respx to mock the Meta Marketing (Graph) API. No test contacts the real
API. Real Meta E2E is a human gate (AI-07 / STANDARD_ACCESS_RAMP.md).
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
    Path(__file__).parents[4] / "server" / "modules" / "meta-ads" / "connector.py"
)

_META_INSIGHTS_RESPONSE = {
    "data": [
        {
            "date_start": "2026-07-01",
            "date_stop": "2026-07-01",
            "campaign_id": "23851234500010001",
            "campaign_name": "Summer Sale - Prospecting",
            "adset_id": "23851234500020001",
            "adset_name": "FR 25-44 Interests",
            "ad_id": "23851234500030001",
            "spend": "250.75",
            "impressions": "48200",
            "clicks": "1310",
            "conversions": "42",
        },
        {
            "date_start": "2026-07-01",
            "date_stop": "2026-07-01",
            "campaign_id": "23851234500010002",
            "campaign_name": "Summer Sale - Retargeting",
            "adset_id": "23851234500020002",
            "adset_name": "Cart Abandoners 30d",
            "ad_id": "23851234500030002",
            "spend": "118.40",
            "impressions": "12040",
            "clicks": "640",
            "actions": [
                {"action_type": "offsite_conversion.fb_pixel_purchase", "value": "58"},
                {"action_type": "link_click", "value": "640"},
            ],
        },
    ],
    "paging": {},
}

_META_INSIGHTS_URL = (
    "https://graph.facebook.com/v20.0/act_1234567890/insights"
)


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_meta_pull", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@respx.mock
def test_pull_calls_meta_api_with_correct_params(connector, tmp_path, monkeypatch):
    """pull hits the insights endpoint with act_<id> in path and correct fields param."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta.duckdb"))

    route = respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_META_INSIGHTS_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-meta",
            pull_id="pull_meta_p1",
            ad_account_id="1234567890",
        )

    assert route.called, "Meta insights endpoint must have been called"
    request = route.calls.last.request
    assert "act_1234567890/insights" in str(request.url)
    fields_param = request.url.params.get("fields", "")
    assert "spend" in fields_param
    assert "impressions" in fields_param
    assert "actions" in fields_param
    assert request.url.params.get("level") == "campaign"
    assert request.url.params.get("time_increment") == "1"


@respx.mock
def test_pull_returns_row_count(connector, tmp_path, monkeypatch):
    """pull returns row_count matching the number of insight rows landed."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "meta_rows.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_META_INSIGHTS_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-meta",
            pull_id="pull_meta_p2",
            ad_account_id="1234567890",
        )

    assert result["pull_id"] == "pull_meta_p2"
    assert result["row_count"] == 2
    assert result["date_from"] == "2026-07-01"
    assert result["date_to"] == "2026-07-03"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT campaign_id, cost_col FROM ("
        "  SELECT campaign_id, spend AS cost_col FROM raw_meta_ads_daily"
        "  WHERE pull_id = 'pull_meta_p2'"
        ") ORDER BY campaign_id"
    ).fetchall()
    # conversions parsed from actions[] for the second row (58)
    conv = con.execute(
        "SELECT conversions FROM raw_meta_ads_daily "
        "WHERE pull_id = 'pull_meta_p2' AND campaign_id = '23851234500010002'"
    ).fetchone()
    con.close()
    assert len(rows) == 2
    assert conv[0] == 58, "conversions must be summed from the pixel purchase action"


@respx.mock
def test_pull_raises_rate_limit_error_on_429(connector, tmp_path, monkeypatch):
    """pull raises RateLimitError('meta-ads', ...) on a 429 response (AC5, HG-4)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta_429.duckdb"))

    respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": "45"}, json={"error": {"code": 4}}
        )
    )

    from core.quota import RateLimitError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RateLimitError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-meta",
                pull_id="pull_meta_429",
                ad_account_id="1234567890",
            )

    assert exc_info.value.platform == "meta-ads"
    assert exc_info.value.retry_after == 45


@respx.mock
def test_pull_non_429_error_raises_runtime_error(connector, tmp_path, monkeypatch):
    """pull raises RuntimeError on non-200, non-429 responses."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta_500.duckdb"))

    respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(500, json={"error": {"code": 500}})
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(RuntimeError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-meta",
                pull_id="pull_meta_500",
                ad_account_id="1234567890",
            )
    assert "500" in str(exc_info.value)


@respx.mock
def test_pull_401_raises_auth_expired_with_payload_preserved(connector, tmp_path, monkeypatch):
    """Story 25.2: a 401 Graph response raises auth_expired with the payload preserved.

    Proves the connector routes non-200/non-429 through classify_http_error and that
    the parsed Graph error body survives as evidence on the typed error.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta_401.duckdb"))

    graph_error = {
        "error": {
            "message": "Error validating access token: Session has expired.",
            "type": "OAuthException",
            "code": 190,
            "fbtrace_id": "AbCdEf123456",
        }
    }
    respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(401, json=graph_error)
    )

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-meta",
                pull_id="pull_meta_401",
                ad_account_id="1234567890",
            )

    err = exc_info.value
    assert err.error_class == "auth_expired"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    # Parsed Graph payload preserved intact (fbtrace_id is the evidence we must keep).
    assert err.provider_payload["error"]["fbtrace_id"] == "AbCdEf123456"
    assert err.provider_payload["error"]["code"] == 190


@respx.mock
def test_pull_401_subcode_458_raises_auth_revoked_via_error_map(connector, tmp_path, monkeypatch):
    """Story 25.4 (AC5): a 401 with Graph error subcode 458 routes to auth_revoked.

    The classifier extracts error.error_subcode BEFORE error.code, and the
    manifest error_map keys "401:458" -> auth_revoked. This proves the connector
    loads and forwards the manifest error_map (not just the pure-HTTP class,
    which would be auth_expired for a 401). Payload (incl. fbtrace_id) preserved.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta_458.duckdb"))

    graph_error = {
        "error": {
            "message": "Error validating access token: The user has not authorized application.",
            "type": "OAuthException",
            "code": 190,
            "error_subcode": 458,
            "fbtrace_id": "Rev0k3dXYZ",
        }
    }
    respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(401, json=graph_error)
    )

    from core.pull_errors import AuthRevokedError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(AuthRevokedError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-meta",
                pull_id="pull_meta_458",
                ad_account_id="1234567890",
            )

    err = exc_info.value
    assert err.error_class == "auth_revoked"
    assert err.retryable is False
    assert err.user_action == "reconnect"
    assert err.provider_status == 401
    # subcode 458 was the discriminator; the full Graph body is preserved as evidence.
    assert err.provider_payload["error"]["error_subcode"] == 458
    assert err.provider_payload["error"]["fbtrace_id"] == "Rev0k3dXYZ"


@respx.mock
def test_pull_403_code_10_raises_permission_denied_via_error_map(connector, tmp_path, monkeypatch):
    """Story 25.4 (AC5): a 403 with Graph error code 10 routes to permission_denied."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta_403.duckdb"))

    graph_error = {
        "error": {
            "message": "Application does not have permission for this action",
            "type": "OAuthException",
            "code": 10,
            "fbtrace_id": "Perm10Denied",
        }
    }
    respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(403, json=graph_error)
    )

    from core.pull_errors import PermissionDeniedError

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with pytest.raises(PermissionDeniedError) as exc_info:
            connector.pull(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-meta",
                pull_id="pull_meta_403",
                ad_account_id="1234567890",
            )
    assert exc_info.value.error_class == "permission_denied"
    assert exc_info.value.provider_payload["error"]["fbtrace_id"] == "Perm10Denied"


def test_manifest_error_map_shape():
    """Story 25.4 (AC5): manifest.json carries a well-formed Meta error_map.

    Keys are "<status>:<code>"; values are canonical pull_errors classes. The
    190 auth subcodes are keyed on 401 because the classifier reads the subcode
    before the code.
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
    assert error_map["401:190"] == "auth_expired"
    assert error_map["401:458"] == "auth_revoked"
    assert error_map["403:10"] == "permission_denied"
    assert error_map["403:200"] == "permission_denied"
    assert error_map["400:100"] == "invalid_request"
    assert error_map["500:1"] == "provider_transient"
    assert error_map["500:2"] == "provider_transient"


@respx.mock
def test_pull_token_not_stored(connector, tmp_path, monkeypatch, caplog):
    """HG-5 / AD-3: the token must not appear in the return value or any log."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta_tok.duckdb"))

    respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_META_INSIGHTS_RESPONSE)
    )

    secret = "super-secret-meta-token-98765"
    with caplog.at_level(logging.DEBUG):
        with patch("core.nango_client.get_fresh_token", return_value=secret):
            result = connector.pull(
                connection_id="conn_test",
                date_from="2026-07-01",
                date_to="2026-07-03",
                project_id="jean-meta",
                pull_id="pull_meta_tok",
                ad_account_id="1234567890",
            )

    assert secret not in str(result)
    for record in caplog.records:
        assert secret not in record.getMessage()


@respx.mock
def test_pull_ad_account_id_from_env(connector, tmp_path, monkeypatch):
    """When ad_account_id is None, pull reads META_ADS_AD_ACCOUNT_ID env var."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta_env.duckdb"))
    monkeypatch.setenv("META_ADS_AD_ACCOUNT_ID", "1234567890")

    route = respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_META_INSIGHTS_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-meta",
            pull_id="pull_meta_env",
        )

    assert route.called
    assert "act_1234567890/insights" in str(route.calls.last.request.url)


def test_pull_missing_ad_account_id_raises(connector, monkeypatch):
    """pull raises ValueError when neither param nor env var is set."""
    monkeypatch.delenv("META_ADS_AD_ACCOUNT_ID", raising=False)
    with pytest.raises(ValueError, match="META_ADS_AD_ACCOUNT_ID"):
        connector.pull(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-meta",
            pull_id="pull_meta_noacct",
            ad_account_id=None,
        )


@respx.mock
def test_pull_accepts_act_prefixed_account_id(connector, tmp_path, monkeypatch):
    """pull normalizes an 'act_'-prefixed account id (no double prefix in URL)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta_prefix.duckdb"))

    route = respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_META_INSIGHTS_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-meta",
            pull_id="pull_meta_prefix",
            ad_account_id="act_1234567890",
        )

    assert route.called
    url = str(route.calls.last.request.url)
    assert "act_1234567890/insights" in url
    assert "act_act_" not in url


@respx.mock
def test_pull_authorization_header_used(connector, tmp_path, monkeypatch):
    """AD-3: pull sends Authorization: Bearer <token>."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta_auth.duckdb"))

    captured: list[dict] = []

    def _capture(request: httpx.Request):
        captured.append(dict(request.headers))
        return httpx.Response(200, json=_META_INSIGHTS_RESPONSE)

    respx.get(_META_INSIGHTS_URL).mock(side_effect=_capture)

    with patch("core.nango_client.get_fresh_token", return_value="fake-token-abc"):
        connector.pull(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-meta",
            pull_id="pull_meta_auth",
            ad_account_id="1234567890",
        )

    assert captured[0].get("authorization") == "Bearer fake-token-abc"


# ---------------------------------------------------------------------------
# review-15-9 F-1: data_level per grain + AI-58 per-profile dispatch.
# ---------------------------------------------------------------------------


@respx.mock
def test_default_pull_stamps_campaign_data_level(connector, tmp_path, monkeypatch):
    """Default pull() (campaign grain) stamps data_level='CAMPAIGN' on every raw row."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "meta_dl.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_META_INSIGHTS_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-meta",
            pull_id="pull_meta_dl",
            ad_account_id="1234567890",
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    levels = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT data_level FROM raw_meta_ads_daily "
            "WHERE pull_id = 'pull_meta_dl'"
        ).fetchall()
    }
    con.close()
    assert levels == {"CAMPAIGN"}, f"campaign grain must stamp CAMPAIGN, got: {levels}"


@respx.mock
def test_ai58_dispatch_adset_uses_adset_level_and_data_level(connector, tmp_path, monkeypatch):
    """AI-58: pull_adset_daily requests Insights level=adset and stamps data_level=ADSET."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "meta_as.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    route = respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_META_INSIGHTS_RESPONSE)
    )
    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_adset_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-meta",
            pull_id="pull_meta_as",
            ad_account_id="1234567890",
        )

    assert route.calls.last.request.url.params.get("level") == "adset"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    levels = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT data_level FROM raw_meta_ads_daily "
            "WHERE pull_id = 'pull_meta_as'"
        ).fetchall()
    }
    con.close()
    assert levels == {"ADSET"}


@respx.mock
def test_ai58_dispatch_creative_uses_ad_level(connector, tmp_path, monkeypatch):
    """AI-58: pull_creative_daily requests Insights level=ad (data_level=CREATIVE)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "meta_cr.duckdb"))

    route = respx.get(_META_INSIGHTS_URL).mock(
        return_value=httpx.Response(200, json=_META_INSIGHTS_RESPONSE)
    )
    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        connector.pull_creative_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-meta",
            pull_id="pull_meta_cr",
            ad_account_id="1234567890",
        )

    assert route.calls.last.request.url.params.get("level") == "ad"


def test_dispatch_resolves_per_profile_pull_fn():
    """AI-58: get_module_pull_fn('meta-ads', <profile>) resolves the per-grain pull fns."""
    import types

    from core.main import get_module_pull_fn

    meta_mod = _import_connector()
    loaded = types.SimpleNamespace(
        name="meta-ads",
        connector_module=meta_mod,
        manifest=json.loads(
            (_TOOROW_PATH.parent / "manifest.json").read_text(encoding="utf-8")
        ),
    )
    with patch("core.main._loaded_modules", [loaded]):
        default_fn = get_module_pull_fn("meta-ads")
        adset_fn = get_module_pull_fn("meta-ads", "adset_daily")
        creative_fn = get_module_pull_fn("meta-ads", "creative_daily")
    assert default_fn is meta_mod.pull
    assert adset_fn is meta_mod.pull_adset_daily
    assert creative_fn is meta_mod.pull_creative_daily


def test_multigrain_seed_reconciles_exactly():
    """review-15-9 F-1: campaign == sum(adsets) == sum(creatives) per (date, metric)."""
    import importlib.util

    gen_path = (
        Path(__file__).parents[4] / "server" / "modules" / "meta-ads" / "seeds"
        / "generate_meta_seed.py"
    )
    spec = importlib.util.spec_from_file_location("gen_meta_seed", gen_path)
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)

    rows = gen.generate_multigrain_rows(days=3)
    by_level: dict[str, dict] = {}
    for r in rows:
        acc = by_level.setdefault(r["data_level"], {"spend": 0.0, "conv": 0})
        acc["spend"] += r["spend"]
        acc["conv"] += r["conversions"]

    assert set(by_level) == {"CAMPAIGN", "ADSET", "CREATIVE"}
    # Adset and creative grains must roll up EXACTLY to the campaign grain.
    assert by_level["ADSET"]["conv"] == by_level["CAMPAIGN"]["conv"]
    assert by_level["CREATIVE"]["conv"] == by_level["CAMPAIGN"]["conv"]
    assert by_level["ADSET"]["spend"] == pytest.approx(by_level["CAMPAIGN"]["spend"])
    assert by_level["CREATIVE"]["spend"] == pytest.approx(by_level["CAMPAIGN"]["spend"])


# ---------------------------------------------------------------------------
# Real Meta E2E — human gate (AI-07). Skipped unless credentials are provided.
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason="Real Meta E2E — human gate per AI-07")
def test_pull_e2e_with_real_meta():  # pragma: no cover
    """Placeholder for the live Meta Marketing API smoke test (Jean's credentials)."""


# ---------------------------------------------------------------------------
# Story 25.5 stitch: discover_accounts (/me/adaccounts, paged, typed errors)
# ---------------------------------------------------------------------------

_META_ADACCOUNTS_URL = "https://graph.facebook.com/v20.0/me/adaccounts"


@respx.mock
def test_discover_accounts_flat_hierarchy_with_paging(connector):
    import httpx as _httpx

    page2_url = _META_ADACCOUNTS_URL + "?after=cursor2"
    # One route with sequential responses: respx matches the base URL regardless
    # of query string, so two separate routes would both resolve to page 1.
    respx.get(_META_ADACCOUNTS_URL).mock(
        side_effect=[
            _httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "act_111", "name": "Acme Ads", "account_status": 1},
                        {"id": "act_222", "name": "", "account_status": 1},
                    ],
                    "paging": {"next": page2_url},
                },
            ),
            _httpx.Response(
                200,
                json={"data": [{"id": "act_333", "name": "Third"}], "paging": {}},
            ),
        ]
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        accounts = connector.discover_accounts("conn_meta_1")

    assert accounts == [
        {"id": "act_111", "label": "Acme Ads"},
        {"id": "act_222", "label": "act_222"},  # empty name falls back to id
        {"id": "act_333", "label": "Third"},
    ]


@respx.mock
def test_discover_accounts_401_code_190_raises_auth_expired(connector):
    respx.get(_META_ADACCOUNTS_URL).respond(
        401,
        json={"error": {"code": 190, "fbtrace_id": "TRACE_DISC"}},
    )

    from core.pull_errors import ConnectorError

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(ConnectorError) as excinfo:
            connector.discover_accounts("conn_meta_1")

    assert excinfo.value.error_class == "auth_expired"
    assert excinfo.value.provider_payload["error"]["fbtrace_id"] == "TRACE_DISC"
