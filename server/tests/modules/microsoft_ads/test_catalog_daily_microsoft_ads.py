"""Story 26.4 -- catalog_driven execution: pull_catalog_daily builds the
SubmitGenerateReport FROM the selection and refuses known incompatibilities
BEFORE any API call (26.1 core field_compat + module request invariants).

None of the refusal tests mount a respx mock: an HTTP attempt would raise a
transport error, not the typed InvalidRequestError, proving the refusal
happens BEFORE any token fetch / provider round-trip.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import httpx
import pytest
import respx
from core.async_reports import InMemoryReportRefStore
from core.pull_errors import InvalidRequestError

from .conftest import (
    ACCOUNT_ID,
    COMPOSITE_ACCOUNT,
    DOWNLOAD_URL,
    POLL_URL,
    SUBMIT_URL,
    make_report_zip,
)


def _pull_catalog(connector, selection, report_type=None, **kwargs):
    return connector.pull_catalog_daily(
        connection_id="c",
        date_from="2026-07-01",
        date_to="2026-07-01",
        project_id="p",
        pull_id=kwargs.pop("pull_id", "pull_cat_ms"),
        selection=selection,
        account_id=COMPOSITE_ACCOUNT,
        report_type=report_type,
        report_store=kwargs.pop("report_store", InMemoryReportRefStore()),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Column Restrictions refused PRE-CALL (the Airbyte stream-split, declarative).
# ---------------------------------------------------------------------------


def test_impression_share_with_restricted_attribute_refused_pre_call(connector):
    with pytest.raises(InvalidRequestError) as exc:
        _pull_catalog(
            connector,
            {
                "metrics": ["impression_share_percent", "impressions"],
                "dimensions": ["time_period", "campaign_id", "bid_match_type"],
            },
            report_type="CampaignPerformanceReportRequest",
        )
    assert exc.value.error_class == "invalid_request"
    assert "impression-share-vs-restricted-attributes" in str(exc.value)
    # Evidence: the violating rule id survives in provider_payload.
    payload = exc.value.provider_payload
    assert any(
        v["rule_id"] == "impression-share-vs-restricted-attributes"
        for v in payload["violations"]
    )


def test_audience_share_with_customer_attribute_refused_pre_call(connector):
    with pytest.raises(InvalidRequestError) as exc:
        _pull_catalog(
            connector,
            {
                "metrics": ["relative_ctr", "clicks"],
                "dimensions": ["time_period", "campaign_id", "customer_name"],
            },
            report_type="CampaignPerformanceReportRequest",
        )
    assert "audience-share-vs-customer-attributes" in str(exc.value)


def test_min_one_statistic_invariant_dimensions_only_refused(connector):
    """>=1 non-impression-share statistic: a dims-only selection is refused."""
    with pytest.raises(InvalidRequestError) as exc:
        _pull_catalog(
            connector,
            {
                "metrics": [],
                "dimensions": ["time_period", "campaign_id", "campaign_name"],
            },
            report_type="CampaignPerformanceReportRequest",
        )
    assert "request-invariants-min-attr-min-stat" in str(exc.value)


def test_min_one_statistic_invariant_impression_share_only_refused(connector):
    """Impression-share columns alone do NOT satisfy the statistic minimum."""
    with pytest.raises(InvalidRequestError) as exc:
        _pull_catalog(
            connector,
            {
                "metrics": ["impression_share_percent", "click_share_percent"],
                "dimensions": ["time_period", "campaign_id"],
            },
            report_type="CampaignPerformanceReportRequest",
        )
    assert "request-invariants-min-attr-min-stat" in str(exc.value)
    assert "NOT an impression-share" in str(exc.value)


def test_out_of_enum_column_refused_pre_call(connector):
    """selectable_set: 'keyword' is not in the CampaignPerformance enum."""
    with pytest.raises(InvalidRequestError) as exc:
        _pull_catalog(
            connector,
            {
                "metrics": ["impressions"],
                "dimensions": ["time_period", "campaign_id", "keyword"],
            },
            report_type="CampaignPerformanceReportRequest",
        )
    assert "columns-campaign-performance" in str(exc.value)


def test_excluded_report_surface_field_refused_pre_call(connector):
    """No silent drops includes EXCLUDED exposure: selecting a next-tier
    report-surface field is a typed refusal carrying the exclusion reason."""
    with pytest.raises(InvalidRequestError) as exc:
        _pull_catalog(
            connector,
            {
                "metrics": ["impressions"],
                "dimensions": ["time_period", "report_surface_goals_and_funnels"],
            },
            report_type="CampaignPerformanceReportRequest",
        )
    assert "excluded" in str(exc.value)
    payload = exc.value.provider_payload
    assert any(
        v["rule_id"] == "exposure-excluded"
        and "report-type-next-tier" in v["message"]
        for v in payload["violations"]
    )


def test_unknown_report_type_refused(connector):
    with pytest.raises(ValueError) as exc:
        _pull_catalog(
            connector,
            {"metrics": ["impressions"], "dimensions": ["time_period"]},
            report_type="AppsPerformanceReportRequest",
        )
    assert "whitelisted" in str(exc.value)


def test_no_covering_report_type_is_a_typed_refusal(connector):
    """Fields that never co-exist in one enum (keyword x age_group) -> typed
    refusal listing per-type misses, never a silent drop."""
    with pytest.raises(InvalidRequestError) as exc:
        _pull_catalog(
            connector,
            {
                "metrics": ["impressions"],
                "dimensions": ["time_period", "keyword", "age_group"],
            },
        )
    assert "no covered report type" in str(exc.value)
    assert exc.value.provider_payload["missing_by_report_type"]


def test_ambiguous_grain_with_non_additive_stat_requires_explicit_type(connector):
    """F-2 (review 26.4): an account-scope selection carrying an
    impression-share statistic is covered by SEVERAL report types of
    different grains (campaign / ad group / account) -- silently picking one
    would reinterpret what the non-additive number MEANS. Typed refusal
    listing the candidates; no HTTP mock mounted = the refusal is pre-call."""
    with pytest.raises(InvalidRequestError) as exc:
        _pull_catalog(
            connector,
            {
                "metrics": ["impression_share_percent", "impressions"],
                "dimensions": ["time_period", "account_id"],
            },
        )
    assert "ambiguous-report-type-non-additive" in str(exc.value)
    candidates = exc.value.provider_payload["candidate_report_types"]
    assert "CampaignPerformanceReportRequest" in candidates
    assert "AccountPerformanceReportRequest" in candidates
    assert len(candidates) > 1
    # The offending non-additive statistic is named in the violation.
    assert any(
        v["rule_id"] == "ambiguous-report-type-non-additive"
        and "impression_share_percent" in v["fields"]
        for v in exc.value.provider_payload["violations"]
    )


@respx.mock
def test_same_ambiguous_selection_with_explicit_report_type_runs(
    connector, tmp_path, monkeypatch
):
    """F-2 counterpart: the SAME selection with an explicit report_type is
    unambiguous and executes (grain chosen BY the user, data_level stamped)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_amb.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    headers = ["TimePeriod", "AccountId", "ImpressionSharePercent", "Impressions"]
    row = ["2026-07-01", ACCOUNT_ID, "37.50%", "12040"]

    submit = respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"ReportRequestId": "rr_amb"})
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
        return_value=httpx.Response(200, content=make_report_zip(headers, [row]))
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = _pull_catalog(
            connector,
            {
                "metrics": ["impression_share_percent", "impressions"],
                "dimensions": ["time_period", "account_id"],
            },
            report_type="AccountPerformanceReportRequest",
            pull_id="pull_cat_amb",
        )

    assert result["status"] == "completed"
    body = json.loads(submit.calls.last.request.content)["ReportRequest"]
    assert body["Type"] == "AccountPerformanceReportRequest"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, value_num, data_level FROM raw_microsoft_ads_daily "
        "WHERE pull_id = 'pull_cat_amb' ORDER BY metric"
    ).fetchall()
    con.close()
    assert all(r[2] == "ACCOUNT" for r in rows)
    by_metric = {r[0]: r[1] for r in rows}
    # Non-additive share lands RAW at day grain (AD-4), '%' stripped.
    assert by_metric["impression_share_percent"] == pytest.approx(37.5)
    assert by_metric["impressions"] == 12040


