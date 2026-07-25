"""Story 28.2 -- catalog_driven request shape + field_compat pre-call refusals.

The selection -> ReportTemplate + dimensions/metrics mapping in the schedule
body (request-shape, mocked), the per-template field_compat selectable_set
refusal BEFORE any API call, the excluded (enrichment-only) refusal, and the
selection=None tier-core-default pruning.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest
import respx
from core.pull_errors import InvalidRequestError

from .conftest import API_HOST

_AUTH_URL = f"{API_HOST}/v3/authentication"
_SCHEDULE_URL = f"{API_HOST}/v3/myreports/reportschedule"
_EXECUTION_URL = f"{API_HOST}/v3/myreports/reportexecution/query/advertisers"
_DOWNLOAD_URL = "https://ttd-report-storage.s3.amazonaws.com/cat.csv"

_DATE_FROM = (date.today() - timedelta(days=3)).isoformat()
_DATE_TO = (date.today() - timedelta(days=1)).isoformat()
_ACCOUNT = "partner-xyz:adv-123"


def _mock_auth():
    from datetime import datetime, timezone

    exp = (datetime.now(tz=timezone.utc) + timedelta(hours=6)).isoformat().replace(
        "+00:00", "Z"
    )
    respx.post(_AUTH_URL).mock(
        return_value=httpx.Response(200, json={"Token": "ttd-tok", "ExpirationDateUtc": exp})
    )


@respx.mock
def test_catalog_daily_selection_becomes_template_dimensions_metrics(
    connector, async_hooks, tmp_path, monkeypatch
):
    """Story DoD: catalog_daily maps the SELECTION -> ReportTemplate +
    dimensions + metrics in the schedule body (request-shape, mocked)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "ttd_cat.duckdb"))
    _mock_auth()
    schedule_route = respx.post(_SCHEDULE_URL).mock(
        return_value=httpx.Response(200, json={"ReportScheduleId": "s-cat"})
    )
    respx.post(_EXECUTION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"Result": [{"ReportExecutionState": "Complete",
                              "ReportDeliveries": [{"DownloadURL": _DOWNLOAD_URL}]}]},
        )
    )
    respx.get(_DOWNLOAD_URL).mock(
        return_value=httpx.Response(
            200,
            content=(
                "date,CampaignId,AdGroupId,CreativeId,AdFormat,Impressions,Clicks\n"
                f"{_DATE_FROM},cmp-1,ag-1,cr-1,300x250,100,3\n"
            ).encode("utf-8"),
        )
    )

    selection = {
        "metrics": ["Impressions", "Clicks"],
        "dimensions": ["date", "CampaignId", "AdGroupId", "CreativeId", "AdFormat"],
        "source_fields": {},
    }
    result = connector.pull_catalog_daily(
        connection_id="conn-ttd",
        date_from=_DATE_FROM,
        date_to=_DATE_TO,
        project_id="p",
        pull_id="pull_ttd_cat",
        selection=selection,
        account_id=_ACCOUNT,
        **async_hooks,
    )

    assert result["report_template"] == "creative"
    body = json.loads(schedule_route.calls.last.request.content)
    assert body["ReportTemplateName"] == "creative"
    cfg = body["Configuration"]
    assert set(cfg["Metrics"]) == {"Impressions", "Clicks"}
    for dim in ("date", "CampaignId", "AdGroupId", "CreativeId", "AdFormat"):
        assert dim in cfg["Dimensions"], dim
    # 2 numeric metrics landed long for the single row.
    assert result["row_count"] == 2


@respx.mock
def test_field_compat_refuses_column_absent_from_template_pre_call(
    connector, async_hooks
):
    """A column NOT in the resolved template's selectable_set is refused BEFORE
    any API call (typed invalid_request carrying the rule id) -- no schedule
    POST is ever issued."""
    _mock_auth()
    schedule_route = respx.post(_SCHEDULE_URL).mock(
        return_value=httpx.Response(200, json={"ReportScheduleId": "never"})
    )
    # video_player template explicitly requested, but Impressions is NOT in its
    # selectable_set -> pre-call refusal.
    selection = {
        "metrics": ["PlayerViews", "Impressions"],
        "dimensions": ["date", "AdGroupId", "CreativeId"],
        "source_fields": {},
    }
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            connection_id="conn-ttd",
            date_from=_DATE_FROM,
            date_to=_DATE_TO,
            project_id="p",
            pull_id="pull_ttd_compat",
            selection=selection,
            account_id=_ACCOUNT,
            report_template="video_player",
            **async_hooks,
        )
    assert "selectable_video_player" in str(exc.value)
    assert schedule_route.call_count == 0


