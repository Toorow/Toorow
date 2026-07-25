"""Unit tests for the GA4 connector get_ga4_report tool — Story 1.4, T6.5.

Tests mock the DuckDB query to assert the canonical AD-1 envelope shape
with real (non-empty) row data — replacing the Story 1.3 stub assertions.

All tests are fast (no I/O except via unittest.mock).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import pytest

_TOOROW_PATH = (
    Path(__file__).parents[4]
    / "server"
    / "modules"
    / "google-analytics"
    / "connector.py"
)


def _import_connector():
    """Dynamically import connector.py from the hyphenated module directory."""
    spec = importlib.util.spec_from_file_location("connector_ga4", _TOOROW_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# Fake mart rows — simulate what the DuckDB mart query returns
# ---------------------------------------------------------------------------

_FAKE_ROWS = [
    {
        "metric": "sessions",
        "breakdown_dimension": "device_category",
        "breakdown_value": "desktop",
        "value": 1500.0,
        "pull_id": "pull_01JTEST000000000000000001",
        "freshness": "2026-07-09T10:00:00Z",
    },
    {
        "metric": "sessions",
        "breakdown_dimension": "country",
        "breakdown_value": "France",
        "value": 800.0,
        "pull_id": "pull_01JTEST000000000000000001",
        "freshness": "2026-07-09T10:00:00Z",
    },
    {
        "metric": "active_users",
        "breakdown_dimension": "device_category",
        "breakdown_value": "desktop",
        "value": 1200.0,
        "pull_id": "pull_01JTEST000000000000000001",
        "freshness": "2026-07-09T10:00:00Z",
    },
    {
        "metric": "conversions",
        "breakdown_dimension": "device_category",
        "breakdown_value": "mobile",
        "value": 45.0,
        "pull_id": "pull_01JTEST000000000000000001",
        "freshness": "2026-07-09T10:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# Tests for _build_envelope
# ---------------------------------------------------------------------------


def test_envelope_has_canonical_keys(connector):
    """_build_envelope returns the canonical AD-1 envelope structure."""
    envelope = connector._build_envelope(
        rows=_FAKE_ROWS,
        report_profile="standard_daily",
        date_from="2026-04-01",
        date_to="2026-06-30",
        project_id="default",
    )
    assert "schema_version" in envelope
    assert "meta" in envelope
    assert "data" in envelope


def test_envelope_meta_provenance_is_dict(connector):
    """meta.provenance must be a dict with NFR8 keys (Story 2.7 AC5 upgrade from scalar)."""
    envelope = connector._build_envelope(
        rows=_FAKE_ROWS,
        report_profile="standard_daily",
        date_from="2026-04-01",
        date_to="2026-06-30",
        project_id="default",
    )
    meta = envelope["meta"]
    prov = meta["provenance"]
    assert isinstance(prov, dict), f"provenance must be a dict (NFR8), got: {type(prov)!r}"
    assert prov["source_system"] == "google-analytics"
    assert prov["source_field"] == "fact_daily_kpi"
    assert prov["pull_id"] == "pull_01JTEST000000000000000001"
    assert prov["pull_id"].startswith("pull_"), (
        f"provenance.pull_id {prov['pull_id']!r} must start with 'pull_'"
    )


def test_envelope_meta_freshness_populated(connector):
    """meta.freshness must be the MAX(loaded_at) from the result set."""
    envelope = connector._build_envelope(
        rows=_FAKE_ROWS,
        report_profile="standard_daily",
        date_from="2026-04-01",
        date_to="2026-06-30",
        project_id="default",
    )
    assert envelope["meta"]["freshness"] == "2026-07-09T10:00:00Z"


def test_envelope_data_has_metrics(connector):
    """data.metrics must be a non-empty dict grouped by metric name (AC5, Story 1.4)."""
    envelope = connector._build_envelope(
        rows=_FAKE_ROWS,
        report_profile="standard_daily",
        date_from="2026-04-01",
        date_to="2026-06-30",
        project_id="default",
    )
    data = envelope["data"]
    assert "metrics" in data, "data must have 'metrics' key (Story 1.4 shape)"
    metrics = data["metrics"]
    assert len(metrics) > 0, "data.metrics must be non-empty for real mart data"
    assert "sessions" in metrics
    assert "active_users" in metrics
    assert "conversions" in metrics


def test_envelope_metrics_have_breakdown(connector):
    """Each metric entry must include breakdown_dimension and breakdown_value."""
    envelope = connector._build_envelope(
        rows=_FAKE_ROWS,
        report_profile="standard_daily",
        date_from="2026-04-01",
        date_to="2026-06-30",
        project_id="default",
    )
    for metric_name, breakdown_rows in envelope["data"]["metrics"].items():
        assert isinstance(breakdown_rows, list), f"{metric_name}: must be a list"
        for row in breakdown_rows:
            assert "breakdown_dimension" in row, f"{metric_name}: missing breakdown_dimension"
            assert "breakdown_value" in row, f"{metric_name}: missing breakdown_value"
            assert "value" in row, f"{metric_name}: missing value"


def test_envelope_no_ratio_metrics(connector):
    """data.metrics must NOT contain ratio columns (CVR, CTR, ROAS) — AD-4."""
    envelope = connector._build_envelope(
        rows=_FAKE_ROWS,
        report_profile="standard_daily",
        date_from="2026-04-01",
        date_to="2026-06-30",
        project_id="default",
    )
    metrics = envelope["data"]["metrics"]
    forbidden = {"cvr", "ctr", "roas", "sessions_per_user"}
    found = forbidden & set(metrics.keys())
    assert not found, f"Ratio metrics found in envelope (violates AD-4): {found}"


def test_empty_rows_envelope_has_null_provenance(connector):
    """When no mart rows match, provenance and freshness should be None."""
    envelope = connector._build_envelope(
        rows=[],
        report_profile="standard_daily",
        date_from="2026-04-01",
        date_to="2026-06-30",
        project_id="default",
    )
    assert envelope["meta"]["provenance"] is None
    assert envelope["meta"]["freshness"] is None
    assert envelope["data"]["metrics"] == {}


def test_get_ga4_report_calls_duckdb_when_mode_duckdb(connector, tmp_path):
    """get_ga4_report queries DuckDB (not CSV, not raw tables) in duckdb mode (AD-12)."""
    with (
        patch.dict(
            os.environ,
            {
                "TOOROW_DB_MODE": "duckdb",
                "TOOROW_DUCKDB_PATH": str(tmp_path / "nonexistent.duckdb"),
            },
        ),
        patch.object(connector, "_query_duckdb", return_value=_FAKE_ROWS) as mock_query,
    ):
        envelope = connector.get_ga4_report(
            project_id="default",
            report_profile="standard_daily",
            date_from="2026-04-01",
            date_to="2026-06-30",
        )

    # _query_duckdb must have been called (not CSV / raw read)
    mock_query.assert_called_once()
    # SQL must reference fact_daily_kpi (the mart), not raw_ga4_standard_daily
    sql_arg = mock_query.call_args[0][0]
    assert "fact_daily_kpi" in sql_arg, "query must target fact_daily_kpi mart (AD-12)"
    assert "raw_ga4" not in sql_arg.lower(), "query must NOT target raw tables (AD-12)"

    # Envelope must be well-formed
    assert envelope["schema_version"] == "1"
    # NFR8 (Story 2.7): provenance is now a dict, not a scalar string
    prov = envelope["meta"]["provenance"]
    assert isinstance(prov, dict)
    assert prov["pull_id"] == "pull_01JTEST000000000000000001"


def test_get_ga4_report_returns_error_envelope_on_db_failure(connector, tmp_path):
    """get_ga4_report returns a structured error envelope (not exception) on DB failure."""
    with patch.dict(
        os.environ,
        {
            "TOOROW_DB_MODE": "duckdb",
            "TOOROW_DUCKDB_PATH": str(tmp_path / "missing.duckdb"),
        },
    ):
        # DuckDB file doesn't exist + no mart table → should return error envelope
        envelope = connector.get_ga4_report(
            project_id="default",
            report_profile="standard_daily",
            date_from="2026-04-01",
            date_to="2026-06-30",
        )

    # Must still be an envelope, not a raised exception
    assert "schema_version" in envelope
    assert "meta" in envelope
    # F-08: the mart table cannot exist in a fresh/auto-created DB, so this
    # code path MUST surface an error alert — an empty list would mean the
    # error-handling path silently produced a healthy-looking envelope.
    alerts = envelope["meta"].get("alerts", [])
    assert len(alerts) > 0, "Expected an error alert when the mart table is missing"
    assert alerts[0].get("severity") in ("error", "critical") or alerts[0].get("level") in (
        "error",
        "critical",
    ), f"Expected error-severity alert, got: {alerts[0]}"
