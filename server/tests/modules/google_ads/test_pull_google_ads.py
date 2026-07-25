"""Story 26.2 -- google-ads pull mechanics (respx-mocked, no real API).

Covers: GAQL request shape for an exact_bundle profile (fields derived from
the manifest, headers carry developer-token + login-customer-id), the LONG
micros-converted landing, the 401 error paths (error_map enum refinement),
429 -> RateLimitError with QuotaErrorDetails.retry_delay, and the
UNSUPPORTED_VERSION loud drift alert. Live probe deferred (AI-13 / 25.6).
"""

from __future__ import annotations

import logging
from unittest.mock import patch

import httpx
import pytest
import respx
from core.pull_errors import (
    AuthExpiredError,
    AuthRevokedError,
    InvalidRequestError,
    PermissionDeniedError,
)
from core.quota import RateLimitError

from .conftest import API_BASE

_CID = "9861234567"
_SEARCH_URL = f"{API_BASE}/customers/{_CID}/googleAds:search"

# camelCase protobuf-JSON row exactly as the REST endpoint returns it
# (int64 as strings, doubles as numbers).
_SEARCH_RESPONSE = {
    "results": [
        {
            "customer": {"id": _CID},
            "campaign": {
                "id": "22334455",
                "name": "Brand - Search - FR",
                "status": "ENABLED",
                "advertisingChannelType": "SEARCH",
                "biddingStrategyType": "TARGET_ROAS",
            },
            "metrics": {
                "costMicros": "118400000",
                "impressions": "12040",
                "clicks": "640",
                "conversions": 42.5,
                "conversionsValue": 3120.75,
                "allConversions": 48.0,
                "viewThroughConversions": "5",
            },
            "segments": {"date": "2026-07-01"},
        }
    ],
    "fieldMask": "segments.date,campaign.id",
}


def _google_ads_failure(http_code: int, status: str, error_key: str, enum: str) -> dict:
    return {
        "error": {
            "code": http_code,
            "message": f"mock {enum}",
            "status": status,
            "details": [
                {
                    "@type": (
                        "type.googleapis.com/google.ads.googleads.v24.errors."
                        "GoogleAdsFailure"
                    ),
                    "errors": [
                        {"errorCode": {error_key: enum}, "message": f"mock {enum}"}
                    ],
                }
            ],
        }
    }


