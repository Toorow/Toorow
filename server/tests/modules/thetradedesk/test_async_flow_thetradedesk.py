"""Story 28.2 -- async report flow through the 26.1 socle (fake clock, respx).

Covers: the happy submit -> poll -> download -> LONG landing path (schedule
body + TTD-Auth header on the wire), the auth token minted before use, the
request-shape (ReportTemplateName + dimensions/metrics), deadline deferral and
fake-clock resumption (re-poll, never resubmit), FAILED -> single resubmission
-> provider_transient, configuration FAILED -> invalid_request, the 401
re-auth-once path, 403 permission_denied, 429 breaker, download-link expiry.
No real sleeps: the socle clock/sleeper/store are the 26.1 fakes.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
import respx
from core.pull_errors import (
    AuthExpiredError,
    InvalidRequestError,
    PermissionDeniedError,
    ProviderTransientError,
)

from .conftest import API_HOST

_AUTH_URL = f"{API_HOST}/v3/authentication"
_SCHEDULE_URL = f"{API_HOST}/v3/myreports/reportschedule"
_EXECUTION_URL = f"{API_HOST}/v3/myreports/reportexecution/query/advertisers"
_DOWNLOAD_URL = "https://ttd-report-storage.s3.amazonaws.com/report.csv"

_DATE_FROM = (date.today() - timedelta(days=3)).isoformat()
_DATE_TO = (date.today() - timedelta(days=1)).isoformat()

_ACCOUNT = "partner-xyz:adv-123"

_ROWS = [
    {
        "date": _DATE_FROM,
        "AdvertiserId": "adv-123",
        "CampaignId": "cmp-1",
        "AdGroupId": "ag-1",
        "Impressions": 12040,
        "Clicks": 640,
        "Bids": 90000,
        "AdvertiserCostAdvCurrency": 118.4,
        "AdvertiserCostUSD": 128.1,
        "TtdCostUsd": 12.0,
        "PartnerCostUsd": 5.0,
        "TotalClickConversions": 42,
        "TotalViewThroughConversions": 15,
    }
]

_CSV = (
    "date,AdvertiserId,CampaignId,AdGroupId,Impressions,Clicks,Bids,"
    "AdvertiserCostAdvCurrency,AdvertiserCostUSD,TtdCostUsd,PartnerCostUsd,"
    "TotalClickConversions,TotalViewThroughConversions\n"
    f"{_DATE_FROM},adv-123,cmp-1,ag-1,12040,640,90000,118.4,128.1,12.0,5.0,42,15\n"
)


def _future_expiry() -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(hours=6)).isoformat().replace(
        "+00:00", "Z"
    )


def _auth_response() -> httpx.Response:
    return httpx.Response(200, json={"Token": "ttd-tok", "ExpirationDateUtc": _future_expiry()})


def _schedule_response(schedule_id: str = "sched-1") -> httpx.Response:
    return httpx.Response(200, json={"ReportScheduleId": schedule_id})


def _execution(state: str, url: str | None = None, reason: str | None = None) -> dict:
    execution: dict = {"ReportExecutionState": state}
    if url:
        execution["ReportDeliveries"] = [{"DownloadURL": url}]
    if reason:
        execution["ReportExecutionErrorMessage"] = reason
    return execution


def _execution_response(state: str, url: str | None = None, reason: str | None = None):
    return httpx.Response(200, json={"Result": [_execution(state, url, reason)]})


def _mock_auth():
    respx.post(_AUTH_URL).mock(return_value=_auth_response())


def _run_pull(connector, hooks, pull_id: str, **kwargs):
    return connector.pull_daily_performance(
        connection_id="conn-ttd",
        date_from=_DATE_FROM,
        date_to=_DATE_TO,
        project_id="p",
        pull_id=pull_id,
        account_id=_ACCOUNT,
        **hooks,
        **kwargs,
    )


@respx.mock
def test_happy_path_submit_poll_download_and_long_landing(
    connector, async_hooks, tmp_path, monkeypatch
):
    """Schedule body + TTD-Auth on the wire; CSV parsed; LONG rows landed."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "ttd.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    _mock_auth()
    schedule_route = respx.post(_SCHEDULE_URL).mock(return_value=_schedule_response())
    respx.post(_EXECUTION_URL).mock(
        side_effect=[
            _execution_response("Pending"),
            _execution_response("Complete", url=_DOWNLOAD_URL),
        ]
    )
    download_route = respx.get(_DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, content=_CSV.encode("utf-8"))
    )

    result = _run_pull(connector, async_hooks, "pull_ttd_t1")

    # 9 numeric bundle metrics land long for the single row.
    assert result["row_count"] == 9
    assert result["report_ref"] == "sched-1"

    # Auth minted before use -> TTD-Auth header on the schedule call.
    req = schedule_route.calls.last.request
    assert req.headers["ttd-auth"] == "ttd-tok"
    assert req.headers["content-type"] == "application/json"
    body = json.loads(req.content)
    assert body["ReportTemplateName"] == "daily_performance"
    assert body["AdvertiserFilters"] == ["adv-123"]
    assert body["ReportStartDateInclusive"] == _DATE_FROM
    assert body["ReportEndDateExclusive"] == _DATE_TO
    cfg = body["Configuration"]
    assert "date" in cfg["Dimensions"]
    for dim in ("AdvertiserId", "CampaignId", "AdGroupId"):
        assert dim in cfg["Dimensions"], dim
    for metric in ("Impressions", "Clicks", "Bids", "AdvertiserCostUSD",
                   "TotalClickConversions"):
        assert metric in cfg["Metrics"], metric

    # Pre-signed download carries NO TTD-Auth header.
    dl_req = download_route.calls.last.request
    assert "ttd-auth" not in dl_req.headers

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, value_num, report_template, advertiser_id, partner_id, "
        "campaign_id, ad_group_id FROM raw_thetradedesk_daily "
        "WHERE pull_id = 'pull_ttd_t1' ORDER BY metric"
    ).fetchall()
    con.close()
    by_metric = {r[0]: r[1] for r in rows}
    assert by_metric["impressions"] == 12040
    assert by_metric["advertiser_cost_adv_currency"] == pytest.approx(118.4)
    assert by_metric["total_click_conversions"] == 42
    assert all(
        r[2] == "daily_performance" and r[3] == "adv-123" and r[4] == "partner-xyz"
        and r[5] == "cmp-1" and r[6] == "ag-1"
        for r in rows
    )


