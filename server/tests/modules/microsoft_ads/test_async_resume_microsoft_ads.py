"""Story 26.4 -- async resumption through the 26.1 socle (fake clock).

Proves the module runs EXCLUSIVELY through core.async_reports.run_async_report:
  - a run that exhausts its polling budget raises a RETRYABLE
    ProviderTransientError (cross-review fix: core/queue.py never reads
    result["status"], so a deferred RETURN would mark the job done at 0 rows
    and an off-ladder window would never be re-pulled) with the
    ReportRequestId persisted in flight;
  - the retry with the same request hash RESUMES BY RE-POLL (zero new
    Submit) and downloads IMMEDIATELY after the completing poll;
  - a dead download URL (5-minute expiry) consumes the SINGLE allowed
    resubmission, then completes.

No real sleeps: the injected sleeper advances the fake clock (26.1 pattern).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
import respx
from core.async_reports import InMemoryReportRefStore

from .conftest import (
    ACCOUNT_ID,
    COMPOSITE_ACCOUNT,
    DOWNLOAD_URL,
    POLL_URL,
    SUBMIT_URL,
    FakeClock,
    make_report_zip,
)

_HEADERS = [
    "TimePeriod", "AccountId", "CampaignId", "CampaignName", "CampaignType",
    "CampaignStatus", "CurrencyCode", "Spend", "Impressions", "Clicks",
    "Conversions", "Revenue",
]
_ROW = ["2026-07-01", ACCOUNT_ID, "22334455", "Brand", "Search", "Active",
        "EUR", "10.0", "100", "10", "1", "20"]


def _pull(connector, store, clockobj, **kwargs):
    return connector.pull_campaign_daily(
        connection_id="c",
        date_from="2026-07-01",
        date_to="2026-07-01",
        project_id="p",
        pull_id=kwargs.pop("pull_id", "pull_ms_async"),
        account_id=COMPOSITE_ACCOUNT,
        report_store=store,
        clock=clockobj.clock,
        sleeper=clockobj.sleep,
        **kwargs,
    )


def _poll_response(status: str, url: str | None = None) -> httpx.Response:
    payload: dict = {"ReportRequestStatus": {"Status": status}}
    if url is not None:
        payload["ReportRequestStatus"]["ReportDownloadUrl"] = url
    return httpx.Response(200, json=payload)


def test_deferred_run_raises_retryable_transient_then_retry_repolls_same_ref(
    connector, tmp_path, monkeypatch
):
    """Run 1: provider stays Pending past the deadline -> RETRYABLE
    ProviderTransientError (NEVER a {"status": "deferred"} return:
    core/queue.py does not read result["status"] and would mark the job done
    at 0 rows -- a permanent hole for off-ladder backfill windows), ONE
    submit, ref persisted. Run 2 (same store/hash): ZERO submits, re-poll of
    the SAME ReportRequestId, Success -> immediate download -> landing."""
    from core.pull_errors import ProviderTransientError

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "ms_resume.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    clock = FakeClock()
    store = InMemoryReportRefStore(clock.clock)

    # --- Run 1: always Pending, tight deadline -> typed retryable transient.
    with respx.mock:
        submit = respx.post(SUBMIT_URL).mock(
            return_value=httpx.Response(200, json={"ReportRequestId": "rr_slow"})
        )
        respx.post(POLL_URL).mock(return_value=_poll_response("Pending"))
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            with pytest.raises(ProviderTransientError) as exc:
                _pull(
                    connector, store, clock, deadline_seconds=40,
                    pull_id="pull_ms_run1",
                )
        assert submit.call_count == 1

    # Evidence for the operator AND the retry: the persisted reference and
    # the dedup request_hash both survive in the typed error.
    assert exc.value.error_class == "provider_transient"
    assert exc.value.provider_payload["report_ref"] == "rr_slow"
    request_hash = exc.value.provider_payload["request_hash"]
    assert request_hash and request_hash in str(exc.value)
    assert "re-poll" in str(exc.value)
    # Progressive socle backoff drove the waiting (15s then 30s ladder).
    assert clock.sleeps and clock.sleeps[0] == 15

    # --- Run 2: same request -> resume by re-poll; download <= 5 min after
    # the completing poll (the URL is only surfaced by that poll).
    with respx.mock:
        submit2 = respx.post(SUBMIT_URL).mock(
            return_value=httpx.Response(200, json={"ReportRequestId": "rr_NEW"})
        )
        poll2 = respx.post(POLL_URL).mock(
            return_value=_poll_response("Success", DOWNLOAD_URL)
        )
        download = respx.get(DOWNLOAD_URL).mock(
            return_value=httpx.Response(
                200, content=make_report_zip(_HEADERS, [_ROW])
            )
        )
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            result2 = _pull(connector, store, clock, pull_id="pull_ms_run2")

        # NO BLIND RESUBMISSION: the second run never touched Submit.
        assert submit2.call_count == 0
        assert (
            json.loads(poll2.calls.last.request.content)["ReportRequestId"]
            == "rr_slow"
        )
        assert download.call_count == 1

    assert result2["status"] == "completed"
    assert result2["report_ref"] == "rr_slow"
    assert result2["row_count"] == 5

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    count = con.execute(
        "SELECT COUNT(*) FROM raw_microsoft_ads_daily "
        "WHERE pull_id = 'pull_ms_run2'"
    ).fetchone()[0]
    con.close()
    assert count == 5


def test_dead_download_url_consumes_single_resubmission_then_completes(
    connector, tmp_path, monkeypatch
):
    """5-minute URL expiry: a dead link (HTTP 410) maps to DownloadLinkExpired
    -> exactly ONE resubmission, fresh poll, fresh URL, completion."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ms_dead.duckdb"))

    clock = FakeClock()
    store = InMemoryReportRefStore(clock.clock)
    fresh_url = "https://bingadsreports.test/download/fresh.zip"

    with respx.mock:
        submit = respx.post(SUBMIT_URL).mock(
            side_effect=[
                httpx.Response(200, json={"ReportRequestId": "rr_a"}),
                httpx.Response(200, json={"ReportRequestId": "rr_b"}),
            ]
        )
        respx.post(POLL_URL).mock(
            side_effect=lambda request: _poll_response(
                "Success",
                DOWNLOAD_URL
                if json.loads(request.content)["ReportRequestId"] == "rr_a"
                else fresh_url,
            )
        )
        respx.get(DOWNLOAD_URL).mock(return_value=httpx.Response(410))
        respx.get(fresh_url).mock(
            return_value=httpx.Response(
                200, content=make_report_zip(_HEADERS, [_ROW])
            )
        )
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            result = _pull(connector, store, clock, pull_id="pull_ms_dead")

        assert submit.call_count == 2  # one original + the SINGLE resubmission

    assert result["status"] == "completed"
    assert result["report_ref"] == "rr_b"


