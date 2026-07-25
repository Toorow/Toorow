"""Tests for Google Business Profile connector (Story 30.1)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULES_DIR = REPO_ROOT / "server" / "modules"
if str(MODULES_DIR) not in sys.path:
    sys.path.insert(0, str(MODULES_DIR))

from google_business_profile import connector


def test_gbp_transform_canonical_field_mapping():
    """Verify transform() maps DailyMetric names to canonical names."""
    raw_rows = [
        {
            "date": "2026-07-01",
            "location_id": "loc_100",
            "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": 120,
            "CALL_CLICKS": 15,
            "starRating": "FIVE",
        }
    ]

    transformed = connector.transform(raw_rows)
    assert len(transformed) == 1
    row = transformed[0]

    assert row["date"] == "2026-07-01"
    assert row["location_id"] == "loc_100"
    assert row["impressions_desktop_maps"] == 120
    assert row["call_clicks"] == 15
    assert row["star_rating"] == "FIVE"


def test_gbp_insert_raw_location_daily_rows(tmp_path, monkeypatch):
    """Verify _insert_raw_location_daily_rows writes rows to DuckDB."""
    db_path = str(tmp_path / "test_gbp.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    rows = [
        {
            "metric_date": "2026-07-01",
            "location_id": "loc_100",
            "account_id": "acc_200",
            "metric_name": "CALL_CLICKS",
            "value": 15,
        }
    ]

    count = connector._insert_raw_location_daily_rows(rows, "pull_001", "default")
    assert count == 1

    import duckdb

    con = duckdb.connect(db_path)
    res = con.execute("SELECT metric_date, location_id, metric_name, value FROM raw_gbp_location_daily").fetchall()
    con.close()

    assert len(res) == 1
    assert res[0] == ("2026-07-01", "loc_100", "CALL_CLICKS", 15)


def test_gbp_pull_location_daily_gap_fill_and_403_access_pending(tmp_path, monkeypatch):
    """Verify pull_location_daily gap-fills absent days and handles 403 0-QPM gracefully."""
    db_path = str(tmp_path / "test_gbp_pull.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    def mock_get(url, headers=None, params=None, timeout=None):
        if "loc_403" in url:
            # 0-QPM pending access
            return httpx.Response(403, json={"error": {"code": 403, "message": "Quota 0 QPM pending"}})
        # Reachable location
        return httpx.Response(
            200,
            json={
                "multiDailyMetricTimeSeries": [
                    {
                        "dailyMetricTimeSeries": [
                            {
                                "dailyMetric": "CALL_CLICKS",
                                "timeSeries": {
                                    "datedValues": [
                                        {
                                            "date": {"year": 2026, "month": 7, "day": 1},
                                            "value": "25",
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ]
            },
        )

    with patch("core.nango_client.get_fresh_token", return_value="fake_token"), patch(
        "httpx.Client.get", side_effect=mock_get
    ):
        res = connector.pull_location_daily(
            connection_id="conn_gbp",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="default",
            pull_id="pull_gbp_001",
            location_ids=["loc_100", "loc_403"],
        )

    assert res["pull_id"] == "pull_gbp_001"
    # 11 metrics x 2 days for loc_100 = 22 rows landed
    assert res["row_count"] == 22


def test_gbp_pull_reviews_403_non_fatal(tmp_path, monkeypatch):
    """Verify pull_reviews handles 403 allowlist pending gracefully."""
    db_path = str(tmp_path / "test_gbp_reviews.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    def mock_get(url, headers=None, params=None, timeout=None):
        return httpx.Response(403, json={"error": {"code": 403, "message": "v4 allowlist pending"}})

    with patch("core.nango_client.get_fresh_token", return_value="fake_token"), patch(
        "httpx.Client.get", side_effect=mock_get
    ):
        res = connector.pull_reviews(
            connection_id="conn_gbp",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="default",
            pull_id="pull_gbp_rev_001",
            location_ids=["loc_100"],
        )

    assert res["pull_id"] == "pull_gbp_rev_001"
    assert res["row_count"] == 0


def test_gbp_discover_accounts_topology():
    """Verify discover_accounts enumerates account -> location hierarchy."""
    def mock_get(url, headers=None, params=None, timeout=None):
        if "accounts/acc_1/locations" in url:
            return httpx.Response(
                200,
                json={
                    "locations": [
                        {"name": "accounts/acc_1/locations/loc_101", "title": "Store Downtown"}
                    ]
                },
            )
        elif "accounts" in url:
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {"name": "accounts/acc_1", "accountName": "Bistro Group"}
                    ]
                },
            )
        return httpx.Response(404)

    with patch("core.nango_client.get_fresh_token", return_value="fake_token"), patch(
        "httpx.Client.get", side_effect=mock_get
    ):
        topology = connector.discover_accounts("conn_gbp")

    assert len(topology) == 1
    assert topology[0]["id"] == "loc_101"
    assert "Bistro Group - Store Downtown" in topology[0]["label"]
