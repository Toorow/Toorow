"""Story 26.4 -- microsoft-ads pull mechanics (respx-mocked, no real API).

Covers: the SubmitGenerateReport request shape for an exact_bundle profile
(columns derived from the manifest bundle, headers carry Authorization +
DeveloperToken + CustomerId + CustomerAccountId), the async happy path
(Submit -> Poll Success -> immediate ZIP CSV download), the LONG typed
landing, the 401 body-code error paths (109 auth_expired / 105 auth_revoked),
the 207 concurrent-report cap -> RateLimitError (body code beats the generic
HTTP 400), the ReportingService* invalid_request family, and the loud
date/charset guards. Live probe deferred (AI-13 / 25.6).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
import respx
from core.async_reports import InMemoryReportRefStore
from core.pull_errors import AuthExpiredError, AuthRevokedError, InvalidRequestError
from core.quota import RateLimitError

from .conftest import (
    ACCOUNT_ID,
    COMPOSITE_ACCOUNT,
    CUSTOMER_ID,
    DOWNLOAD_URL,
    POLL_URL,
    SUBMIT_URL,
    fault_body,
    make_report_zip,
)

_CAMPAIGN_HEADERS = [
    "TimePeriod", "AccountId", "CampaignId", "CampaignName", "CampaignType",
    "CampaignStatus", "CurrencyCode", "Spend", "Impressions", "Clicks",
    "Conversions", "Revenue",
]
_CAMPAIGN_ROW = [
    "2026-07-01", ACCOUNT_ID, "22334455", "Brand - Search - FR", "Search",
    "Active", "EUR", "118.40", "12040", "640", "42", "3120.75",
]


def _mock_happy_flow():
    submit = respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"ReportRequestId": "rr_0001"})
    )
    poll = respx.post(POLL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "ReportRequestStatus": {
                    "Status": "Success",
                    "ReportDownloadUrl": DOWNLOAD_URL,
                }
            },
        )
    )
    download = respx.get(DOWNLOAD_URL).mock(
        return_value=httpx.Response(
            200, content=make_report_zip(_CAMPAIGN_HEADERS, [_CAMPAIGN_ROW])
        )
    )
    return submit, poll, download


@respx.mock
def test_campaign_daily_submit_shape_headers_and_typed_landing(
    connector, tmp_path, monkeypatch
):
    """Submit body derived from the manifest bundle; headers complete;
    values coerced by catalog physical_type; LONG canonical landing."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "msads.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    submit, poll, download = _mock_happy_flow()
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_campaign_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_msads_t1",
            account_id=COMPOSITE_ACCOUNT,
            report_store=InMemoryReportRefStore(),
        )

    assert result["status"] == "completed"
    assert result["row_count"] == 5  # 5 bundle metrics landed long

    body = json.loads(submit.calls.last.request.content)["ReportRequest"]
    # Report type whitelisted + full request shape (dossier section 4).
    assert body["Type"] == "CampaignPerformanceReportRequest"
    assert body["Aggregation"] == "Daily"
    assert body["Format"] == "Csv" and body["FormatVersion"] == "2.0"
    assert body["ReturnOnlyCompleteData"] is False
    assert body["Scope"] == {"AccountIds": [int(ACCOUNT_ID)]}
    assert body["Time"]["CustomDateRangeStart"] == {
        "Day": 1, "Month": 7, "Year": 2026,
    }
    assert body["Time"]["CustomDateRangeEnd"] == {
        "Day": 1, "Month": 7, "Year": 2026,
    }
    # Columns come from the manifest bundle (AD-2), provider charset only.
    for column in _CAMPAIGN_HEADERS:
        assert column in body["Columns"], column
    assert all(c.isalnum() for c in body["Columns"])

    # Headers: bearer + platform developer token + BOTH customer headers on
    # every reporting call (story hard requirement).
    req = submit.calls.last.request
    assert req.headers["authorization"] == "Bearer tok"
    assert req.headers["developertoken"] == "dev-token-test"
    assert req.headers["customerid"] == CUSTOMER_ID
    assert req.headers["customeraccountid"] == ACCOUNT_ID
    poll_req = poll.calls.last.request
    assert poll_req.headers["customerid"] == CUSTOMER_ID
    assert poll_req.headers["customeraccountid"] == ACCOUNT_ID
    assert json.loads(poll_req.content) == {"ReportRequestId": "rr_0001"}
    assert download.called

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, value_num, data_level, campaign_id, cost_source_currency "
        "FROM raw_microsoft_ads_daily WHERE pull_id = 'pull_msads_t1' "
        "ORDER BY metric"
    ).fetchall()
    con.close()
    by_metric = {r[0]: r[1] for r in rows}
    # Canonical rename (spend -> cost) + physical_type coercion.
    assert by_metric["cost"] == pytest.approx(118.4)
    assert "spend" not in by_metric
    assert by_metric["impressions"] == 12040
    assert by_metric["conversions"] == 42
    assert by_metric["revenue"] == pytest.approx(3120.75)
    assert all(r[2] == "CAMPAIGN" and r[3] == "22334455" for r in rows)
    assert all(r[4] == "EUR" for r in rows)


