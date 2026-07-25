"""Story 26.5 -- async report flow through the 26.1 socle (fake clock, respx).

Covers: the happy submit -> poll -> download -> LONG landing path (request
shape + headers on the wire), poll-time transient 401s (x5 budget), the 425
dedup contract (socle resume + residual provider 425), FAILED -> single
resubmission -> provider_transient, configuration FAILED -> invalid_request,
deadline deferral and fake-clock resumption (re-poll, never resubmit).
No real sleeps: the socle clock/sleeper/store are the 26.1 fakes.
"""

from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
import respx
from core.pull_errors import (
    AuthExpiredError,
    InvalidRequestError,
    ProviderTransientError,
)

from .conftest import HOSTS

_NA = HOSTS["NA"]
_REPORTS_URL = f"{_NA}/reporting/reports"
_S3_URL = "https://offline-report-storage.s3.amazonaws.com/report.json.gz"

_DATE_FROM = (date.today() - timedelta(days=3)).isoformat()
_DATE_TO = (date.today() - timedelta(days=1)).isoformat()

_ROWS = [
    {
        "date": _DATE_FROM,
        "campaignId": 111222333,
        "campaignName": "SP - Brand - FR",
        "campaignStatus": "ENABLED",
        "campaignBudgetCurrencyCode": "EUR",
        "impressions": 12040,
        "clicks": 640,
        "cost": 118.4,
        "purchases14d": 42,
        "purchases30d": 45,
        "sales14d": 3120.75,
        "sales30d": 3300.5,
    }
]


def _gzip_body(rows) -> bytes:
    return gzip.compress(json.dumps(rows).encode("utf-8"))


def _created(report_id: str = "r1") -> httpx.Response:
    return httpx.Response(200, json={"reportId": report_id, "status": "PENDING"})


def _status(status: str, report_id: str = "r1", url: str | None = None,
            failure_reason: str | None = None) -> httpx.Response:
    payload: dict = {"reportId": report_id, "status": status}
    if url:
        payload["url"] = url
    if failure_reason:
        payload["failureReason"] = failure_reason
    return httpx.Response(200, json=payload)


def _run_pull(connector, hooks, pull_id: str, **kwargs):
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        return connector.pull_sp_campaigns_daily(
            connection_id="conn-az",
            date_from=_DATE_FROM,
            date_to=_DATE_TO,
            project_id="p",
            pull_id=pull_id,
            account_id="NA:1234",
            **hooks,
            **kwargs,
        )


