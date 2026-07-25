"""Story 26.6 -- grain of supersede vs descriptive-mutable attributes, and the
core-resolved tier-core default recognition.

Part A: entity STATUS/TYPE columns (campaign_status, campaign_type, ...) are
MUTABLE across the refetch ladder. They must land in ``attributes_json``
(latest-wins, OUT of the dbt supersede grain), NEVER in ``segments_json``
(which is part of the QUALIFY partition). Proof: re-pull the SAME window with
CampaignStatus changed Active->Paused -> a single row survives per grain and
cost/impressions are NOT doubled (finding F-1), attributes = last known.

Part B: core/queue.py resolves ``selection=None`` to the tier-core default and
passes it as an EXPLICIT selection. The module recognises it (amazon-ads
pattern) and applies the SAME prune it applies to ``selection=None``; a genuine
user selection is never pruned.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import duckdb
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
    make_report_zip,
)

# A campaign-grain report carrying the mutable CampaignStatus + CampaignType
# attributes alongside a segmenting DeviceType dimension and additive metrics.
_HEADERS = [
    "TimePeriod", "AccountId", "CampaignId", "CampaignName", "CampaignType",
    "CampaignStatus", "DeviceType", "CurrencyCode", "Spend", "Impressions",
    "Clicks",
]


def _row(status: str) -> list[str]:
    return [
        "2026-07-01", ACCOUNT_ID, "22334455", "Brand - Search - FR", "Search",
        status, "Smartphone", "EUR", "118.40", "12040", "640",
    ]


def _mock_flow(status: str, report_ref: str) -> None:
    respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"ReportRequestId": report_ref})
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
            200, content=make_report_zip(_HEADERS, [_row(status)])
        )
    )


def _pull(connector, pull_id: str, status: str, report_ref: str, db_path: str):
    with respx.mock:
        _mock_flow(status, report_ref)
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            return connector.pull_catalog_daily(
                connection_id="c",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="p",
                pull_id=pull_id,
                selection={
                    "metrics": ["spend", "impressions", "clicks"],
                    "dimensions": [
                        "time_period", "campaign_id", "campaign_type",
                        "campaign_status", "device_type",
                    ],
                },
                report_type="CampaignPerformanceReportRequest",
                account_id=COMPOSITE_ACCOUNT,
                report_store=InMemoryReportRefStore(),
            )


# ---------------------------------------------------------------------------
# Part A -- landing split: status/type -> attributes_json, NOT segments_json.
# ---------------------------------------------------------------------------


def test_status_and_type_land_in_attributes_json_not_segments(
    connector, tmp_path, monkeypatch
):
    """The mutable CampaignStatus/CampaignType land in attributes_json; the
    segmenting DeviceType stays in segments_json (part of the grain)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "grain.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    _pull(connector, "pull_a1", "Active", "rr_a1", db_path)

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT metric, segments_json, attributes_json "
        "FROM raw_microsoft_ads_daily WHERE pull_id = 'pull_a1' ORDER BY metric"
    ).fetchall()
    con.close()

    assert rows, "expected landed rows"
    for _metric, segments, attributes in rows:
        seg = json.loads(segments)
        attr = json.loads(attributes)
        # Segmenting dimension stays in the grain.
        assert seg == {"device_type": "Smartphone"}
        # Mutable status/type are OUT of the grain, in attributes_json.
        assert attr == {"campaign_status": "Active", "campaign_type": "Search"}
        # And absolutely NOT in segments_json (would fork the supersede grain).
        assert "campaign_status" not in seg
        assert "campaign_type" not in seg