# ---------------------------------------------------------------------------
# Request shape: the Submit body is BUILT FROM the selection.
# ---------------------------------------------------------------------------


@respx.mock
def test_catalog_pull_builds_submit_from_selection(connector, tmp_path, monkeypatch):
    """Selection {spend, clicks / time_period, campaign_id, device_type} ->
    Columns carry exactly those + the grain carriers; nothing else. The
    selected segment lands in segments_json."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_ms.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    headers = [
        "TimePeriod", "AccountId", "CampaignId", "CampaignName", "DeviceType",
        "Spend", "Clicks",
    ]
    row = ["2026-07-01", ACCOUNT_ID, "22334455", "Brand - Search - FR",
           "Smartphone", "118.40", "640"]

    submit = respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"ReportRequestId": "rr_cat"})
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
        return_value=httpx.Response(200, content=make_report_zip(headers, [row]))
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = _pull_catalog(
            connector,
            {
                "metrics": ["spend", "clicks"],
                "dimensions": ["time_period", "campaign_id", "device_type"],
            },
            pull_id="pull_cat_ms_1",
        )

    assert result["row_count"] == 2  # cost + clicks landed long
    body = json.loads(submit.calls.last.request.content)["ReportRequest"]
    assert body["Type"] == "CampaignPerformanceReportRequest"
    assert sorted(body["Columns"]) == sorted(
        ["TimePeriod", "CampaignId", "DeviceType", "Spend", "Clicks",
         "AccountId", "CampaignName"]
    )
    # Nothing beyond selection + carriers (no hardcoded field lists, AD-2).
    assert "Impressions" not in body["Columns"]
    assert "Conversions" not in body["Columns"]

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, value_num, segments_json, data_level "
        "FROM raw_microsoft_ads_daily WHERE pull_id = 'pull_cat_ms_1' "
        "ORDER BY metric"
    ).fetchall()
    con.close()
    by_metric = {r[0]: r for r in rows}
    assert by_metric["cost"][1] == pytest.approx(118.4)
    assert by_metric["clicks"][1] == 640
    # The selected non-key dimension landed in the canonical segments_json.
    assert json.loads(by_metric["cost"][2]) == {"device_type": "Smartphone"}
    assert all(r[3] == "CAMPAIGN" for r in rows)


@respx.mock
def test_selection_chooses_the_report_type(connector, tmp_path, monkeypatch):
    """age_group/gender only exist in the AgeGenderAudience enum: the
    selection infers that report type and stamps its data_level."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "cat_ag.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    headers = ["TimePeriod", "AccountId", "CampaignId", "AgeGroup", "Gender",
               "Impressions"]
    row = ["2026-07-01", ACCOUNT_ID, "22334455", "25-34", "Female", "5310"]

    submit = respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"ReportRequestId": "rr_ag"})
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
        return_value=httpx.Response(200, content=make_report_zip(headers, [row]))
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        _pull_catalog(
            connector,
            {
                "metrics": ["impressions"],
                "dimensions": ["time_period", "age_group", "gender"],
            },
            pull_id="pull_cat_ag",
        )

    body = json.loads(submit.calls.last.request.content)["ReportRequest"]
    assert body["Type"] == "AgeGenderAudienceReportRequest"

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT data_level, segments_json FROM raw_microsoft_ads_daily "
        "WHERE pull_id = 'pull_cat_ag'"
    ).fetchall()
    con.close()
    assert rows and all(r[0] == "AGE_GENDER" for r in rows)
    assert json.loads(rows[0][1]) == {"age_group": "25-34", "gender": "Female"}


