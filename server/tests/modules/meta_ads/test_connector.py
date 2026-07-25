"""Tests for the Meta Ads connector — Story 3.6 (AC3, AC10, AC11).

Covers get_meta_ads_report envelope shape, transform() canonical mapping, and
the AI-10 aggregation-guard exemption (all Meta metrics are additive at day
grain, so no guard error is raised).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_TOOROW_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "meta-ads" / "connector.py"
)


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_meta_ads", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# get_meta_ads_report envelope shape (AC3)
# ---------------------------------------------------------------------------


def test_get_meta_ads_report_returns_envelope(connector):
    """get_meta_ads_report returns the canonical AD-1 envelope from mart rows."""
    fake_rows = [
        {
            "metric": "cost",
            "breakdown_dimension": "campaign_id",
            "breakdown_value": "23851234500010001",
            "value": 250.75,
            "pull_id": "pull_meta_001",
            "freshness": "2026-07-01T10:00:00Z",
        },
        {
            "metric": "impressions",
            "breakdown_dimension": "campaign_id",
            "breakdown_value": "23851234500010001",
            "value": 48200.0,
            "pull_id": "pull_meta_001",
            "freshness": "2026-07-01T10:00:00Z",
        },
    ]
    with patch.object(connector, "_query_mart", return_value=fake_rows):
        env = connector.get_meta_ads_report(
            project_id="jean-meta",
            report_profile="campaign_daily",
            date_from="2026-07-01",
            date_to="2026-07-03",
        )

    assert env["schema_version"] == "1"
    assert env["meta"]["alerts"] == []
    assert env["meta"]["provenance"]["source_system"] == "meta-ads"
    assert env["meta"]["provenance"]["source_field"] == "fact_daily_kpi"
    assert env["data"]["project_id"] == "jean-meta"
    assert env["data"]["report_profile"] == "campaign_daily"
    assert set(env["data"]["metrics"].keys()) == {"cost", "impressions"}


def test_get_meta_ads_report_empty_result(connector):
    """Empty mart: alerts empty, metrics empty dict, provenance None."""
    with patch.object(connector, "_query_mart", return_value=[]):
        env = connector.get_meta_ads_report(
            project_id="jean-meta",
            date_from="2026-07-01",
            date_to="2026-07-03",
        )
    assert env["meta"]["alerts"] == []
    assert env["meta"]["provenance"] is None
    assert env["data"]["metrics"] == {}


def test_get_meta_ads_report_error_becomes_alert(connector):
    """A mart query failure surfaces as a single error alert, not an exception."""
    with patch.object(connector, "_query_mart", side_effect=RuntimeError("boom")):
        env = connector.get_meta_ads_report(
            project_id="jean-meta",
            date_from="2026-07-01",
            date_to="2026-07-03",
        )
    assert env["data"]["metrics"] == {}
    assert env["meta"]["alerts"][0]["level"] == "error"
    assert "boom" in env["meta"]["alerts"][0]["message"]


# ---------------------------------------------------------------------------
# transform() canonical mapping (AC3)
# ---------------------------------------------------------------------------


def test_transform_maps_spend_to_cost(connector):
    """transform renames the Meta 'spend' field to the canonical 'cost'."""
    out = connector.transform([{"spend": 100, "date": "2026-07-01"}])
    assert out[0]["cost"] == 100
    assert "spend" not in out[0]
    assert out[0]["date"] == "2026-07-01"


def test_transform_maps_conversions_unchanged(connector):
    """conversions passes through transform unchanged (identity mapping)."""
    out = connector.transform([{"conversions": 42, "impressions": 900, "clicks": 30}])
    assert out[0]["conversions"] == 42
    assert out[0]["impressions"] == 900
    assert out[0]["clicks"] == 30


def test_transform_passthrough_provenance(connector):
    """Fields not in either mapping (pull_id, connector) pass through unchanged."""
    out = connector.transform(
        [{"pull_id": "pull_meta_001", "connector": "meta-ads", "spend": 5}]
    )
    assert out[0]["pull_id"] == "pull_meta_001"
    assert out[0]["connector"] == "meta-ads"
    assert out[0]["cost"] == 5


# ---------------------------------------------------------------------------
# AI-10 — aggregation guard exemption at day grain (AC11)
# ---------------------------------------------------------------------------


def test_day_grain_report_raises_no_aggregation_guard(connector):
    """AI-10: all Meta metrics are additive at day grain — no guard error.

    Confirms get_meta_ads_report at day granularity returns a normal envelope
    (no exception, no error alert) for every additive metric.
    """
    fake_rows = [
        {
            "metric": m,
            "breakdown_dimension": "campaign_id",
            "breakdown_value": "c1",
            "value": 1.0,
            "pull_id": "pull_meta_001",
            "freshness": "2026-07-01T10:00:00Z",
        }
        for m in ("cost", "impressions", "clicks", "conversions")
    ]
    with patch.object(connector, "_query_mart", return_value=fake_rows):
        env = connector.get_meta_ads_report(
            project_id="jean-meta",
            date_from="2026-07-01",
            date_to="2026-07-01",
        )
    assert env["meta"]["alerts"] == []
    assert set(env["data"]["metrics"].keys()) == {
        "cost",
        "impressions",
        "clicks",
        "conversions",
    }