def test_repull_with_status_change_does_not_double_count(
    connector, tmp_path, monkeypatch
):
    """F-1: re-pull the SAME window with CampaignStatus Active->Paused. Only the
    latest row per grain survives the dbt QUALIFY (segments_json unchanged since
    status is NOT in it) -> cost/impressions NOT doubled; attributes = Paused."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "repull.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    # First pull (Active) then a refetch-ladder re-pull of the SAME day (Paused).
    _pull(connector, "pull_r1", "Active", "rr_r1", db_path)
    _pull(connector, "pull_r2", "Paused", "rr_r2", db_path)

    con = duckdb.connect(db_path, read_only=True)

    # Raw holds BOTH pulls (append-only, AD-7).
    raw_pulls = con.execute(
        "SELECT DISTINCT pull_id FROM raw_microsoft_ads_daily ORDER BY pull_id"
    ).fetchall()
    assert {r[0] for r in raw_pulls} == {"pull_r1", "pull_r2"}

    # segments_json is IDENTICAL across the two pulls (status is not in it), so
    # the two pulls share the supersede grain -- the QUALIFY keeps exactly one.
    distinct_segments = con.execute(
        "SELECT DISTINCT segments_json FROM raw_microsoft_ads_daily "
        "WHERE metric = 'cost'"
    ).fetchall()
    assert len(distinct_segments) == 1

    # Reproduce the staging QUALIFY supersede (latest pull_id per grain wins).
    superseded = con.execute(
        """
        SELECT metric, value_num, attributes_json
        FROM (
            SELECT metric, value_num, attributes_json, pull_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY project_id, date, data_level, account_id,
                           campaign_id, ad_group_id, ad_id, keyword_id,
                           search_query, COALESCE(segments_json, ''), metric
                       ORDER BY pull_id DESC
                   ) AS rn
            FROM raw_microsoft_ads_daily
        )
        WHERE rn = 1
        ORDER BY metric
        """
    ).fetchall()
    con.close()

    by_metric = {r[0]: r for r in superseded}
    # ONE surviving row per metric -- NOT two (no double-count).
    assert len(superseded) == 3
    assert by_metric["cost"][1] == pytest.approx(118.4)  # not 236.8
    assert by_metric["impressions"][1] == 12040  # not 24080
    assert by_metric["clicks"][1] == 640
    # Latest-wins: the surviving attributes reflect the LAST re-pull (Paused).
    for _metric, _value, attributes in superseded:
        assert json.loads(attributes)["campaign_status"] == "Paused"


def test_descriptive_mutable_field_ids_generated_into_catalog_sources(connector):
    """The generator emitted the status/type field ids into the module's own
    field_compatibility block; the connector reads them (schema allows ^_ keys).
    campaign_status/type + the other statuses are present; grain ids/names and
    segmenting dimensions are NOT."""
    ids = connector._descriptive_mutable_field_ids()
    assert {
        "campaign_status", "ad_group_status", "ad_status", "keyword_status",
        "campaign_type",
    } <= ids
    # Grain keys and segmenting dimensions must never be descriptive_mutable.
    for forbidden in (
        "campaign_id", "campaign_name", "age_group", "gender", "country",
        "device_type", "time_period",
    ):
        assert forbidden not in ids


# ---------------------------------------------------------------------------
# Part B -- the core-resolved tier-core default is recognised and pruned.
# ---------------------------------------------------------------------------


def _default_columns(connector, submit) -> set[str]:
    body = json.loads(submit.calls.last.request.content)["ReportRequest"]
    return set(body["Columns"])


@respx.mock
def test_core_resolved_default_selection_is_pruned_like_none(
    connector, tmp_path, monkeypatch
):
    """Part B: the queue resolves selection=None to the tier-core default and
    passes it as an EXPLICIT selection. Passing that resolved default must
    Submit VALIDLY (same prune as None) -- never a pre-call refusal."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "b1.duckdb"))

    resolved_default = connector._resolved_core_default_selection()
    # Sanity: the unpruned core default spans the whole catalog (other-grain
    # ids / ratios) -- exactly what would trip the pre-call refusals if treated
    # as a user selection.
    assert len(resolved_default["dimensions"]) + len(resolved_default["metrics"]) > 11

    headers = ["TimePeriod", "AccountId", "CampaignId", "CampaignName",
               "CampaignType", "CurrencyCode", "Spend", "Impressions", "Clicks",
               "Conversions", "Revenue"]
    row = ["2026-07-01", ACCOUNT_ID, "22334455", "Brand", "Search", "EUR",
           "1.0", "10", "1", "0", "0"]

    submit = respx.post(SUBMIT_URL).mock(
        return_value=httpx.Response(200, json={"ReportRequestId": "rr_b1"})
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
        result = connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_b1",
            selection=resolved_default,
            account_id=COMPOSITE_ACCOUNT,
            report_store=InMemoryReportRefStore(),
        )

    assert result["status"] == "completed"
    columns = _default_columns(connector, submit)
    # Pruned to the campaign enum + additive numeric metrics (same as None).
    assert {"TimePeriod", "AccountId", "CampaignId", "Spend", "Impressions",
            "Clicks"} <= columns
    for forbidden in ("AdGroupId", "AdId", "Ctr", "AverageCpc",
                      "ImpressionSharePercent"):
        assert forbidden not in columns, forbidden


def test_is_resolved_core_default_recognises_only_the_exact_default(connector):
    """The recognition is order-independent on the SETS, and a genuine user
    selection with any different membership is NOT the prunable default."""
    default = connector._resolved_core_default_selection()
    # Same sets, reordered -> recognised.
    shuffled = {
        "metrics": list(reversed(default.get("metrics", []))),
        "dimensions": list(reversed(default.get("dimensions", []))),
    }
    assert connector._is_resolved_core_default(shuffled) is True
    # A real user selection (subset) is NOT the default -> stays unpruned.
    assert connector._is_resolved_core_default(
        {"metrics": ["impressions"], "dimensions": ["time_period", "campaign_id"]}
    ) is False


def test_user_selection_is_never_pruned_typed_refusal_survives(connector):
    """Part B guard: a genuine user selection that trips a Column Restriction is
    a typed refusal -- it is never silently pruned into validity."""
    from core.pull_errors import InvalidRequestError

    with pytest.raises(InvalidRequestError):
        connector.pull_catalog_daily(
            connection_id="c",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="p",
            pull_id="pull_b_user",
            selection={
                "metrics": ["impression_share_percent", "impressions"],
                "dimensions": ["time_period", "campaign_id", "bid_match_type"],
            },
            report_type="CampaignPerformanceReportRequest",
            account_id=COMPOSITE_ACCOUNT,
            report_store=InMemoryReportRefStore(),
        )