@respx.mock
def test_campaign_daily_request_shape_and_micros_landing(
    connector, tmp_path, monkeypatch
):
    """GAQL derived from the manifest bundle; headers complete; cost = micros/1e6."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gads.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    route = respx.post(_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_SEARCH_RESPONSE)
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_campaign_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_gads_t1",
            customer_id=f"{_CID}@1112223334",
        )

    assert result["row_count"] == 7  # 7 bundle metrics landed long
    req = route.calls.last.request
    import json as _json

    query = _json.loads(req.content)["query"]
    # Field lists come from the manifest capability report (AD-2), snake_case GAQL.
    for token in (
        "segments.date", "customer.id", "campaign.id", "campaign.name",
        "campaign.status", "campaign.advertising_channel_type",
        "campaign.bidding_strategy_type", "metrics.cost_micros",
        "metrics.impressions", "metrics.clicks", "metrics.conversions",
        "metrics.conversions_value", "metrics.all_conversions",
        "metrics.view_through_conversions",
    ):
        assert token in query, f"missing {token} in GAQL: {query}"
    assert "FROM campaign" in query
    assert "WHERE segments.date BETWEEN '2026-07-01' AND '2026-07-01'" in query
    # Headers: bearer + platform developer token + login-customer-id from the
    # composite topology account id '<cid>@<login_cid>'.
    assert req.headers["authorization"] == "Bearer tok"
    assert req.headers["developer-token"] == "dev-token-test"
    assert req.headers["login-customer-id"] == "1112223334"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, value_num, data_level, campaign_id FROM raw_google_ads_daily "
        "WHERE pull_id = 'pull_gads_t1' ORDER BY metric"
    ).fetchall()
    con.close()
    by_metric = {r[0]: r[1] for r in rows}
    # Micros rule: cost landed in currency units, canonical name 'cost'.
    assert by_metric["cost"] == pytest.approx(118.4)
    assert "cost_micros" not in by_metric
    assert by_metric["impressions"] == 12040
    assert by_metric["conversions"] == pytest.approx(42.5)
    assert all(r[2] == "CAMPAIGN" and r[3] == "22334455" for r in rows)


def test_no_account_selected_is_a_hard_error_without_env_fallback(connector):
    """GOOGLE_ADS_*_ID env fallbacks are forbidden: unselected account = error."""
    with patch(
        "core.token_service.resolve_connection_by_nango_id", return_value=None
    ):
        with pytest.raises(ValueError) as exc:
            connector.pull_campaign_daily(
                connection_id="c",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="p",
                pull_id="pull_gads_noacct",
            )
    assert "topology" in str(exc.value)


@respx.mock
def test_401_oauth_token_expired_maps_to_auth_expired(connector):
    """AC error path: 401 OAUTH_TOKEN_EXPIRED -> AuthExpiredError, payload kept."""
    body = _google_ads_failure(
        401, "UNAUTHENTICATED", "authenticationError", "OAUTH_TOKEN_EXPIRED"
    )
    respx.post(_SEARCH_URL).mock(return_value=httpx.Response(401, json=body))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(AuthExpiredError) as exc:
            connector.pull_campaign_daily(
                connection_id="c",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="p",
                pull_id="pull_gads_401",
                customer_id=_CID,
            )
    assert exc.value.error_class == "auth_expired"
    assert exc.value.provider_status == 401
    # Provider payload preserved as evidence (Story 25.2 contract).
    assert "OAUTH_TOKEN_EXPIRED" in str(exc.value.provider_payload)


@respx.mock
def test_401_oauth_token_revoked_maps_to_auth_revoked(connector):
    body = _google_ads_failure(
        401, "UNAUTHENTICATED", "authenticationError", "OAUTH_TOKEN_REVOKED"
    )
    respx.post(_SEARCH_URL).mock(return_value=httpx.Response(401, json=body))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(AuthRevokedError):
            connector.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
                project_id="p", pull_id="pull_gads_401r", customer_id=_CID,
            )


@respx.mock
def test_403_customer_not_enabled_maps_to_permission_denied(connector):
    body = _google_ads_failure(
        403, "PERMISSION_DENIED", "authorizationError", "CUSTOMER_NOT_ENABLED"
    )
    respx.post(_SEARCH_URL).mock(return_value=httpx.Response(403, json=body))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(PermissionDeniedError):
            connector.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
                project_id="p", pull_id="pull_gads_403", customer_id=_CID,
            )


@respx.mock
def test_429_raises_rate_limit_error_with_quota_retry_delay(connector):
    """429 stays on the breaker path; retry_after honors quotaErrorDetails."""
    body = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {
                    "@type": (
                        "type.googleapis.com/google.ads.googleads.v24.errors."
                        "GoogleAdsFailure"
                    ),
                    "errors": [
                        {
                            "errorCode": {"quotaError": "RESOURCE_EXHAUSTED"},
                            "details": {"quotaErrorDetails": {"retryDelay": "37s"}},
                        }
                    ],
                }
            ],
        }
    }
    respx.post(_SEARCH_URL).mock(return_value=httpx.Response(429, json=body))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(RateLimitError) as exc:
            connector.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
                project_id="p", pull_id="pull_gads_429", customer_id=_CID,
            )
    assert exc.value.retry_after == 37


@respx.mock
def test_unsupported_version_is_invalid_request_and_logs_drift_alert(
    connector, caplog
):
    """400 UNSUPPORTED_VERSION -> invalid_request + LOUD version-drift alert."""
    body = _google_ads_failure(
        400, "INVALID_ARGUMENT", "requestError", "UNSUPPORTED_VERSION"
    )
    respx.post(_SEARCH_URL).mock(return_value=httpx.Response(400, json=body))
    with caplog.at_level(logging.ERROR):
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            with pytest.raises(InvalidRequestError):
                connector.pull_campaign_daily(
                    connection_id="c", date_from="2026-07-01",
                    date_to="2026-07-01", project_id="p",
                    pull_id="pull_gads_ver", customer_id=_CID,
                )
    assert "google_ads_unsupported_version_drift" in caplog.text


@respx.mock
def test_pagination_follows_next_page_token(connector, tmp_path, monkeypatch):
    """search pages via nextPageToken until the last page (fixed 10k pages)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gads_pages.duckdb"))

    page_two = {
        "results": [
            {
                "customer": {"id": _CID},
                "campaign": {"id": "22334466", "name": "Second"},
                "metrics": {"costMicros": "1000000", "impressions": "10",
                            "clicks": "1", "conversions": 0.0,
                            "conversionsValue": 0.0, "allConversions": 0.0,
                            "viewThroughConversions": "0"},
                "segments": {"date": "2026-07-01"},
            }
        ]
    }
    first = dict(_SEARCH_RESPONSE)
    first["nextPageToken"] = "tok-page-2"
    route = respx.post(_SEARCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=first),
            httpx.Response(200, json=page_two),
        ]
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_campaign_daily(
            connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
            project_id="p", pull_id="pull_gads_pg", customer_id=_CID,
        )
    assert route.call_count == 2
    assert result["pages"] == 2
    assert result["truncated"] is False
    import json as _json

    second_body = _json.loads(route.calls[1].request.content)
    assert second_body.get("pageToken") == "tok-page-2"


@respx.mock
def test_pagination_page_cap_truncates_loudly(
    connector, tmp_path, monkeypatch, caplog
):
    """Review 26.2 F-11: hitting the page cap stops the loop, flags
    truncated=True in the result AND logs the truncation warning -- never a
    silent partial pull."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gads_cap.duckdb"))
    monkeypatch.setattr(connector, "_MAX_SEARCH_PAGES", 2)

    endless = dict(_SEARCH_RESPONSE)
    endless["nextPageToken"] = "tok-more"
    route = respx.post(_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=endless)
    )
    with caplog.at_level(logging.WARNING):
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            result = connector.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
                project_id="p", pull_id="pull_gads_cap", customer_id=_CID,
            )
    assert route.call_count == 2  # the cap stops the loop
    assert result["pages"] == 2
    assert result["truncated"] is True
    assert "google_ads_search_truncated" in caplog.text