@respx.mock
def test_happy_path_submit_poll_download_and_long_landing(
    connector, async_hooks, tmp_path, monkeypatch
):
    """Body v3 + headers on the wire; GZIP decompressed; LONG rows landed."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "az.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    submit_route = respx.post(_REPORTS_URL).mock(return_value=_created())
    respx.get(f"{_REPORTS_URL}/r1").mock(
        side_effect=[_status("PENDING"), _status("COMPLETED", url=_S3_URL)]
    )
    download_route = respx.get(_S3_URL).mock(
        return_value=httpx.Response(200, content=_gzip_body(_ROWS))
    )

    result = _run_pull(connector, async_hooks, "pull_az_t1")

    # 12 metric values? 7 numeric bundle metrics land long.
    assert result["row_count"] == 7
    assert result["report_ref"] == "r1"

    req = submit_route.calls.last.request
    assert req.headers["content-type"] == "application/vnd.createasyncreportrequest.v3+json"
    assert req.headers["authorization"] == "Bearer tok"
    assert req.headers["amazon-ads-clientid"].startswith("amzn1.application-oa2-client")
    assert req.headers["amazon-advertising-api-scope"] == "1234"
    body = json.loads(req.content)
    cfg = body["configuration"]
    assert cfg["adProduct"] == "SPONSORED_PRODUCTS"
    assert cfg["reportTypeId"] == "spCampaigns"
    assert cfg["groupBy"] == ["campaign"]
    assert cfg["timeUnit"] == "DAILY"
    assert cfg["format"] == "GZIP_JSON"
    for column in ("date", "campaignId", "campaignName", "campaignStatus",
                   "campaignBudgetCurrencyCode", "impressions", "clicks", "cost",
                   "purchases14d", "purchases30d", "sales14d", "sales30d"):
        assert column in cfg["columns"], column

    # Pre-signed download carries NO auth headers.
    dl_req = download_route.calls.last.request
    assert "authorization" not in dl_req.headers

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, value_num, data_level, ad_product, region, profile_id, "
        "campaign_id, cost_source_currency FROM raw_amazon_ads_daily "
        "WHERE pull_id = 'pull_az_t1' ORDER BY metric"
    ).fetchall()
    con.close()
    by_metric = {r[0]: r[1] for r in rows}
    assert by_metric["cost"] == pytest.approx(118.4)
    assert by_metric["impressions"] == 12040
    assert by_metric["purchases_14d"] == 42
    assert by_metric["sales_30d"] == pytest.approx(3300.5)
    assert all(
        r[2] == "spCampaigns" and r[3] == "SPONSORED_PRODUCTS" and r[4] == "NA"
        and r[5] == "1234" and r[6] == "111222333" and r[7] == "EUR"
        for r in rows
    )


@respx.mock
def test_poll_transient_401_retried_five_times_then_success(connector, async_hooks):
    respx.post(_REPORTS_URL).mock(return_value=_created())
    respx.get(f"{_REPORTS_URL}/r1").mock(
        side_effect=[httpx.Response(401, json={"code": "401", "details": "x"})] * 5
        + [_status("COMPLETED", url=_S3_URL)]
    )
    respx.get(_S3_URL).mock(return_value=httpx.Response(200, content=_gzip_body(_ROWS)))

    with patch.object(connector, "_insert_raw_rows", return_value=7):
        result = _run_pull(connector, async_hooks, "pull_az_401ok")
    assert result["row_count"] == 7


@respx.mock
def test_poll_401_over_budget_classifies_auth_expired(connector, async_hooks):
    respx.post(_REPORTS_URL).mock(return_value=_created())
    respx.get(f"{_REPORTS_URL}/r1").mock(
        return_value=httpx.Response(401, json={"code": "401", "details": "expired"})
    )
    with pytest.raises(AuthExpiredError) as exc:
        _run_pull(connector, async_hooks, "pull_az_401ko")
    assert exc.value.provider_status == 401


@respx.mock
def test_deadline_deferred_then_fake_clock_resume_by_repoll(
    connector, async_hooks, ref_store
):
    """Reprise: run 1 defers past the deadline (typed retryable, ref stays
    in flight); run 2 with the SAME request hash RESUMES by re-poll -- the
    provider sees exactly ONE submission."""
    submit_route = respx.post(_REPORTS_URL).mock(return_value=_created())
    poll_route = respx.get(f"{_REPORTS_URL}/r1").mock(
        side_effect=[_status("PENDING"), _status("PROCESSING"),
                     _status("COMPLETED", url=_S3_URL)]
    )
    respx.get(_S3_URL).mock(return_value=httpx.Response(200, content=_gzip_body(_ROWS)))

    hooks_run1 = dict(async_hooks)
    hooks_run1["_deadline_seconds"] = 1  # defer after the first PENDING poll
    with pytest.raises(ProviderTransientError, match="deferred"):
        _run_pull(connector, hooks_run1, "pull_az_defer")
    assert submit_route.call_count == 1
    # The reference is persisted in flight for the next run.
    stored = ref_store._rows[next(iter(ref_store._rows))]
    assert stored["state"] == "in_flight"
    assert stored["report_ref"] == "r1"

    with patch.object(connector, "_insert_raw_rows", return_value=7):
        result = _run_pull(connector, async_hooks, "pull_az_resume")
    assert result["resumed"] is True
    assert submit_route.call_count == 1  # NEVER resubmitted
    assert poll_route.call_count == 3


@respx.mock
def test_residual_provider_425_reuses_reference_never_resubmits(
    connector, async_hooks
):
    """A residual provider 425 (identical request in flight at the provider)
    reuses the reportId from the 425 body -- never a blind resubmission."""
    respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(
            425, json={"reportId": "r-dup", "detail": "Duplicate request"}
        )
    )
    respx.get(f"{_REPORTS_URL}/r-dup").mock(
        return_value=_status("COMPLETED", "r-dup", url=_S3_URL)
    )
    respx.get(_S3_URL).mock(return_value=httpx.Response(200, content=_gzip_body(_ROWS)))

    with patch.object(connector, "_insert_raw_rows", return_value=7):
        result = _run_pull(connector, async_hooks, "pull_az_425")
    assert result["report_ref"] == "r-dup"


@respx.mock
def test_residual_425_without_recoverable_ref_is_typed_transient(
    connector, async_hooks
):
    respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(425, json={"detail": "Duplicate request"})
    )
    with pytest.raises(ProviderTransientError, match="425"):
        _run_pull(connector, async_hooks, "pull_az_425b")


@respx.mock
def test_failed_report_resubmitted_once_then_provider_transient(
    connector, async_hooks
):
    """Non-configuration FAILED -> canonical expired -> ONE resubmission ->
    second FAILED -> typed provider_transient (story contract)."""
    submit_route = respx.post(_REPORTS_URL).mock(
        side_effect=[_created("r1"), _created("r2")]
    )
    respx.get(f"{_REPORTS_URL}/r1").mock(
        return_value=_status("FAILED", "r1", failure_reason="internal error")
    )
    respx.get(f"{_REPORTS_URL}/r2").mock(
        return_value=_status("FAILED", "r2", failure_reason="internal error")
    )
    with pytest.raises(ProviderTransientError):
        _run_pull(connector, async_hooks, "pull_az_failed")
    assert submit_route.call_count == 2  # exactly one resubmission


@respx.mock
def test_failed_with_configuration_reason_is_invalid_request(connector, async_hooks):
    respx.post(_REPORTS_URL).mock(return_value=_created())
    respx.get(f"{_REPORTS_URL}/r1").mock(
        return_value=_status(
            "FAILED", failure_reason="column notAColumn is invalid for spCampaigns"
        )
    )
    with pytest.raises(InvalidRequestError, match="failureReason"):
        _run_pull(connector, async_hooks, "pull_az_cfg")


@respx.mock
def test_expired_download_link_resubmits_once(connector, async_hooks):
    """Dead pre-signed link -> DownloadLinkExpired -> single resubmission."""
    submit_route = respx.post(_REPORTS_URL).mock(
        side_effect=[_created("r1"), _created("r2")]
    )
    respx.get(f"{_REPORTS_URL}/r1").mock(
        return_value=_status("COMPLETED", "r1", url=_S3_URL)
    )
    fresh_url = _S3_URL.replace("report.json.gz", "report-2.json.gz")
    respx.get(f"{_REPORTS_URL}/r2").mock(
        return_value=_status("COMPLETED", "r2", url=fresh_url)
    )
    respx.get(_S3_URL).mock(return_value=httpx.Response(403, text="expired"))
    respx.get(fresh_url).mock(
        return_value=httpx.Response(200, content=_gzip_body(_ROWS))
    )
    with patch.object(connector, "_insert_raw_rows", return_value=7):
        result = _run_pull(connector, async_hooks, "pull_az_dl")
    assert submit_route.call_count == 2
    assert result["report_ref"] == "r2"


@respx.mock
def test_429_on_submit_raises_rate_limit_error_with_retry_after(
    connector, async_hooks
):
    respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "42"}, json={})
    )
    from core.quota import RateLimitError

    with pytest.raises(RateLimitError) as exc:
        _run_pull(connector, async_hooks, "pull_az_429")
    assert exc.value.retry_after == 42
    assert exc.value.platform == "amazon-ads"


@respx.mock
def test_403_maps_to_permission_denied_with_payload(connector, async_hooks):
    from core.pull_errors import PermissionDeniedError

    respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(
            403, json={"code": "403", "details": "Not authorized to access scope 1234"}
        )
    )
    with pytest.raises(PermissionDeniedError) as exc:
        _run_pull(connector, async_hooks, "pull_az_403")
    assert exc.value.provider_status == 403
    assert "Not authorized" in str(exc.value.provider_payload)


@respx.mock
def test_kdp_details_refinement_permission_denied(connector, async_hooks):
    from core.pull_errors import PermissionDeniedError

    respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "code": "400",
                "details": "KDP authors do not have access to Sponsored Brands functionality",
            },
        )
    )
    with pytest.raises(PermissionDeniedError):
        _run_pull(connector, async_hooks, "pull_az_kdp")


@respx.mock
def test_catalog_daily_selection_becomes_report_type_group_by_columns(
    connector, async_hooks, tmp_path, monkeypatch
):
    """Story DoD: catalog_daily maps the SELECTION -> reportTypeId + groupBy +
    columns in the v3 body (request-shape, mocked)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "az_cat.duckdb"))
    s3 = _S3_URL.replace("report.json.gz", "catalog.json.gz")
    submit_route = respx.post(_REPORTS_URL).mock(return_value=_created("r-cat"))
    respx.get(f"{_REPORTS_URL}/r-cat").mock(
        return_value=_status("COMPLETED", "r-cat", url=s3)
    )
    respx.get(s3).mock(
        return_value=httpx.Response(
            200,
            content=_gzip_body(
                [{"date": _DATE_FROM, "searchTerm": "running shoes",
                  "keywordId": 42, "campaignId": 7, "adGroupId": 8,
                  "impressions": 100, "clicks": 3, "cost": 1.2}]
            ),
        )
    )

    selection = {
        "metrics": ["impressions", "clicks", "cost"],
        "dimensions": ["date", "searchTerm", "keywordId", "campaignId", "adGroupId"],
        "source_fields": {},
    }
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_catalog_daily(
            connection_id="conn-az",
            date_from=_DATE_FROM,
            date_to=_DATE_TO,
            project_id="p",
            pull_id="pull_az_cat",
            selection=selection,
            account_id="NA:1234",
            **async_hooks,
        )

    assert result["report_type"] == "spSearchTerm"
    body = json.loads(submit_route.calls.last.request.content)
    cfg = body["configuration"]
    assert cfg["reportTypeId"] == "spSearchTerm"
    assert cfg["adProduct"] == "SPONSORED_PRODUCTS"
    assert cfg["groupBy"] == ["searchTerm"]
    for column in ("date", "searchTerm", "keywordId", "campaignId", "adGroupId",
                   "impressions", "clicks", "cost"):
        assert column in cfg["columns"], column
    # 3 numeric metrics landed long for the single row.
    assert result["row_count"] == 3


