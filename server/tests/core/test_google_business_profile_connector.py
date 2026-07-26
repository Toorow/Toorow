"""Tests du connecteur Google Business Profile (Story 30.1).

Ce fichier avait cesse d'etre execute: le dossier du module s'appelle
"google-business-profile" et, avec un tiret, ce n'est pas un identifiant Python,
donc le `from google_business_profile import connector` place apres un
sys.path.insert n'a jamais pu resoudre. L'ImportError faisait tomber la collecte
de toute la suite -- et pendant ce temps le connecteur a evolue sous des
assertions que plus personne ne voyait rougir (_insert_raw_location_daily_rows
renomme _insert_raw_rows, location_ids devenu location_id, pull_reviews
supprime, prefixe business_ ajoute aux impressions).

Le connecteur est desormais charge par chemin, comme le fait deja
test_pull_campaign_launch pour meta-ads, et les assertions sont reecrites sur
l'API reelle.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_CONNECTOR_PATH = REPO_ROOT / "server" / "modules" / "google-business-profile" / "connector.py"
_spec = importlib.util.spec_from_file_location("gbp_connector_under_test", _CONNECTOR_PATH)
connector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(connector)


def test_gbp_transform_canonical_field_mapping():
    """transform() renomme les enums DailyMetric via canonical_metric_mapping (AD-2)."""
    raw_rows = [
        {
            "date": "2026-07-01",
            "location_id": "locations/loc_100",
            "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": 120,
            "CALL_CLICKS": 15,
        }
    ]

    transformed = connector.transform(raw_rows)
    assert len(transformed) == 1
    row = transformed[0]

    assert row["business_impressions_desktop_maps"] == 120
    assert row["call_clicks"] == 15
    # Les cles absentes du mapping passent inchangees.
    assert row["date"] == "2026-07-01"
    assert row["location_id"] == "locations/loc_100"


def test_gbp_insert_raw_rows_lands_one_wide_row_per_location_date(tmp_path, monkeypatch):
    """_insert_raw_rows ecrit UNE ligne large par (location, date), metriques en colonnes."""
    db_path = str(tmp_path / "test_gbp.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    rows = [
        {
            "date": "2026-07-01",
            "location_id": "locations/loc_100",
            "call_clicks": "15",
            "website_clicks": 7,
        }
    ]

    count = connector._insert_raw_rows(rows, "pull_001", "default")
    assert count == 1

    import duckdb

    con = duckdb.connect(db_path)
    res = con.execute(
        "SELECT date, location_id, call_clicks, website_clicks, "
        "business_bookings, pull_id, project_id FROM raw_gbp_location_daily"
    ).fetchall()
    con.close()

    assert len(res) == 1
    # _to_int coerce la valeur stringifiee; une metrique absente reste NULL,
    # jamais 0 -- le gap-fill est une affaire de mart, pas de landing.
    assert res[0] == ("2026-07-01", "locations/loc_100", 15, 7, None, "pull_001", "default")


def test_gbp_pull_location_daily_pivots_series_into_wide_rows(tmp_path, monkeypatch):
    """pull_location_daily pivote les series par metrique en une ligne par date."""
    db_path = str(tmp_path / "test_gbp_pull.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    def mock_get(url, params=None, headers=None, timeout=None):
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "multiDailyMetricTimeSeries": [
                    {
                        "dailyMetricTimeSeries": [
                            {
                                "dailyMetric": "CALL_CLICKS",
                                "timeSeries": {
                                    "datedValues": [
                                        {"date": {"year": 2026, "month": 7, "day": 1},
                                         "value": "25"},
                                        {"date": {"year": 2026, "month": 7, "day": 2},
                                         "value": "31"},
                                    ]
                                },
                            },
                            {
                                "dailyMetric": "WEBSITE_CLICKS",
                                "timeSeries": {
                                    "datedValues": [
                                        {"date": {"year": 2026, "month": 7, "day": 1},
                                         "value": "4"},
                                    ]
                                },
                            },
                        ]
                    }
                ]
            },
        )

    with (
        patch("core.nango_client.get_fresh_token", return_value="fake_token"),
        patch("httpx.get", side_effect=mock_get),
    ):
        res = connector.pull_location_daily(
            connection_id="conn_gbp",
            date_from="2026-07-01",
            date_to="2026-07-02",
            project_id="default",
            pull_id="pull_gbp_001",
            location_id="loc_100",
        )

    assert res["pull_id"] == "pull_gbp_001"
    # Deux dates distinctes dans les series => deux lignes larges, pas quatre.
    assert res["row_count"] == 2

    import duckdb

    con = duckdb.connect(db_path)
    res_rows = con.execute(
        "SELECT date, location_id, call_clicks, website_clicks "
        "FROM raw_gbp_location_daily ORDER BY date"
    ).fetchall()
    con.close()

    # Le jour 2 n'a pas de website_clicks: il reste NULL au landing.
    assert res_rows == [
        ("2026-07-01", "locations/loc_100", 25, 4),
        ("2026-07-02", "locations/loc_100", 31, None),
    ]


def test_gbp_pull_without_location_refuses_instead_of_falling_back():
    """Sans location selectionnee, le pull refuse -- plus de repli par env-var (25.5+)."""
    with patch("core.nango_client.get_fresh_token", return_value="fake_token"):
        with pytest.raises(ValueError, match="location_id"):
            connector.pull_location_daily(
                connection_id="conn_gbp",
                date_from="2026-07-01",
                date_to="2026-07-02",
                project_id="default",
                pull_id="pull_gbp_002",
                location_id=None,
            )


def test_gbp_discover_accounts_topology():
    """discover_accounts enumere la hierarchie compte -> etablissement."""

    def mock_get(url, params=None, headers=None, timeout=None):
        request = httpx.Request("GET", url)
        if "accounts/acc_1/locations" in url:
            return httpx.Response(
                200,
                request=request,
                json={
                    "locations": [
                        {"name": "locations/loc_101", "title": "Store Downtown"}
                    ]
                },
            )
        if url.endswith("/accounts"):
            return httpx.Response(
                200,
                request=request,
                json={"accounts": [{"name": "accounts/acc_1", "accountName": "Bistro Group"}]},
            )
        return httpx.Response(404, request=request)

    with (
        patch("core.nango_client.get_fresh_token", return_value="fake_token"),
        patch("httpx.Client.get", side_effect=mock_get),
    ):
        topology = connector.discover_accounts("conn_gbp")

    assert topology == [
        {"id": "locations/loc_101", "label": "Store Downtown", "parent": "accounts/acc_1"}
    ]
