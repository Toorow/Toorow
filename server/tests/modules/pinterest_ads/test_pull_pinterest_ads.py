"""Story 26.3 -- pinterest-ads sync pull mechanics (respx-mocked, no real API).

Covers: sync analytics request shape (columns derived from the manifest
bundle, pinned attribution windows, DAY granularity, 250-id batching), the
LONG micros-converted landing (catalog-driven conversion set), the BODY-CODE
error taxonomy (401 code 2 -> auth_expired AND the 400 code 12 transient
trap), 429 code 8 -> RateLimitError with x-ratelimit-reset, and the pre-call
window refusals. Live probe deferred (AI-13 / 25.6 -- ROLLOUT_NOTES).
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
import respx
from core.pull_errors import (
    AuthExpiredError,
    InvalidRequestError,
    PermissionDeniedError,
    ProviderTransientError,
)
from core.quota import RateLimitError

from .conftest import AD_ACCOUNT, API_BASE, MANIFEST

_CAMPAIGNS_URL = f"{API_BASE}/ad_accounts/{AD_ACCOUNT}/campaigns"
_ANALYTICS_URL = f"{API_BASE}/ad_accounts/{AD_ACCOUNT}/campaigns/analytics"

# Recent dates so the 90-day sync window check never rots the suite.
_DAY = (date.today() - timedelta(days=2)).isoformat()

_CAMPAIGN_LIST = {"items": [{"id": "626744128982"}], "bookmark": None}

_ANALYTICS_ROWS = [
    {
        "DATE": _DAY,
        "CAMPAIGN_ID": "626744128982",
        "CAMPAIGN_NAME": "Prospecting - FR",
        "CAMPAIGN_ENTITY_STATUS": "ACTIVE",
        "CAMPAIGN_OBJECTIVE_TYPE": "CONVERSIONS",
        "SPEND_IN_MICRO_DOLLAR": 118400000,
        "TOTAL_IMPRESSION": 12040,
        "TOTAL_CLICKTHROUGH": 640,
        "TOTAL_ENGAGEMENT": 810,
        "TOTAL_CONVERSIONS": 42,
        "TOTAL_CHECKOUT": 17,
        "TOTAL_CHECKOUT_VALUE_IN_MICRO_DOLLAR": 3120750000,
    }
]


def _run_pull(connector, **overrides):
    kwargs = dict(
        connection_id="c",
        date_from=_DAY,
        date_to=_DAY,
        project_id="p",
        pull_id="pull_pin_t1",
        ad_account_id=AD_ACCOUNT,
    )
    kwargs.update(overrides)
    return connector.pull_campaign_daily(**kwargs)


@respx.mock
def test_campaign_daily_request_shape_and_micros_landing(
    connector, tmp_path, monkeypatch
):
    """Columns come from the manifest bundle (AD-2); attribution pinned;
    micro-currency landed as value/1e6 via the CATALOG marker set."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "pin.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(200, json=_CAMPAIGN_LIST)
    )
    analytics = respx.get(_ANALYTICS_URL).mock(
        return_value=httpx.Response(200, json=_ANALYTICS_ROWS)
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = _run_pull(connector)

    # 7 bundle metrics landed long for the single row.
    assert result["row_count"] == 7
    assert result["truncated"] is False  # review 26.3 F-6
    assert result["non_numeric_landed"] == 0  # review 26.3 F-5
    assert result["attribution"] == {
        "click_window_days": 30,
        "engagement_window_days": 30,
        "view_window_days": 1,
        "conversion_report_time": "TIME_OF_AD_ACTION",
    }

    params = analytics.calls.last.request.url.params
    profile = next(
        r for r in MANIFEST["source_capabilities"]["reports"]
        if r["id"] == "campaign_daily"
    )
    expected_columns = [d for d in profile["dimensions"] if d != "date"] + (
        profile["metrics"]
    )
    sent = params["columns"].split(",")
    assert sent == expected_columns, sent
    # Charset gate held for every sent token.
    assert all(re.match(r"^[A-Z][A-Z0-9_]*$", c) for c in sent)
    assert params["granularity"] == "DAY"
    assert params["campaign_ids"] == "626744128982"
    assert params["start_date"] == _DAY and params["end_date"] == _DAY
    # Attribution windows PINNED explicitly (never provider defaults).
    assert params["click_window_days"] == "30"
    assert params["engagement_window_days"] == "30"
    assert params["view_window_days"] == "1"
    assert params["conversion_report_time"] == "TIME_OF_AD_ACTION"
    # Bearer token used immediately, never logged.
    assert analytics.calls.last.request.headers["authorization"] == "Bearer tok"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, value_num, data_level, ad_account_id, campaign_id,"
        " segments_json, attributes_json FROM raw_pinterest_ads_daily"
        " WHERE pull_id = 'pull_pin_t1' ORDER BY metric"
    ).fetchall()
    con.close()
    by_metric = {r[0]: r for r in rows}
    # Micros rule: cost + checkout_value in currency units, canonical names.
    assert by_metric["cost"][1] == pytest.approx(118.4)
    assert by_metric["checkout_value"][1] == pytest.approx(3120.75)
    assert "SPEND_IN_MICRO_DOLLAR" not in by_metric
    assert by_metric["impressions"][1] == 12040
    assert by_metric["conversions"][1] == 42
    assert all(
        r[2] == "CAMPAIGN" and r[3] == AD_ACCOUNT and r[4] == "626744128982"
        for r in rows
    )
    # AD-22 diff (story 26.6 Part A): status/objective are DESCRIPTIVE MUTABLE
    # entity attributes -- they moved OUT of segments_json (the grain key) INTO
    # attributes_json (latest-wins, hors partition). segments_json now carries
    # only grain-bearing breakdowns; this row has none, so it is NULL.
    assert by_metric["cost"][5] is None  # segments_json empty (no grain dim)
    attributes = json.loads(by_metric["cost"][6])
    assert attributes["campaign_status"] == "ACTIVE"
    assert attributes["campaign_objective_type"] == "CONVERSIONS"