def test_no_account_selected_is_a_hard_error_without_env_fallback(connector):
    """MICROSOFT_ADS_*_ID env fallbacks are forbidden: unselected = error."""
    with patch(
        "core.token_service.resolve_connection_by_nango_id", return_value=None
    ):
        with pytest.raises(ValueError) as exc:
            connector.pull_campaign_daily(
                connection_id="c",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="p",
                pull_id="pull_msads_noacct",
                report_store=InMemoryReportRefStore(),
            )
    assert "topology" in str(exc.value)


@respx.mock
def test_401_code_109_maps_to_auth_expired(connector):
    """AC error path: 401 body Code 109 (AuthenticationTokenExpired) ->
    AuthExpiredError, provider payload preserved as evidence."""
    respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(
            401, json=fault_body(109, "AuthenticationTokenExpired")
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(AuthExpiredError) as exc:
            connector.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
                project_id="p", pull_id="pull_msads_401",
                account_id=COMPOSITE_ACCOUNT,
                report_store=InMemoryReportRefStore(),
            )
    assert exc.value.error_class == "auth_expired"
    assert exc.value.provider_status == 401
    assert "AuthenticationTokenExpired" in str(exc.value.provider_payload)


@respx.mock
def test_401_code_105_maps_to_auth_revoked(connector):
    respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(401, json=fault_body(105, "InvalidCredentials"))
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(AuthRevokedError):
            connector.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
                project_id="p", pull_id="pull_msads_401r",
                account_id=COMPOSITE_ACCOUNT,
                report_store=InMemoryReportRefStore(),
            )


@respx.mock
def test_code_207_concurrent_limit_is_rate_limited_even_on_http_400(connector):
    """The concurrent-report cap ships as HTTP 400 + body Code 207: the BODY
    code wins -> RateLimitError (breaker re-queue), never invalid_request."""
    respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(
            400, json=fault_body(207, "ConcurrentRequestOverLimit")
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(RateLimitError) as exc:
            connector.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
                project_id="p", pull_id="pull_msads_207",
                account_id=COMPOSITE_ACCOUNT,
                report_store=InMemoryReportRefStore(),
            )
    assert exc.value.platform == "microsoft-ads"
    assert exc.value.retry_after is None  # no documented Retry-After


@respx.mock
def test_code_117_call_rate_is_rate_limited(connector):
    respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(400, json=fault_body(117, "CallRateExceeded"))
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(RateLimitError):
            connector.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
                project_id="p", pull_id="pull_msads_117",
                account_id=COMPOSITE_ACCOUNT,
                report_store=InMemoryReportRefStore(),
            )


@respx.mock
def test_reporting_validation_code_2015_maps_to_invalid_request(connector):
    respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(
            400, json=fault_body(2015, "RequiredColumnsNotSelected")
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with pytest.raises(InvalidRequestError) as exc:
            connector.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
                project_id="p", pull_id="pull_msads_2015",
                account_id=COMPOSITE_ACCOUNT,
                report_store=InMemoryReportRefStore(),
            )
    assert "RequiredColumnsNotSelected" in str(exc.value.provider_payload)


def test_invalid_dates_are_refused_before_any_submit(connector):
    with pytest.raises(ValueError):
        connector.build_submit_request(
            "CampaignPerformanceReportRequest", ["TimePeriod", "Impressions"],
            ACCOUNT_ID, "2026-07-99", "2026-07-01", "toorowtest",
        )
    with pytest.raises(ValueError):
        connector.build_submit_request(
            "CampaignPerformanceReportRequest", ["TimePeriod", "Impressions"],
            ACCOUNT_ID, "2026-07-02", "2026-07-01", "toorowtest",
        )


def test_non_whitelisted_report_type_is_refused(connector):
    with pytest.raises(ValueError) as exc:
        connector.build_submit_request(
            "AppsPerformanceReportRequest", ["TimePeriod", "Impressions"],
            ACCOUNT_ID, "2026-07-01", "2026-07-01", "toorowtest",
        )
    assert "whitelisted" in str(exc.value)


