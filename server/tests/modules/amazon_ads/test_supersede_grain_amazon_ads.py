"""Story 26.6 Part A -- descriptive MUTABLE attributes out of the supersede grain.

Finding F-2 (review 26.5): entity STATE/TYPE dimensions (campaignStatus, ...)
are mutable attributes of a grain entity, not row-segmenting breakdowns. If they
ride inside segments_json (the dbt QUALIFY supersede key) a status flip
(ENABLED -> PAUSED) on a re-pulled day mints a SECOND grain key and the stale
row SURVIVES -> SUM(cost) doubles for the whole refetched window (the 26.1
refetch ladder makes this systematic).

These tests prove, END TO END on a real DuckDB, that:
  1. campaignStatus lands in attributes_json (latest-wins) NOT segments_json;
  2. a re-pull of the SAME window with a FLIPPED status supersedes to ONE row
     per (grain, metric) -- cost/impressions are NOT doubled and the surviving
     row carries the LATEST attributes;
  3. a genuine segmenting dimension (matchType) still rides in segments_json and
     DOES split the grain (control -- proves we did not over-move columns).

No network: the async socle is driven with the 26.1 fakes (fresh InMemory store
per pull so submit() actually fires each time), respx mocks the 3 provider hops.
"""

from __future__ import annotations

import gzip
import json
from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
import respx

from .conftest import HOSTS

_NA = HOSTS["NA"]
_REPORTS_URL = f"{_NA}/reporting/reports"
_S3_URL = "https://offline-report-storage.s3.amazonaws.com/report.json.gz"

# One fixed day so a re-pull targets the SAME grain key (the F-2 scenario).
_DAY = (date.today() - timedelta(days=5)).isoformat()


def _gzip_body(rows) -> bytes:
    return gzip.compress(json.dumps(rows).encode("utf-8"))


def _created(report_id: str) -> httpx.Response:
    return httpx.Response(200, json={"reportId": report_id, "status": "PENDING"})


def _completed(report_id: str) -> httpx.Response:
    return httpx.Response(
        200, json={"reportId": report_id, "status": "COMPLETED", "url": _S3_URL}
    )


def _campaign_row(status: str, cost: float = 118.4) -> dict:
    return {
        "date": _DAY,
        "campaignId": 111222333,
        "campaignName": "SP - Brand - FR",
        "campaignStatus": status,
        "campaignBudgetCurrencyCode": "EUR",
        "impressions": 12040,
        "clicks": 640,
        "cost": cost,
        "purchases14d": 42,
        "purchases30d": 45,
        "sales14d": 3120.75,
        "sales30d": 3300.5,
    }


def _fresh_hooks(fake_clock_cls):
    """A pull driven by an INDEPENDENT in-memory ref store (submit fires)."""
    from core.async_reports import InMemoryReportRefStore

    clock = fake_clock_cls()
    return {
        "_store": InMemoryReportRefStore(clock=clock),
        "_clock": clock,
        "_sleeper": clock.sleep,
        "_deadline_seconds": 100000,
    }


def _run_pull(connector, hooks, pull_id: str, report_id: str):
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        return connector.pull_sp_campaigns_daily(
            connection_id="conn-az",
            date_from=_DAY,
            date_to=_DAY,
            project_id="p",
            pull_id=pull_id,
            account_id="NA:1234",
            **hooks,
        )


def _mock_one_report(report_id: str, rows: list[dict]) -> None:
    respx.post(_REPORTS_URL).mock(return_value=_created(report_id))
    respx.get(f"{_REPORTS_URL}/{report_id}").mock(return_value=_completed(report_id))
    respx.get(_S3_URL).mock(
        return_value=httpx.Response(200, content=_gzip_body(rows))
    )


def _stg_sql() -> str:
    """The stg model SQL, rewritten to a plain query over the raw table.

    Mirrors stg_amazon_ads_daily.sql EXACTLY -- same PARTITION BY (segments_json
    IN, attributes_json OUT) -- so the supersede behavior is what dbt produces.
    """
    return """
    SELECT date, campaign_id, segments_json, attributes_json, metric, value_num,
           pull_id
    FROM raw_amazon_ads_daily
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, data_level, ad_product, region, profile_id,
                     campaign_id, ad_group_id, ad_id, keyword_id, search_term,
                     advertised_asin, purchased_asin,
                     COALESCE(segments_json, ''), metric
        ORDER BY pull_id DESC
    ) = 1
    """


def _setup_duckdb(monkeypatch, tmp_path):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "az_supersede.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    return db_path