def test_no_account_selected_is_a_hard_error_without_env_fallback(connector):
    """PINTEREST_ADS_*_ID env fallbacks are forbidden: unselected = error."""
    with patch(
        "core.token_service.resolve_connection_by_nango_id", return_value=None
    ):
        with pytest.raises(ValueError) as exc:
            _run_pull(connector, ad_account_id=None)
    assert "topology" in str(exc.value)


def test_sync_window_beyond_90_days_refused_before_any_call(connector):
    """Sync limits (90 back / 90 range) are typed refusals PRE-CALL: no respx
    mock is active, so an HTTP attempt would raise ConnectError instead."""
    old = (date.today() - timedelta(days=120)).isoformat()
    with pytest.raises(InvalidRequestError) as exc:
        _run_pull(connector, date_from=old, date_to=old)
    assert exc.value.error_class == "invalid_request"
    assert "90" in str(exc.value)


def test_malformed_date_refused_typed(connector):
    with pytest.raises(InvalidRequestError):
        _run_pull(connector, date_from="07/01/2026", date_to=_DAY)


@respx.mock
def test_401_body_code_2_maps_to_auth_expired(connector):
    """AC error path: the BODY code drives the classification (dossier s.7)."""
    body = {"code": 2, "message": "Authentication failed."}
    respx.get(_CAMPAIGNS_URL).mock(return_value=httpx.Response(401, json=body))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(AuthExpiredError) as exc:
            _run_pull(connector)
    assert exc.value.error_class == "auth_expired"
    assert exc.value.provider_status == 401
    # Provider payload preserved as evidence (Story 25.2 contract).
    assert "Authentication failed" in str(exc.value.provider_payload)


@respx.mock
def test_400_body_code_12_is_provider_transient_not_invalid_request(connector):
    """THE Pinterest trap: a 400 with body code 12 ('Retry after') is
    TRANSIENT -- a naive 400=invalid_request would break the breaker and
    fire false pull_invalid_request_drift."""
    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(200, json=_CAMPAIGN_LIST)
    )
    body = {"code": 12, "message": "Retry after 60 seconds."}
    respx.get(_ANALYTICS_URL).mock(return_value=httpx.Response(400, json=body))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(ProviderTransientError) as exc:
            _run_pull(connector)
    assert exc.value.error_class == "provider_transient"
    assert exc.value.retryable is True
    assert exc.value.provider_status == 400


@respx.mock
def test_400_retry_after_message_without_code_12_still_transient(connector):
    """Body-code drift guard (Airbyte cross-check): the message wins."""
    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(200, json=_CAMPAIGN_LIST)
    )
    body = {"code": 0, "message": "Something went wrong. Retry after a while."}
    respx.get(_ANALYTICS_URL).mock(return_value=httpx.Response(400, json=body))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(ProviderTransientError):
            _run_pull(connector)