def test_column_charset_guard(connector):
    with pytest.raises(ValueError) as exc:
        connector.build_submit_request(
            "CampaignPerformanceReportRequest",
            ["TimePeriod", "Impressions; DROP TABLE"],
            ACCOUNT_ID, "2026-07-01", "2026-07-01", "toorowtest",
        )
    assert "A-Za-z0-9" in str(exc.value)


@respx.mock
def test_non_numeric_statistic_lands_in_segments_json_with_counter(
    connector, tmp_path, monkeypatch, caplog
):
    """F-3 (review 26.4): a NON-NUMERIC statistic value is never silently
    dropped -- no value_num row is emitted for it, but the raw value is
    reinjected into segments_json keyed by the catalog FIELD ID (spend, not
    the canonical alias cost), a warning is logged and the result carries the
    non_numeric_landed counter."""
    import logging

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "ms_nonnum.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    weird_row = list(_CAMPAIGN_ROW)
    weird_row[7] = "n/a"  # Spend cell -- provider glitch, non-numeric
    respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"ReportRequestId": "rr_nn"})
    )
    respx.post(POLL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "ReportRequestStatus": {
                    "Status": "Success", "ReportDownloadUrl": DOWNLOAD_URL,
                }
            },
        )
    )
    respx.get(DOWNLOAD_URL).mock(
        return_value=httpx.Response(
            200, content=make_report_zip(_CAMPAIGN_HEADERS, [weird_row])
        )
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        with caplog.at_level(logging.WARNING):
            result = connector.pull_campaign_daily(
                connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
                project_id="p", pull_id="pull_msads_nonnum",
                account_id=COMPOSITE_ACCOUNT,
                report_store=InMemoryReportRefStore(),
            )

    # 4 of the 5 bundle metrics landed numeric; the counter reports the 5th.
    assert result["status"] == "completed"
    assert result["row_count"] == 4
    assert result["non_numeric_landed"] == 1
    assert any(
        "microsoft_ads_non_numeric_statistics" in rec.getMessage()
        for rec in caplog.records
    )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, segments_json FROM raw_microsoft_ads_daily "
        "WHERE pull_id = 'pull_msads_nonnum'"
    ).fetchall()
    con.close()
    metrics = {r[0] for r in rows}
    assert "cost" not in metrics  # no corrupted value_num row for Spend
    assert metrics == {"impressions", "clicks", "conversions", "revenue"}
    # Raw evidence keyed by the catalog field_id, on every row of the grain.
    for _metric, segments in rows:
        assert json.loads(segments)["spend"] == "n/a"


def test_request_hash_is_column_order_insensitive(connector):
    """F-4 (review 26.4): the Submit body keeps the caller's column order but
    two reordered selections ask for the SAME data -- they must share one
    request hash so the second resumes the first's report by re-poll."""
    kwargs = dict(
        account_id=ACCOUNT_ID, date_from="2026-07-01", date_to="2026-07-01",
    )
    body_a = connector.build_submit_request(
        "CampaignPerformanceReportRequest",
        ["TimePeriod", "CampaignId", "Impressions", "Clicks"],
        report_name="toorowrun1", **kwargs,
    )
    body_b = connector.build_submit_request(
        "CampaignPerformanceReportRequest",
        ["Clicks", "TimePeriod", "Impressions", "CampaignId"],
        report_name="toorowrun2", **kwargs,
    )
    body_c = connector.build_submit_request(
        "CampaignPerformanceReportRequest",
        ["TimePeriod", "CampaignId", "Impressions", "Spend"],
        report_name="toorowrun3", **kwargs,
    )
    assert connector._request_hash(body_a, CUSTOMER_ID) == \
        connector._request_hash(body_b, CUSTOMER_ID)
    # Different column SET (not order) still yields a different hash.
    assert connector._request_hash(body_a, CUSTOMER_ID) != \
        connector._request_hash(body_c, CUSTOMER_ID)
    # And the customer header stays part of the dedup key.
    assert connector._request_hash(body_a, CUSTOMER_ID) != \
        connector._request_hash(body_a, "999000111")


def test_transform_matches_golden_fixture(connector):
    """Layer-4 contract locally: transform(golden) == expected, field by field
    (catalog physical_type coercion + manifest renames, zero drops)."""
    import json as _json
    from pathlib import Path

    fixtures = (
        Path(__file__).parents[4]
        / "server" / "modules" / "microsoft-ads" / "tests" / "fixtures"
    )
    golden = _json.loads((fixtures / "golden_pull.json").read_text("utf-8"))
    expected = _json.loads((fixtures / "expected_facts.json").read_text("utf-8"))
    assert connector.transform(golden) == expected