@respx.mock
def test_status_lands_in_attributes_not_segments(
    connector, tmp_path, monkeypatch
):
    """campaignStatus -> attributes_json (latest-wins); NOT in segments_json."""
    import duckdb

    from .conftest import FakeClock

    db_path = _setup_duckdb(monkeypatch, tmp_path)
    _mock_one_report("rA", [_campaign_row("ENABLED")])
    _run_pull(connector, _fresh_hooks(FakeClock), "pull_a1", "rA")

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT segments_json, attributes_json FROM raw_amazon_ads_daily "
        "WHERE pull_id = 'pull_a1' LIMIT 1"
    ).fetchall()
    con.close()

    segments_json, attributes_json = rows[0]
    attrs = json.loads(attributes_json)
    # campaign_status is the post-transform canonical name; it must be an
    # attribute, latest-wins, and NEVER a grain segment.
    assert attrs.get("campaign_status") == "ENABLED"
    if segments_json is not None:
        assert "campaign_status" not in json.loads(segments_json)
        assert "campaignStatus" not in json.loads(segments_json)


@respx.mock
def test_repull_status_flip_supersedes_no_double_count(
    connector, tmp_path, monkeypatch
):
    """Re-pull same window with ENABLED->PAUSED: ONE row per (grain, metric).

    The stale ENABLED row must NOT survive the QUALIFY. cost/impressions stay
    single (F-2 double-count would make cost = 236.8) and the surviving row
    carries the LATEST attributes (PAUSED).
    """
    import duckdb

    from .conftest import FakeClock

    db_path = _setup_duckdb(monkeypatch, tmp_path)

    # First pull: ENABLED. Second pull (later pull_id, same window, fresh store
    # so submit fires): status flipped to PAUSED, identical metrics.
    _mock_one_report("rEnabled", [_campaign_row("ENABLED")])
    _run_pull(connector, _fresh_hooks(FakeClock), "pull_0001_enabled", "rEnabled")

    respx.reset()
    _mock_one_report("rPaused", [_campaign_row("PAUSED")])
    _run_pull(connector, _fresh_hooks(FakeClock), "pull_0002_paused", "rPaused")

    con = duckdb.connect(db_path, read_only=True)
    # Sanity: BOTH pulls landed raw rows (the re-pull really happened).
    raw_cost = con.execute(
        "SELECT COUNT(*) FROM raw_amazon_ads_daily WHERE metric = 'cost'"
    ).fetchone()[0]
    assert raw_cost == 2, "expected the raw table to hold both pulls' cost rows"

    staged = con.execute(_stg_sql()).fetchall()
    con.close()

    # ONE surviving row per metric (7 numeric bundle metrics), never doubled.
    by_metric: dict[str, list] = {}
    for date_, campaign_id, seg, attrs, metric, value, pull_id in staged:
        by_metric.setdefault(metric, []).append((value, attrs, pull_id))

    assert len(by_metric["cost"]) == 1, "status flip split the grain (F-2 regression)"
    assert len(by_metric["impressions"]) == 1

    cost_value, cost_attrs, cost_pull = by_metric["cost"][0]
    assert cost_value == pytest.approx(118.4), "cost was DOUBLED across the re-pull"
    assert by_metric["impressions"][0][0] == 12040
    # Latest pull wins (ULID-monotonic pull_id) and carries the LATEST attribute.
    assert cost_pull == "pull_0002_paused"
    assert json.loads(cost_attrs)["campaign_status"] == "PAUSED"


@respx.mock
def test_segmenting_dimension_still_splits_grain(
    connector, tmp_path, monkeypatch
):
    """Control: a genuine segment (matchType) stays in segments_json and DOES
    produce distinct grain rows -- proves Part A did not over-move columns.
    """
    import duckdb

    from .conftest import FakeClock

    db_path = _setup_duckdb(monkeypatch, tmp_path)

    # spTargeting supports the matchType segment; two rows differing ONLY by
    # matchType must remain TWO distinct grain rows for the same metric.
    row_broad = {
        "date": _DAY,
        "campaignId": 111222333,
        "campaignName": "SP - Brand - FR",
        "campaignStatus": "ENABLED",
        "matchType": "BROAD",
        "cost": 10.0,
        "impressions": 100,
        "clicks": 5,
    }
    row_exact = dict(row_broad, matchType="EXACT", cost=20.0, impressions=200)

    respx.post(_REPORTS_URL).mock(return_value=_created("rSeg"))
    respx.get(f"{_REPORTS_URL}/rSeg").mock(return_value=_completed("rSeg"))
    respx.get(_S3_URL).mock(
        return_value=httpx.Response(200, content=_gzip_body([row_broad, row_exact]))
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="conn-az",
            date_from=_DAY,
            date_to=_DAY,
            project_id="p",
            pull_id="pull_seg",
            selection={
                "metrics": ["cost", "impressions", "clicks"],
                "dimensions": [
                    "campaignId", "campaignName", "campaignStatus", "matchType",
                ],
                "source_fields": {},
            },
            report_type="spTargeting",
            account_id="NA:1234",
            **_fresh_hooks(FakeClock),
        )

    con = duckdb.connect(db_path, read_only=True)
    seg_values = con.execute(
        "SELECT segments_json FROM raw_amazon_ads_daily "
        "WHERE metric = 'cost' AND pull_id = 'pull_seg'"
    ).fetchall()
    con.close()

    match_types = sorted(
        json.loads(s[0]).get("match_type") or json.loads(s[0]).get("matchType")
        for s in seg_values
    )
    # Two distinct grain rows, one per matchType (segment stayed in the grain).
    assert match_types == ["BROAD", "EXACT"]