@respx.mock
def test_400_other_code_is_invalid_request_drift_signal(connector):
    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(200, json=_CAMPAIGN_LIST)
    )
    body = {"code": 2900, "message": "Invalid column."}
    respx.get(_ANALYTICS_URL).mock(return_value=httpx.Response(400, json=body))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(InvalidRequestError):
            _run_pull(connector)


@respx.mock
def test_403_body_code_29_maps_to_permission_denied(connector):
    """Trial-tier feature gate (403 code 29) -> readable permission_denied."""
    body = {"code": 29, "message": "Not allowed for your app tier."}
    respx.get(_CAMPAIGNS_URL).mock(return_value=httpx.Response(403, json=body))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(PermissionDeniedError):
            _run_pull(connector)


@respx.mock
def test_429_code_8_raises_rate_limit_error_with_reset_header(connector):
    """429 stays on the breaker path; retry_after from x-ratelimit-reset."""
    body = {"code": 8, "message": "This request exceeded a rate limit."}
    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(
            429, json=body, headers={"x-ratelimit-reset": "37"}
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(RateLimitError) as exc:
            _run_pull(connector)
    assert exc.value.retry_after == 37


@respx.mock
def test_non_numeric_metric_value_lands_in_segments_not_dropped(
    connector, tmp_path, monkeypatch, caplog
):
    """Review 26.3 F-5: a non-numeric metric value is NEVER silently dropped:
    it lands in segments_json (key = the metric field id), a warning is
    logged, and the pull result counts it (non_numeric_landed)."""
    import logging

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "pin_nonnum.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    poisoned = [dict(_ANALYTICS_ROWS[0])]
    poisoned[0]["TOTAL_CONVERSIONS"] = "not-a-number"
    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(200, json=_CAMPAIGN_LIST)
    )
    respx.get(_ANALYTICS_URL).mock(
        return_value=httpx.Response(200, json=poisoned)
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with caplog.at_level(logging.WARNING):
            result = _run_pull(connector)

    assert result["non_numeric_landed"] == 1
    assert result["row_count"] == 6  # the poisoned metric landed no value row
    assert "pinterest_ads_non_numeric_metric_landed" in caplog.text

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, segments_json FROM raw_pinterest_ads_daily"
        " WHERE pull_id = 'pull_pin_t1'"
    ).fetchall()
    con.close()
    metrics = {r[0] for r in rows}
    assert "conversions" not in metrics  # no corrupted value_num row
    # ... but the value is OBSERVABLE in every sibling row's segments_json.
    for _metric, segments_raw in rows:
        segments = json.loads(segments_raw)
        assert segments["conversions"] == "not-a-number"


@respx.mock
def test_entity_page_cap_propagates_truncated_flag(
    connector, tmp_path, monkeypatch
):
    """Review 26.3 F-6 (google-ads pattern): the bookmark page cap with more
    pages pending -> the pull result carries truncated=True instead of a
    silent partial entity universe."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "pin_trunc.duckdb"))
    monkeypatch.setattr(connector, "_MAX_BOOKMARK_PAGES", 25)

    # Every page returns a bookmark: page 26 would exist -> cap hit at 25.
    pages = respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": "1"}], "bookmark": "more"}
        )
    )
    respx.get(_ANALYTICS_URL).mock(return_value=httpx.Response(200, json=[]))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = _run_pull(connector)
    assert pages.call_count == 25
    assert result["truncated"] is True


@respx.mock
def test_entity_batching_respects_250_id_cap(connector, tmp_path, monkeypatch):
    """260 campaigns -> two analytics calls (250 + 10 ids)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "pin_batch.duckdb"))

    ids = [str(1000 + i) for i in range(260)]
    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": i} for i in ids], "bookmark": None}
        )
    )
    analytics = respx.get(_ANALYTICS_URL).mock(
        return_value=httpx.Response(200, json=[])
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = _run_pull(connector)
    assert result["row_count"] == 0
    assert analytics.call_count == 2
    first = analytics.calls[0].request.url.params["campaign_ids"].split(",")
    second = analytics.calls[1].request.url.params["campaign_ids"].split(",")
    assert len(first) == 250 and len(second) == 10