@respx.mock
def test_json_payload_download_also_parsed(connector, async_hooks, tmp_path, monkeypatch):
    """A JSON-array payload (probe may flip CSV->JSON) is parsed too."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ttd_json.duckdb"))
    _mock_auth()
    respx.post(_SCHEDULE_URL).mock(return_value=_schedule_response())
    respx.post(_EXECUTION_URL).mock(
        return_value=_execution_response("Complete", url=_DOWNLOAD_URL)
    )
    respx.get(_DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, content=json.dumps(_ROWS).encode("utf-8"))
    )
    result = _run_pull(connector, async_hooks, "pull_ttd_json")
    assert result["row_count"] == 9


@respx.mock
def test_deadline_deferred_then_fake_clock_resume_by_repoll(
    connector, async_hooks, ref_store
):
    """Reprise: run 1 defers past the deadline (typed retryable, ref stays in
    flight); run 2 with the SAME request hash RESUMES by re-poll -- the provider
    sees exactly ONE schedule submission."""
    _mock_auth()
    schedule_route = respx.post(_SCHEDULE_URL).mock(return_value=_schedule_response())
    exec_route = respx.post(_EXECUTION_URL).mock(
        side_effect=[
            _execution_response("Pending"),
            _execution_response("Running"),
            _execution_response("Complete", url=_DOWNLOAD_URL),
        ]
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_CSV.encode("utf-8")))

    hooks_run1 = dict(async_hooks)
    hooks_run1["_deadline_seconds"] = 1  # defer after the first PENDING poll
    with pytest.raises(ProviderTransientError, match="deferred"):
        _run_pull(connector, hooks_run1, "pull_ttd_defer")
    assert schedule_route.call_count == 1
    stored = ref_store._rows[next(iter(ref_store._rows))]
    assert stored["state"] == "in_flight"
    assert stored["report_ref"] == "sched-1"

    from unittest.mock import patch

    with patch.object(connector, "_insert_raw_rows", return_value=9):
        result = _run_pull(connector, async_hooks, "pull_ttd_resume")
    assert result["resumed"] is True
    assert schedule_route.call_count == 1  # NEVER resubmitted
    assert exec_route.call_count == 3


@respx.mock
def test_failed_execution_resubmitted_once_then_provider_transient(
    connector, async_hooks
):
    """Non-configuration FAILED -> canonical expired -> ONE resubmission ->
    second FAILED -> typed provider_transient (story contract)."""
    _mock_auth()
    schedule_route = respx.post(_SCHEDULE_URL).mock(
        side_effect=[_schedule_response("s1"), _schedule_response("s2")]
    )
    respx.post(_EXECUTION_URL).mock(
        return_value=_execution_response("Failed", reason="internal engine error")
    )
    with pytest.raises(ProviderTransientError):
        _run_pull(connector, async_hooks, "pull_ttd_failed")
    assert schedule_route.call_count == 2  # exactly one resubmission


@respx.mock
def test_failed_with_configuration_reason_is_invalid_request(connector, async_hooks):
    _mock_auth()
    respx.post(_SCHEDULE_URL).mock(return_value=_schedule_response())
    respx.post(_EXECUTION_URL).mock(
        return_value=_execution_response(
            "Failed", reason="unknown metric NotAMetric in template"
        )
    )
    with pytest.raises(InvalidRequestError, match="configuration"):
        _run_pull(connector, async_hooks, "pull_ttd_cfg")


@respx.mock
def test_no_execution_row_is_expired_then_single_resubmission(connector, async_hooks):
    """An empty execution result on a submitted schedule -> canonical expired
    (socle single resubmission), never a silent 0-row success."""
    _mock_auth()
    schedule_route = respx.post(_SCHEDULE_URL).mock(
        side_effect=[_schedule_response("s1"), _schedule_response("s2")]
    )
    # Always empty -> expired -> resubmit once -> empty again -> transient.
    respx.post(_EXECUTION_URL).mock(return_value=httpx.Response(200, json={"Result": []}))
    with pytest.raises(ProviderTransientError):
        _run_pull(connector, async_hooks, "pull_ttd_noexec")
    assert schedule_route.call_count == 2


@respx.mock
def test_expired_download_link_resubmits_once(connector, async_hooks):
    """Dead pre-signed link -> DownloadLinkExpired -> single resubmission."""
    from unittest.mock import patch

    _mock_auth()
    schedule_route = respx.post(_SCHEDULE_URL).mock(
        side_effect=[_schedule_response("s1"), _schedule_response("s2")]
    )
    fresh_url = _DOWNLOAD_URL.replace("report.csv", "report-2.csv")
    respx.post(_EXECUTION_URL).mock(
        side_effect=[
            _execution_response("Complete", url=_DOWNLOAD_URL),
            _execution_response("Complete", url=fresh_url),
        ]
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(403, text="expired"))
    respx.get(fresh_url).mock(return_value=httpx.Response(200, content=_CSV.encode("utf-8")))
    with patch.object(connector, "_insert_raw_rows", return_value=9):
        result = _run_pull(connector, async_hooks, "pull_ttd_dl")
    assert schedule_route.call_count == 2
    assert result["report_ref"] == "s2"


@respx.mock
def test_complete_without_deliveries_expires_never_serves_stale_url(
    connector, async_hooks
):
    """F-1: a Complete poll stashes url-A; a LATER Complete poll on the SAME run
    with an EMPTY ReportDeliveries must overwrite download_url to None so it
    raises DownloadLinkExpired (clean expired path) -- it must NEVER silently
    serve url-A's bytes (the OLD report). Here url-A downloads fine on the first
    Complete (single resubmission consumed elsewhere is not needed): we assert
    the stale link is not re-hit after the empty-deliveries Complete."""
    _mock_auth()
    schedule_route = respx.post(_SCHEDULE_URL).mock(
        side_effect=[_schedule_response("s1"), _schedule_response("s2")]
    )
    stale_url = _DOWNLOAD_URL  # url-A, stashed by the first Complete
    # Ref A: Complete WITH url-A -> download of url-A fails (dead link) -> socle
    #        resubmits ONCE (s2) -> Ref B: Complete WITHOUT deliveries -> F-1
    #        overwrites download_url to None -> DownloadLinkExpired again. The
    #        single resubmission is now exhausted -> ProviderTransientError.
    #        Crucially the stale url-A is hit EXACTLY ONCE (never re-served).
    respx.post(_EXECUTION_URL).mock(
        side_effect=[
            _execution_response("Complete", url=stale_url),
            _execution_response("Complete"),  # EMPTY ReportDeliveries
        ]
    )
    stale_route = respx.get(stale_url).mock(
        return_value=httpx.Response(403, text="expired")
    )
    with pytest.raises(ProviderTransientError):
        _run_pull(connector, async_hooks, "pull_ttd_stale")
    # The stale link is hit ONCE (the first, failed attempt) and NEVER re-served
    # after the empty-deliveries Complete overwrote download_url to None.
    assert stale_route.call_count == 1
    assert schedule_route.call_count == 2  # exactly one resubmission


@respx.mock
def test_complete_empty_deliveries_does_not_reuse_prior_run_url(
    connector, async_hooks, tmp_path, monkeypatch
):
    """F-1 (direct): the SECOND Complete carrying empty deliveries must clear the
    url stashed by the FIRST Complete. Without the fix download() would fetch
    url-A's bytes; with it, download() raises DownloadLinkExpired. We drive the
    flow callables directly to assert download() refuses to serve the stale url."""
    _mock_auth()
    from core import async_reports

    body = connector._build_schedule_body(
        "daily_performance", "adv-123",
        list(connector._template_specs()["daily_performance"]["dimensions"]),
        list(connector._template_specs()["daily_performance"]["metrics"]),
        _DATE_FROM, _DATE_TO,
    )
    flow = connector._build_flow("adv-123", "daily_performance", body)

    respx.post(_EXECUTION_URL).mock(
        side_effect=[
            _execution_response("Complete", url=_DOWNLOAD_URL),  # stashes url-A
            _execution_response("Complete"),  # empty -> must clear to None
        ]
    )
    assert flow.poll("s1") == async_reports.COMPLETED  # stashes url-A
    assert flow.poll("s1") == async_reports.COMPLETED  # empty deliveries
    with pytest.raises(async_reports.DownloadLinkExpired):
        flow.download("s1")  # url-A must NOT be served


@respx.mock
def test_401_on_submit_reauths_once_then_succeeds(connector, async_hooks):
    """A 401 on the schedule call clears the token cache and re-authenticates
    ONCE (ExpirationDateUtc passed mid-flow) before succeeding."""
    from unittest.mock import patch

    auth_route = respx.post(_AUTH_URL).mock(return_value=_auth_response())
    respx.post(_SCHEDULE_URL).mock(
        side_effect=[
            httpx.Response(401, json={"Message": "token expired"}),
            _schedule_response("s-ok"),
        ]
    )
    respx.post(_EXECUTION_URL).mock(
        return_value=_execution_response("Complete", url=_DOWNLOAD_URL)
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, content=_CSV.encode("utf-8")))
    with patch.object(connector, "_insert_raw_rows", return_value=9):
        result = _run_pull(connector, async_hooks, "pull_ttd_401ok")
    assert result["report_ref"] == "s-ok"
    # Auth called twice: initial mint + the re-auth after the 401.
    assert auth_route.call_count == 2


@respx.mock
def test_401_persisting_after_reauth_classifies_auth_expired(connector, async_hooks):
    respx.post(_AUTH_URL).mock(return_value=_auth_response())
    respx.post(_SCHEDULE_URL).mock(
        return_value=httpx.Response(401, json={"Message": "token invalid"})
    )
    with pytest.raises(AuthExpiredError) as exc:
        _run_pull(connector, async_hooks, "pull_ttd_401ko")
    assert exc.value.provider_status == 401


@respx.mock
def test_401_revoked_body_token_maps_to_auth_revoked(connector, async_hooks):
    from core.pull_errors import AuthRevokedError

    respx.post(_AUTH_URL).mock(return_value=_auth_response())
    respx.post(_SCHEDULE_URL).mock(
        return_value=httpx.Response(401, json={"Message": "credential revoked"})
    )
    with pytest.raises(AuthRevokedError):
        _run_pull(connector, async_hooks, "pull_ttd_revoked")


@respx.mock
def test_403_maps_to_permission_denied_with_payload(connector, async_hooks):
    _mock_auth()
    respx.post(_SCHEDULE_URL).mock(
        return_value=httpx.Response(
            403, json={"Message": "advertiser adv-123 not permitted for this token"}
        )
    )
    with pytest.raises(PermissionDeniedError) as exc:
        _run_pull(connector, async_hooks, "pull_ttd_403")
    assert exc.value.provider_status == 403
    assert "not permitted" in str(exc.value.provider_payload)


@respx.mock
def test_429_on_submit_raises_rate_limit_error_breaker(connector, async_hooks):
    _mock_auth()
    respx.post(_SCHEDULE_URL).mock(return_value=httpx.Response(429, json={}))
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as exc:
        _run_pull(connector, async_hooks, "pull_ttd_429")
    # TTD publishes no Retry-After -> default backoff (retry_after None).
    assert exc.value.retry_after is None
    assert exc.value.platform == "thetradedesk"


@respx.mock
def test_404_status_only_key_maps_to_invalid_request(connector, async_hooks):
    _mock_auth()
    respx.post(_SCHEDULE_URL).mock(
        return_value=httpx.Response(404, json={"Message": "unknown schedule"})
    )
    with pytest.raises(InvalidRequestError):
        _run_pull(connector, async_hooks, "pull_ttd_404")
