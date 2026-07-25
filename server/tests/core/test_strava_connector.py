"""Tests for Strava connector (Story 29.1 / Epic 29 / Epic 40)."""

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

from strava import connector


def test_strava_transform_canonical_field_mapping():
    """Verify transform() maps DetailedClub fields to canonical column names."""
    raw_rows = [
        {
            "id": 12345,
            "name": "Running Club Paris",
            "city": "Paris",
            "state": "IDF",
            "country": "France",
            "private": False,
            "verified": True,
            "url": "strava-paris",
            "member_count": 150,
            "following_count": 12,
            "date": "2026-07-01",
            "is_own_club": True,
            "pull_id": "pull_001",
            # Dropped fields
            "resource_state": 2,
            "cover_photo": "https://img.com/photo.jpg",
        }
    ]

    transformed = connector.transform(raw_rows)
    assert len(transformed) == 1
    row = transformed[0]

    assert row["club_id"] == 12345
    assert row["club_name"] == "Running Club Paris"
    assert row["club_city"] == "Paris"
    assert row["is_private"] is False
    assert row["is_verified"] is True
    assert row["member_count"] == 150
    assert row["following_count"] == 12
    assert "resource_state" not in row
    assert "cover_photo" not in row


def test_strava_insert_raw_rows_and_duckdb_landing(tmp_path, monkeypatch):
    """Verify _insert_raw_rows writes canonical snapshot rows into DuckDB."""
    db_path = str(tmp_path / "test_strava.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    canonical_rows = [
        {
            "date": "2026-07-01",
            "club_id": "12345",
            "club_name": "Running Club Paris",
            "sport_type": "running",
            "club_city": "Paris",
            "club_state": "IDF",
            "club_country": "France",
            "is_private": False,
            "is_verified": True,
            "club_url": "strava-paris",
            "is_own_club": True,
            "member_count": 150,
            "following_count": 12,
        }
    ]

    count = connector._insert_raw_rows(canonical_rows, "pull_001", "default")
    assert count == 1

    import duckdb

    con = duckdb.connect(db_path)
    res = con.execute("SELECT club_id, club_name, member_count FROM raw_strava_club_daily").fetchall()
    con.close()

    assert len(res) == 1
    assert res[0] == ("12345", "Running Club Paris", 150)


def test_strava_pull_competitor_snapshot_404_unreachable_skipped(tmp_path, monkeypatch):
    """Verify 404 on competitor club is a non-fatal skip outcome (unreachable_club_ids)."""
    db_path = str(tmp_path / "test_strava_pull.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    def mock_get(url, headers=None, params=None, timeout=None):
        if "12345" in url:
            # Public reachable club
            return httpx.Response(
                200,
                json={
                    "id": 12345,
                    "name": "Public Club",
                    "member_count": 100,
                    "following_count": 10,
                },
            )
        elif "99999" in url:
            # Unreachable / private 404 club
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404)

    with patch("core.nango_client.get_fresh_token", return_value="fake_token"), patch(
        "httpx.Client.get", side_effect=mock_get
    ):
        res = connector.pull_competitor_snapshot(
            connection_id="conn_strava",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="default",
            pull_id="pull_strava_001",
            club_ids=["12345", "99999"],
            own_club_ids=["12345"],
        )

    assert res["pull_id"] == "pull_strava_001"
    assert res["row_count"] == 1
    assert res["unreachable_club_ids"] == ["99999"]


def test_strava_get_report_mcp_tool(tmp_path, monkeypatch):
    """Verify get_strava_report returns structured envelope from mart."""
    db_path = str(tmp_path / "test_strava_report.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    import duckdb

    con = duckdb.connect(db_path)
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute(
        """
        CREATE TABLE main_marts.fact_strava_club_snapshot (
            project_id VARCHAR, snapshot_date VARCHAR, club_id VARCHAR,
            club_name VARCHAR, is_own_club BOOLEAN, member_count BIGINT,
            following_count BIGINT, pull_id VARCHAR, loaded_at VARCHAR
        )
        """
    )
    con.execute(
        """
        INSERT INTO main_marts.fact_strava_club_snapshot VALUES
        ('default', '2026-07-01', '12345', 'Paris Runners', true, 150, 12, 'pull_001', '2026-07-01T00:00:00Z')
        """
    )
    con.close()

    res = connector.get_strava_report(
        project_id="default",
        report_profile="competitor_snapshot",
        date_from="2026-06-01",
        date_to="2026-07-01",
    )

    assert res["schema_version"] == "1"
    assert len(res["data"]["clubs"]) == 1
    assert res["data"]["clubs"][0]["club_name"] == "Paris Runners"
    assert res["data"]["clubs"][0]["member_count"] == 150
