"""Story 26.5 -- createasyncreportrequest v3 request shape + pre-call refusals.

Everything here is PURE (no HTTP): the body builder validates dates
(fromisoformat + per-product retention floors, SB = 60 days), columns
(dictionary charset + core field_compat selectable_set with the
{report_type, group_by} context), and whitelists the reportTypeId -- a
selected column either lands or is refused TYPED before any provider
round-trip (never silently dropped).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from core.pull_errors import InvalidRequestError

_YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


# ---------------------------------------------------------------------------
# Body construction.
# ---------------------------------------------------------------------------


def test_body_shape_sp_campaigns(connector):
    body = connector._build_report_body(
        "spCampaigns", ["campaign"],
        ["date", "campaignId", "campaignName", "impressions", "clicks", "cost"],
        _iso(7), _iso(1),
    )
    cfg = body["configuration"]
    assert cfg["adProduct"] == "SPONSORED_PRODUCTS"
    assert cfg["reportTypeId"] == "spCampaigns"
    assert cfg["groupBy"] == ["campaign"]
    assert cfg["timeUnit"] == "DAILY"
    assert cfg["format"] == "GZIP_JSON"
    assert body["startDate"] == _iso(7)
    assert body["endDate"] == _iso(1)
    assert cfg["columns"][0] == "date" or "date" in cfg["columns"]


def test_daily_time_unit_forces_date_column(connector):
    body = connector._build_report_body(
        "spCampaigns", ["campaign"], ["campaignId", "impressions"], _iso(3), _iso(1)
    )
    assert "date" in body["configuration"]["columns"]


# ---------------------------------------------------------------------------
# Date validation (typed refusals BEFORE any API call).
# ---------------------------------------------------------------------------


def test_non_iso_dates_refused(connector):
    with pytest.raises(InvalidRequestError, match="ISO-8601"):
        connector._build_report_body(
            "spCampaigns", ["campaign"], ["impressions"], "07/01/2026", _YESTERDAY
        )


def test_inverted_window_refused(connector):
    with pytest.raises(InvalidRequestError, match="after"):
        connector._build_report_body(
            "spCampaigns", ["campaign"], ["impressions"], _iso(1), _iso(5)
        )


def test_window_over_31_days_refused(connector):
    with pytest.raises(InvalidRequestError, match="31"):
        connector._build_report_body(
            "spCampaigns", ["campaign"], ["impressions"], _iso(40), _iso(1)
        )


def test_sb_retention_floor_60_days_refused_pre_call(connector):
    """SB retention floor: 'Report date is too far in the past' never
    round-trips -- refused typed here (dossier 1.7 / story AC)."""
    with pytest.raises(InvalidRequestError, match="retention floor is 60 days"):
        connector._build_report_body(
            "sbCampaigns", ["campaign"], ["impressions", "clicks"],
            _iso(80), _iso(60),
        )


def test_sp_deeper_window_allowed_within_95_days(connector):
    body = connector._build_report_body(
        "spCampaigns", ["campaign"], ["impressions"], _iso(90), _iso(80)
    )
    assert body["configuration"]["reportTypeId"] == "spCampaigns"


# ---------------------------------------------------------------------------
# Report-type whitelist + groupBy legality.
# ---------------------------------------------------------------------------


def test_dsp_report_type_refused(connector):
    with pytest.raises(InvalidRequestError, match="dsp"):
        connector._build_report_body(
            "dspCampaign", ["campaign"], ["impressions"], _iso(3), _iso(1)
        )


def test_conversion_path_report_type_refused(connector):
    with pytest.raises(InvalidRequestError):
        connector._build_report_body(
            "conversionPath", ["brandConversionPath"], ["sales"], _iso(3), _iso(1)
        )


def test_illegal_group_by_refused(connector):
    with pytest.raises(InvalidRequestError, match="groupBy"):
        connector._build_report_body(
            "spTargeting", ["campaign"], ["impressions"], _iso(3), _iso(1)
        )


# ---------------------------------------------------------------------------
# Column validation (charset + report-type membership + groupBy narrowing).
# ---------------------------------------------------------------------------


def test_column_charset_guard(connector):
    with pytest.raises(InvalidRequestError, match="charset"):
        connector._build_report_body(
            "spCampaigns", ["campaign"], ["impressions", "actions[purchase]"],
            _iso(3), _iso(1),
        )


def test_column_outside_report_type_refused_with_rule_id(connector):
    """unitsSold is an SB/SD column -- selecting it on spCampaigns is a typed
    pre-call refusal carrying the selectable_set rule id (26.1 contract)."""
    with pytest.raises(InvalidRequestError) as exc:
        connector._build_report_body(
            "spCampaigns", ["campaign"], ["impressions", "unitsSold"],
            _iso(3), _iso(1),
        )
    assert "selectable_spCampaigns" in str(exc.value)
    assert "unitsSold" in str(exc.value)


def test_group_by_narrowing_spcampaigns_campaign_refuses_adgroup_columns(connector):
    """Page rule: adGroupName is only unlocked by groupBy adGroup."""
    with pytest.raises(InvalidRequestError) as exc:
        connector._build_report_body(
            "spCampaigns", ["campaign"], ["impressions", "adGroupName"],
            _iso(3), _iso(1),
        )
    assert "selectable_spCampaigns_campaign" in str(exc.value)


def test_group_by_adgroup_allows_adgroup_columns(connector):
    body = connector._build_report_body(
        "spCampaigns", ["adGroup"], ["impressions", "adGroupName", "adGroupId"],
        _iso(3), _iso(1),
    )
    assert "adGroupName" in body["configuration"]["columns"]


# ---------------------------------------------------------------------------
# catalog_daily selection resolution (selection -> report type + groupBy).
# ---------------------------------------------------------------------------


def test_selection_resolves_to_search_term_report(connector):
    report_type, group_by = connector._resolve_report_type_for_selection(
        ["date", "searchTerm", "keywordId", "impressions", "clicks"], None
    )
    assert report_type == "spSearchTerm"
    assert group_by == ["searchTerm"]


def test_selection_resolves_default_to_sp_campaigns(connector):
    report_type, _gb = connector._resolve_report_type_for_selection(
        ["date", "campaignId", "impressions", "clicks", "cost"], None
    )
    assert report_type == "spCampaigns"


def test_unresolvable_selection_refused_with_missing_columns(connector):
    """No sponsored report type carries searchTerm AND purchasedAsin together."""
    with pytest.raises(InvalidRequestError, match="no single sponsored report type"):
        connector._resolve_report_type_for_selection(
            ["searchTerm", "purchasedAsin", "impressions"], None
        )


def test_excluded_dsp_column_selection_refused_with_reason(connector):
    """NO SILENT DROP: a dsp-seat-gated column in the selection is a typed
    refusal carrying the catalog exclusion reason."""
    with pytest.raises(InvalidRequestError, match="dsp-seat-gated"):
        connector._refuse_excluded_columns(["impressions", "3pFeeAutomotive"])


def test_excluded_probe_column_selection_refused(connector):
    with pytest.raises(InvalidRequestError, match="probe-to-confirm"):
        connector._refuse_excluded_columns(["linkOuts"])


def test_pull_catalog_daily_refuses_excluded_selection_before_any_call(connector):
    """End to end: the refusal fires BEFORE token fetch / HTTP (no mocks needed)."""
    with pytest.raises(InvalidRequestError, match="dsp-seat-gated"):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from=_iso(3),
            date_to=_iso(1),
            project_id="p",
            pull_id="pull_az_excl",
            selection={"metrics": ["3pFeeAutomotive"], "dimensions": ["date"],
                       "source_fields": {}},
            account_id="NA:1234",
        )


# ---------------------------------------------------------------------------
# Deterministic request hash (the socle dedup key).
# ---------------------------------------------------------------------------


def test_request_hash_ignores_name_and_is_deterministic(connector):
    body_a = connector._build_report_body(
        "spCampaigns", ["campaign"], ["impressions", "clicks"], _iso(3), _iso(1)
    )
    body_b = connector._build_report_body(
        "spCampaigns", ["campaign"], ["impressions", "clicks"], _iso(3), _iso(1)
    )
    body_b["name"] = "another run name"
    assert connector._request_hash("NA", "123", body_a) == connector._request_hash(
        "NA", "123", body_b
    )


def test_request_hash_changes_with_shape(connector):
    body_a = connector._build_report_body(
        "spCampaigns", ["campaign"], ["impressions"], _iso(3), _iso(1)
    )
    body_b = connector._build_report_body(
        "spCampaigns", ["campaign"], ["impressions", "clicks"], _iso(3), _iso(1)
    )
    assert connector._request_hash("NA", "123", body_a) != connector._request_hash(
        "NA", "123", body_b
    )
    assert connector._request_hash("NA", "123", body_a) != connector._request_hash(
        "EU", "123", body_a
    )