def test_purged_report_ref_polls_as_expired_and_resubmits(
    connector, tmp_path, monkeypatch
):
    """26.1 F-3 contract: Poll answering 400 with body code 2100 (invalid
    report id) on a previously valid reference maps to canonical 'expired'
    -> single resubmission, never a hard failure."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ms_purge.duckdb"))

    clock = FakeClock()
    store = InMemoryReportRefStore(clock.clock)

    from .conftest import fault_body

    with respx.mock:
        submit = respx.post(SUBMIT_URL).mock(
            side_effect=[
                httpx.Response(200, json={"ReportRequestId": "rr_old"}),
                httpx.Response(200, json={"ReportRequestId": "rr_fresh"}),
            ]
        )
        respx.post(POLL_URL).mock(
            side_effect=lambda request: (
                httpx.Response(400, json=fault_body(2100, "InvalidReportId"))
                if json.loads(request.content)["ReportRequestId"] == "rr_old"
                else _poll_response("Success", DOWNLOAD_URL)
            )
        )
        respx.get(DOWNLOAD_URL).mock(
            return_value=httpx.Response(
                200, content=make_report_zip(_HEADERS, [_ROW])
            )
        )
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            result = _pull(connector, store, clock, pull_id="pull_ms_purged")

        assert submit.call_count == 2

    assert result["status"] == "completed"
    assert result["report_ref"] == "rr_fresh"


def test_success_without_download_url_lands_zero_rows_honestly(
    connector, tmp_path, monkeypatch
):
    """Success with no ReportDownloadUrl = report generated empty: zero rows,
    completed status (absence stays distinguishable from failure, AD-9)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ms_empty.duckdb"))

    clock = FakeClock()
    store = InMemoryReportRefStore(clock.clock)

    with respx.mock:
        respx.post(SUBMIT_URL).mock(
            return_value=httpx.Response(200, json={"ReportRequestId": "rr_empty"})
        )
        respx.post(POLL_URL).mock(return_value=_poll_response("Success"))
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            result = _pull(connector, store, clock, pull_id="pull_ms_empty")

    assert result["status"] == "completed"
    assert result["row_count"] == 0


def test_provider_error_status_raises_typed_transient(
    connector, tmp_path, monkeypatch
):
    """Poll Status=Error -> canonical failed -> ProviderTransientError
    (retryable class; the queue's standard policy applies)."""
    from core.pull_errors import ProviderTransientError

    clock = FakeClock()
    store = InMemoryReportRefStore(clock.clock)

    with respx.mock:
        respx.post(SUBMIT_URL).mock(
            return_value=httpx.Response(200, json={"ReportRequestId": "rr_err"})
        )
        respx.post(POLL_URL).mock(return_value=_poll_response("Error"))
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            with pytest.raises(ProviderTransientError):
                _pull(connector, store, clock, pull_id="pull_ms_err")