@respx.mock
def test_selection_with_excluded_enrichment_only_column_refused(connector, async_hooks):
    """A selected excluded (enrichment-only) column is a typed refusal carrying
    its reason -- refused before any API call."""
    _mock_auth()
    schedule_route = respx.post(_SCHEDULE_URL).mock(
        return_value=httpx.Response(200, json={"ReportScheduleId": "never"})
    )
    selection = {
        "metrics": ["Impressions"],
        "dimensions": ["date", "dataSourceName"],
        "source_fields": {},
    }
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            connection_id="conn-ttd",
            date_from=_DATE_FROM,
            date_to=_DATE_TO,
            project_id="p",
            pull_id="pull_ttd_excl",
            selection=selection,
            account_id=_ACCOUNT,
            **async_hooks,
        )
    assert "enrichment-only" in str(exc.value)
    assert "dataSourceName" in str(exc.value)
    assert schedule_route.call_count == 0


@respx.mock
def test_unresolvable_selection_typed_refusal_lists_missing(connector, async_hooks):
    """A selection spanning columns no single managed template covers is a
    typed refusal naming the closest candidate's missing columns."""
    _mock_auth()
    respx.post(_SCHEDULE_URL).mock(
        return_value=httpx.Response(200, json={"ReportScheduleId": "never"})
    )
    # Player metric (video_player only) + Country (geo_platform only): no single
    # template covers both.
    selection = {
        "metrics": ["PlayerViews", "Impressions"],
        "dimensions": ["date", "Country"],
        "source_fields": {},
    }
    with pytest.raises(InvalidRequestError, match="no single managed ReportTemplate"):
        connector.pull_catalog_daily(
            connection_id="conn-ttd",
            date_from=_DATE_FROM,
            date_to=_DATE_TO,
            project_id="p",
            pull_id="pull_ttd_unres",
            selection=selection,
            account_id=_ACCOUNT,
            **async_hooks,
        )


@respx.mock
def test_catalog_daily_selection_none_submits_daily_performance(connector, async_hooks):
    """selection=None (the profile-less default) prunes the core-tier default to
    the daily_performance template and submits a VALID report."""
    from unittest.mock import patch

    _mock_auth()
    schedule_route = respx.post(_SCHEDULE_URL).mock(
        return_value=httpx.Response(200, json={"ReportScheduleId": "s-def"})
    )
    respx.post(_EXECUTION_URL).mock(
        return_value=httpx.Response(
            200,
            json={"Result": [{"ReportExecutionState": "Complete",
                              "ReportDeliveries": [{"DownloadURL": _DOWNLOAD_URL}]}]},
        )
    )
    respx.get(_DOWNLOAD_URL).mock(
        return_value=httpx.Response(200, content=b"date\n" + _DATE_FROM.encode() + b"\n")
    )
    with patch.object(connector, "_insert_raw_rows", return_value=0):
        result = connector.pull_catalog_daily(
            connection_id="conn-ttd",
            date_from=_DATE_FROM,
            date_to=_DATE_TO,
            project_id="p",
            pull_id="pull_ttd_def",
            selection=None,
            account_id=_ACCOUNT,
            **async_hooks,
        )
    assert result["report_template"] == "daily_performance"
    cfg = json.loads(schedule_route.calls.last.request.content)["Configuration"]
    # Every submitted column is legal on daily_performance's selectable set.
    allowed = set(
        json.loads(
            (connector._MODULE_DIR / "catalog_sources" / "template_columns.json")
            .read_text(encoding="utf-8")
        )["daily_performance"]
    )
    assert set(cfg["Dimensions"]) | set(cfg["Metrics"]) <= allowed