def _core_resolved_default(connector):
    """The catalog tier-core default AS THE CORE QUEUE RESOLVES IT.

    core/queue.py resolves a selection=None catalog_driven job to
    validate_selection(catalog, catalog_default_selection(catalog)) and passes
    it as an EXPLICIT selection -- the exact shape the module must recognise and
    prune (review 26.5 F-1). Rebuilt here from the SAME core helpers, without
    reaching into the connector's private cache.
    """
    import json as _json
    from pathlib import Path as _Path

    from core.catalog_contract import (
        catalog_default_selection,
        validate_selection,
    )

    catalog = _json.loads(
        (_Path(connector.__file__).parent / "api_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    resolved, issues = validate_selection(catalog, catalog_default_selection(catalog))
    assert issues == []
    return resolved


@respx.mock
def test_catalog_daily_selection_none_submits_valid_spcampaigns(
    connector, async_hooks
):
    """F-1: selection=None (the profile-less default) prunes the core-tier
    default to spCampaigns + campaign and submits a VALID report -- no
    'no single sponsored report type covers the selection' refusal."""
    submit_route = respx.post(_REPORTS_URL).mock(return_value=_created("r-def1"))
    respx.get(f"{_REPORTS_URL}/r-def1").mock(
        return_value=_status("COMPLETED", "r-def1", url=_S3_URL)
    )
    respx.get(_S3_URL).mock(return_value=httpx.Response(200, content=_gzip_body(_ROWS)))

    with patch.object(connector, "_insert_raw_rows", return_value=7):
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            result = connector.pull_catalog_daily(
                connection_id="conn-az",
                date_from=_DATE_FROM,
                date_to=_DATE_TO,
                project_id="p",
                pull_id="pull_az_def1",
                selection=None,
                account_id="NA:1234",
                **async_hooks,
            )
    assert result["report_type"] == "spCampaigns"
    cfg = json.loads(submit_route.calls.last.request.content)["configuration"]
    assert cfg["reportTypeId"] == "spCampaigns"
    assert cfg["groupBy"] == ["campaign"]


@respx.mock
def test_catalog_daily_core_resolved_default_submits_valid_spcampaigns(
    connector, async_hooks
):
    """F-1 (the queue path): the core-RESOLVED tier-core default (what
    core/queue.py passes as an EXPLICIT selection when the job carries none)
    is recognised as the prunable default and submits a VALID spCampaigns
    report -- NOT refused as an unresolvable user selection."""
    resolved_default = _core_resolved_default(connector)
    # Sanity: the raw core default spans multiple report types (searchTerm AND
    # purchasedAsin), so WITHOUT the F-1 pruning it would be unresolvable.
    dims = set(resolved_default["dimensions"])
    assert {"searchTerm", "purchasedAsin"} & dims

    submit_route = respx.post(_REPORTS_URL).mock(return_value=_created("r-def2"))
    respx.get(f"{_REPORTS_URL}/r-def2").mock(
        return_value=_status("COMPLETED", "r-def2", url=_S3_URL)
    )
    respx.get(_S3_URL).mock(return_value=httpx.Response(200, content=_gzip_body(_ROWS)))

    with patch.object(connector, "_insert_raw_rows", return_value=7):
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            result = connector.pull_catalog_daily(
                connection_id="conn-az",
                date_from=_DATE_FROM,
                date_to=_DATE_TO,
                project_id="p",
                pull_id="pull_az_def2",
                selection=resolved_default,
                account_id="NA:1234",
                **async_hooks,
            )
    assert result["report_type"] == "spCampaigns"
    cfg = json.loads(submit_route.calls.last.request.content)["configuration"]
    assert cfg["reportTypeId"] == "spCampaigns"
    assert cfg["groupBy"] == ["campaign"]
    # Every submitted column is legal on spCampaigns/campaign (pruned, not user).
    allowed = set(
        json.loads(
            (
                connector._MODULE_DIR
                / "catalog_sources"
                / "report_type_columns.json"
            ).read_text(encoding="utf-8")
        )["spCampaigns"]
    )
    assert set(cfg["columns"]) <= allowed


@respx.mock
def test_404_status_only_key_maps_to_invalid_request(connector, async_hooks):
    """The manifest status-only error_map (module-side decision) covers 404,
    which core's pure-HTTP fallback would leave unclassified."""
    respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(404, json={"code": "404", "details": "not found"})
    )
    with pytest.raises(InvalidRequestError):
        _run_pull(connector, async_hooks, "pull_az_404")