@respx.mock
def test_attribution_type_splits_grain_not_attribute(
    connector, tmp_path, monkeypatch
):
    """Review 26.6 F-1 regression (CRITICAL, silent data loss).

    attributionType is an ATTRIBUTION BUCKET that carries ROWS on
    sbPurchasedProduct: it co-occurs with purchasedAsin + sales14d/orders14d, and
    PROMOTED vs BRAND_HALO are DISTINCT provider rows for the SAME
    purchasedAsin/date. It MUST ride in segments_json (grain), NOT attributes_json
    -- otherwise the two rows collapse at the QUALIFY and sales14d/orders14d are
    UNDER-COUNTED. Mirrors test_segmenting_dimension_still_splits_grain (which
    only exercises matchType).
    """
    import duckdb

    from .conftest import FakeClock

    db_path = _setup_duckdb(monkeypatch, tmp_path)

    # Same purchasedAsin/date, two attribution buckets -> two provider rows.
    row_promoted = {
        "date": _DAY,
        "campaignId": 111222333,
        "campaignName": "SB - Brand - FR",
        "purchasedAsin": "B0PROMOTED1",
        "attributionType": "PROMOTED",
        "sales14d": 500.0,
        "orders14d": 12,
        "unitsSold14d": 15,
    }
    row_halo = dict(
        row_promoted, attributionType="BRAND_HALO", sales14d=80.0,
        orders14d=3, unitsSold14d=4,
    )

    respx.post(_REPORTS_URL).mock(return_value=_created("rAttr"))
    respx.get(f"{_REPORTS_URL}/rAttr").mock(return_value=_completed("rAttr"))
    respx.get(_S3_URL).mock(
        return_value=httpx.Response(
            200, content=_gzip_body([row_promoted, row_halo])
        )
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="conn-az",
            date_from=_DAY,
            date_to=_DAY,
            project_id="p",
            pull_id="pull_attr",
            selection={
                "metrics": ["sales14d", "orders14d", "unitsSold14d"],
                "dimensions": [
                    "campaignId", "campaignName", "purchasedAsin",
                    "attributionType",
                ],
                "source_fields": {},
            },
            report_type="sbPurchasedProduct",
            account_id="NA:1234",
            **_fresh_hooks(FakeClock),
        )

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT segments_json, attributes_json, value_num "
        "FROM raw_amazon_ads_daily "
        "WHERE metric = 'sales_14d' AND pull_id = 'pull_attr'"
    ).fetchall()
    con.close()

    # attributionType lands in segments_json (grain), NEVER attributes_json.
    for segments_json, attributes_json, _value in rows:
        seg = json.loads(segments_json) if segments_json else {}
        assert "attributionType" in seg, (
            "attributionType must ride in segments_json (grain), F-1 regression"
        )
        if attributes_json is not None:
            attrs = json.loads(attributes_json)
            assert "attributionType" not in attrs
            assert "attribution_type" not in attrs

    # Two distinct grain rows, one per attribution bucket (grain SPLIT, not
    # collapsed) -- so sales14d is NOT under-counted.
    buckets = sorted(json.loads(s).get("attributionType") for s, _a, _v in rows)
    assert buckets == ["BRAND_HALO", "PROMOTED"]
    sales_by_bucket = {
        json.loads(s)["attributionType"]: v for s, _a, v in rows
    }
    assert sales_by_bucket["PROMOTED"] == pytest.approx(500.0)
    assert sales_by_bucket["BRAND_HALO"] == pytest.approx(80.0)
