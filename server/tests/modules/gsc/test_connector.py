"""Tests for the GSC connector — Story 6.2 (AC3, AC5, AC15).

Covers get_gsc_report envelope shape, transform() canonical mapping,
and the AD-4 non-additive guard (average_position must not be summed).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_TOOROW_PATH = (
    Path(__file__).parents[4] / "server" / "modules" / "gsc" / "connector.py"
)


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_gsc_tests", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# get_gsc_report envelope shape (AC3, AC15)
# ---------------------------------------------------------------------------


def test_get_gsc_report_returns_envelope(connector):
    """get_gsc_report returns the canonical AD-1 envelope from mart rows."""
    fake_rows = [
        {
            "metric": "clicks",
            "breakdown_dimension": "page",
            "breakdown_value": "https://example.com/blog/",
            "value": 42.0,
            "pull_id": "pull_gsc_001",
            "freshness": "2026-07-01T10:00:00Z",
        },
        {
            "metric": "impressions",
            "breakdown_dimension": "page",
            "breakdown_value": "https://example.com/blog/",
            "value": 850.0,
            "pull_id": "pull_gsc_001",
            "freshness": "2026-07-01T10:00:00Z",
        },
        {
            "metric": "average_position",
            "breakdown_dimension": "page",
            "breakdown_value": "https://example.com/blog/",
            "value": 7.3,
            "pull_id": "pull_gsc_001",
            "freshness": "2026-07-01T10:00:00Z",
        },
    ]
    with patch.object(connector, "_query_mart", return_value=fake_rows):
        env = connector.get_gsc_report(
            project_id="jean-gsc",
            report_profile="page_daily",
            date_from="2026-07-01",
            date_to="2026-07-03",
        )

    assert env["schema_version"] == "1"
    assert env["meta"]["alerts"] == []
    assert env["meta"]["provenance"]["source_system"] == "gsc"
    assert env["meta"]["provenance"]["source_field"] == "fact_daily_kpi"
    assert env["data"]["project_id"] == "jean-gsc"
    assert env["data"]["report_profile"] == "page_daily"
    assert set(env["data"]["metrics"].keys()) == {"clicks", "impressions", "average_position"}


def test_get_gsc_report_empty_result(connector):
    """Empty mart: alerts empty, metrics empty dict, provenance None."""
    with patch.object(connector, "_query_mart", return_value=[]):
        env = connector.get_gsc_report(
            project_id="jean-gsc",
            date_from="2026-07-01",
            date_to="2026-07-03",
        )
    assert env["meta"]["alerts"] == []
    assert env["meta"]["provenance"] is None
    assert env["data"]["metrics"] == {}


def test_get_gsc_report_error_becomes_alert(connector):
    """A mart query failure surfaces as a single error alert, not an exception."""
    with patch.object(connector, "_query_mart", side_effect=RuntimeError("gsc boom")):
        env = connector.get_gsc_report(
            project_id="jean-gsc",
            date_from="2026-07-01",
            date_to="2026-07-03",
        )
    assert env["data"]["metrics"] == {}
    assert env["meta"]["alerts"][0]["level"] == "error"
    assert "gsc boom" in env["meta"]["alerts"][0]["message"]


# ---------------------------------------------------------------------------
# transform() — canonical field mapping (AC3, AC4, AC15)
# ---------------------------------------------------------------------------


def test_transform_maps_position_to_average_position(connector):
    """transform renames the GSC 'position' field to canonical 'average_position'."""
    out = connector.transform([{"position": 7.3, "date": "2026-07-01"}])
    assert out[0]["average_position"] == 7.3
    assert "position" not in out[0]
    assert out[0]["date"] == "2026-07-01"


def test_transform_discards_ctr(connector):
    """AD-4 ratio rule: transform must NOT include ctr in transformed output."""
    row = {
        "position": 7.3,
        "clicks": 42,
        "impressions": 850,
        "ctr": 0.049,
        "date": "2026-07-01",
    }
    out = connector.transform([row])
    assert "ctr" not in out[0], "ctr is a ratio metric and must be discarded"
    assert out[0]["average_position"] == 7.3
    assert out[0]["clicks"] == 42
    assert out[0]["impressions"] == 850


def test_transform_does_not_sum_average_position(connector):
    """AD-4: transform is row-level — it stores each row individually, no aggregation.

    Multi-row input: naive SUM would give 10.4, but transform stores each row's
    value individually (7.3 and 3.1). The semantic layer handles cross-row aggregation.
    """
    rows = [
        {"position": 7.3, "impressions": 850, "clicks": 42},
        {"position": 3.1, "impressions": 200, "clicks": 15},
    ]
    out = connector.transform(rows)
    assert len(out) == 2, "transform must not aggregate rows"
    assert out[0]["average_position"] == 7.3
    assert out[1]["average_position"] == 3.1
    # Confirm naive SUM would differ from the expected weighted avg
    naive_sum = out[0]["average_position"] + out[1]["average_position"]
    assert naive_sum == pytest.approx(10.4), "just verifying the test data is non-trivial"


def test_transform_passthrough_provenance(connector):
    """Fields not in either mapping (pull_id, connector) pass through unchanged."""
    out = connector.transform(
        [{"pull_id": "pull_gsc_001", "connector": "gsc", "position": 5.0, "clicks": 10}]
    )
    assert out[0]["pull_id"] == "pull_gsc_001"
    assert out[0]["connector"] == "gsc"
    assert out[0]["average_position"] == 5.0


def test_transform_clicks_impressions_pass_through(connector):
    """clicks and impressions are additive identity mappings — pass through unchanged."""
    out = connector.transform([{"clicks": 42, "impressions": 850, "position": 7.3}])
    assert out[0]["clicks"] == 42
    assert out[0]["impressions"] == 850
    assert out[0]["average_position"] == 7.3