@respx.mock
def test_none_selection_uses_pruned_tier_core_default(
    connector, tmp_path, monkeypatch
):
    """A None selection resolves the catalog tier-core default PRUNED to the
    campaign enum: 5 additive core metrics + structural dims, no ratios, no
    other-grain ids (review google-ads lesson)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "cat_def.duckdb"))

    headers = ["TimePeriod", "AccountId", "AccountName", "CustomerId",
               "CampaignId", "CampaignName", "CampaignType", "CurrencyCode",
               "Spend", "Impressions", "Clicks", "Conversions", "Revenue"]
    row = ["2026-07-01", ACCOUNT_ID, "Toorow FR", "250012345", "22334455",
           "Brand", "Search", "EUR", "1.0", "10", "1", "0", "0"]

    submit = respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"ReportRequestId": "rr_def"})
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
        return_value=httpx.Response(200, content=make_report_zip(headers, [row]))
    )

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_cat_def",
            selection=None,
            account_id=COMPOSITE_ACCOUNT,
            report_store=InMemoryReportRefStore(),
        )

    columns = set(
        json.loads(submit.calls.last.request.content)["ReportRequest"]["Columns"]
    )
    assert {"TimePeriod", "AccountId", "CampaignId", "CampaignName",
            "CampaignType", "CurrencyCode", "Spend", "Impressions", "Clicks",
            "Conversions", "Revenue"} <= columns
    # Pruned: other-grain ids and ratio/non-additive columns never sneak in.
    for forbidden in ("AdGroupId", "AdId", "Ctr", "AverageCpc",
                      "AveragePosition", "ImpressionSharePercent"):
        assert forbidden not in columns, forbidden
