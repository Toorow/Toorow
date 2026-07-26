"""Tests for pull_catalog_daily — Story 25.9.

Covers:
  - Statistics list built from the selection's source_fields (including drift bridging)
  - Drift bridging: sends→recipients, revenue→conversion_value at request time
  - Both campaign and flow paths execute when neither dimension set is exclusive
  - Campaign-only path when selection only has campaign dimensions
  - Flow-only path when selection only has flow dimensions
  - AD-22: existing pull() / pull_campaign_daily() / pull_flow_daily() still
    callable (dispatch green)
  - AI-58: pull_catalog_daily is callable as a standalone dispatch target
  - 401 typed error re-raise propagates from pull_catalog_daily
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

_CONNECTOR_PATH = Path(__file__).parents[4] / "server" / "modules" / "klaviyo" / "connector.py"

_KLAVIYO_CAMPAIGN_URL = "https://a.klaviyo.com/api/campaign-values-reports/"
_KLAVIYO_FLOW_URL = "https://a.klaviyo.com/api/flow-series-reports/"

# Minimal well-formed Reporting API response for one result item over one date.
_CAMPAIGN_RESULT = {
    "data": {
        "attributes": {
            "results": [
                {
                    "groupings": {"campaign_id": "CAMP_001"},
                    "statistics": {
                        "dates": ["2026-07-01"],
                        "recipients": [1000.0],
                        "opens": [250.0],
                        "clicks": [50.0],
                        "conversions": [5.0],
                        "conversion_value": [99.95],
                    },
                }
            ]
        }
    }
}

_FLOW_RESULT = {
    "data": {
        "attributes": {
            "results": [
                {
                    "groupings": {"flow_id": "FLOW_001", "flow_name": "Welcome"},
                    "statistics": {
                        "dates": ["2026-07-01"],
                        "recipients": [500.0],
                        "opens": [100.0],
                        "clicks": [20.0],
                        "conversions": [2.0],
                        "conversion_value": [39.90],
                    },
                }
            ]
        }
    }
}

_EMPTY_RESULT = {"data": {"attributes": {"results": []}}}


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_klaviyo_cd", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# ---------------------------------------------------------------------------
# Unit tests for the helper functions (no HTTP)
# ---------------------------------------------------------------------------


def test_build_catalog_statistics_applies_drift_bridge(connector):
    """Statistics built from the selection apply sends->recipients and revenue->conversion_value.

    This validates the 25.6 probe adjudication: the catalog's source_field carries
    the legacy manifest token ("sends", "revenue"); pull_catalog_daily must bridge
    to the official Klaviyo statistic names at request time.
    """
    selection = {
        "metrics": ["sends", "opens", "clicks", "attributed_conversions", "attributed_revenue"],
        "dimensions": ["date", "campaign_id"],
        "source_fields": {
            "sends": "sends",  # legacy; bridges to "recipients"
            "opens": "opens",
            "clicks": "clicks",
            "attributed_conversions": "conversions",
            "attributed_revenue": "revenue",  # legacy; bridges to "conversion_value"
            "date": "date",
            "campaign_id": "campaign_id",
        },
    }
    stats = connector._build_catalog_statistics(selection)
    # Drift bridges applied:
    assert "recipients" in stats, "sends must bridge to recipients"
    assert "sends" not in stats, (
        "'sends' must not appear in the statistics list (not a Klaviyo stat)"
    )
    assert "conversion_value" in stats, "revenue must bridge to conversion_value"
    assert "revenue" not in stats, (
        "'revenue' must not appear in the statistics list (not a Klaviyo stat)"
    )
    # Pass-through stats:
    assert "opens" in stats
    assert "clicks" in stats
    assert "conversions" in stats


def test_build_catalog_statistics_passthrough_official_names(connector):
    """When source_fields already use official names, no bridge is applied."""
    selection = {
        "metrics": ["recipients_field"],
        "dimensions": [],
        "source_fields": {"recipients_field": "recipients"},
    }
    stats = connector._build_catalog_statistics(selection)
    assert stats == ["recipients"]


def test_build_catalog_groupings_campaign(connector):
    """Campaign groupings include only valid campaign-endpoint keys."""
    selection = {
        "metrics": ["sends"],
        "dimensions": ["date", "campaign_id", "flow_id"],
        "source_fields": {
            "sends": "sends",
            "date": "date",
            "campaign_id": "campaign_id",
            "flow_id": "flow_id",
        },
    }
    groupings = connector._build_catalog_groupings(selection, "campaign")
    # campaign_id is valid on campaign endpoint, flow_id is NOT
    assert "campaign_id" in groupings
    assert "flow_id" not in groupings
    # date is structural; must be excluded
    assert "date" not in groupings


def test_build_catalog_groupings_flow(connector):
    """Flow groupings include only valid flow-endpoint keys."""
    selection = {
        "metrics": ["opens"],
        "dimensions": ["date", "flow_id", "flow_name", "campaign_id"],
        "source_fields": {
            "opens": "opens",
            "date": "date",
            "flow_id": "flow_id",
            "flow_name": "flow_name",
            "campaign_id": "campaign_id",
        },
    }
    groupings = connector._build_catalog_groupings(selection, "flow")
    assert "flow_id" in groupings
    assert "flow_name" in groupings
    # campaign_id is not a valid flow grouping key
    assert "campaign_id" not in groupings
    assert "date" not in groupings


def test_selection_wants_campaign_and_flow_by_default(connector):
    """A selection with only structural dims (date) triggers both paths."""
    selection = {
        "metrics": ["sends"],
        "dimensions": ["date"],
        "source_fields": {"sends": "sends", "date": "date"},
    }
    assert connector._selection_wants_campaign(selection) is True
    assert connector._selection_wants_flow(selection) is True


def test_selection_wants_campaign_only(connector):
    """A selection with campaign_id and no flow dims → campaign path only."""
    selection = {
        "metrics": ["sends"],
        "dimensions": ["date", "campaign_id"],
        "source_fields": {"sends": "sends", "date": "date", "campaign_id": "campaign_id"},
    }
    assert connector._selection_wants_campaign(selection) is True
    assert connector._selection_wants_flow(selection) is False


def test_selection_wants_flow_only(connector):
    """A selection with flow_id and no campaign dims → flow path only."""
    selection = {
        "metrics": ["opens"],
        "dimensions": ["date", "flow_id"],
        "source_fields": {"opens": "opens", "date": "date", "flow_id": "flow_id"},
    }
    assert connector._selection_wants_campaign(selection) is False
    assert connector._selection_wants_flow(selection) is True


def test_parse_catalog_reporting_row_reverse_bridges(connector):
    """Response official statistic names are reverse-bridged back to legacy column names."""
    result_item = {
        "groupings": {"campaign_id": "CAMP_X"},
        "statistics": {
            "dates": ["2026-07-01"],
            "recipients": [999.0],
            "conversion_value": [49.99],
            "opens": [100.0],
        },
    }
    official_stats = ["recipients", "conversion_value", "opens"]
    row = connector._parse_catalog_reporting_row(
        result_item, "2026-07-01", "campaign", official_stats
    )
    # Reverse bridge: recipients → sends column, conversion_value → revenue column
    assert row["sends"] == 999.0, "recipients must map back to 'sends' legacy column"
    assert row["revenue"] == 49.99, "conversion_value must map back to 'revenue' legacy column"
    assert row["opens"] == 100.0
    assert row["campaign_id"] == "CAMP_X"
    assert row["flow_id"] is None


def test_parse_catalog_reporting_row_date_miss(connector):
    """A result_item whose dates array does not include the queried date returns {}."""
    result_item = {
        "groupings": {"campaign_id": "CAMP_Y"},
        "statistics": {
            "dates": ["2026-07-02"],
            "recipients": [10.0],
        },
    }
    row = connector._parse_catalog_reporting_row(
        result_item, "2026-07-01", "campaign", ["recipients"]
    )
    assert row == {}


def test_parse_catalog_reporting_row_null_honest(connector):
    """A statistic missing from the response stays None (AD-9 null honest)."""
    result_item = {
        "groupings": {"flow_id": "FL_1"},
        "statistics": {
            "dates": ["2026-07-01"],
            # "opens" intentionally absent
        },
    }
    row = connector._parse_catalog_reporting_row(result_item, "2026-07-01", "flow", ["opens"])
    assert row.get("opens") is None


# ---------------------------------------------------------------------------
# Integration tests with respx mocks — both campaign + flow paths
# ---------------------------------------------------------------------------


@respx.mock
def test_pull_catalog_daily_both_paths_statistics_list(connector, tmp_path, monkeypatch):
    """pull_catalog_daily calls BOTH endpoints with bridged statistics when no exclusive dims.

    Validates:
      - campaign endpoint receives official statistics
        (recipients, conversion_value, not sends/revenue)
      - flow endpoint receives the same official statistics
      - returns {"pull_id", "row_count", "date_from", "date_to"}
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_cat_both.duckdb"))
    monkeypatch.setenv("KLAVIYO_CONVERSION_METRIC_ID", "CONV_METRIC_001")

    campaign_route = respx.post(_KLAVIYO_CAMPAIGN_URL).mock(
        return_value=httpx.Response(200, json=_CAMPAIGN_RESULT)
    )
    flow_route = respx.post(_KLAVIYO_FLOW_URL).mock(
        return_value=httpx.Response(200, json=_FLOW_RESULT)
    )

    selection = {
        "metrics": ["sends", "opens", "clicks", "attributed_conversions", "attributed_revenue"],
        "dimensions": ["date"],
        "source_fields": {
            "sends": "sends",
            "opens": "opens",
            "clicks": "clicks",
            "attributed_conversions": "conversions",
            "attributed_revenue": "revenue",
            "date": "date",
        },
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-key"):
        result = connector.pull_catalog_daily(
            connection_id="conn_kl_cat",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="proj_cat_test",
            pull_id="pull_cat_001",
            selection=selection,
            conversion_metric_id="CONV_METRIC_001",
        )

    assert result["pull_id"] == "pull_cat_001"
    assert result["date_from"] == "2026-07-01"
    assert result["date_to"] == "2026-07-01"
    assert "row_count" in result

    # Both endpoints must have been called.
    assert campaign_route.called, "Campaign endpoint must be called when no exclusive dims"
    assert flow_route.called, "Flow endpoint must be called when no exclusive dims"

    # Validate the statistics in the campaign request body (drift bridge applied).
    campaign_request_body = json.loads(campaign_route.calls[0].request.content)
    stats_sent = campaign_request_body["data"]["attributes"]["statistics"]
    assert "recipients" in stats_sent, "Official 'recipients' must be in statistics"
    assert "conversion_value" in stats_sent, "Official 'conversion_value' must be in statistics"
    assert "sends" not in stats_sent, "'sends' must NOT appear (bridged to 'recipients')"
    assert "revenue" not in stats_sent, "'revenue' must NOT appear (bridged to 'conversion_value')"


@respx.mock
def test_pull_catalog_daily_campaign_only_skips_flow(connector, tmp_path, monkeypatch):
    """When selection has only campaign dims, flow endpoint is NOT called."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_cat_camp.duckdb"))
    monkeypatch.setenv("KLAVIYO_CONVERSION_METRIC_ID", "CONV_METRIC_001")

    campaign_route = respx.post(_KLAVIYO_CAMPAIGN_URL).mock(
        return_value=httpx.Response(200, json=_CAMPAIGN_RESULT)
    )
    flow_route = respx.post(_KLAVIYO_FLOW_URL).mock(
        return_value=httpx.Response(200, json=_EMPTY_RESULT)
    )

    selection = {
        "metrics": ["sends", "opens"],
        "dimensions": ["date", "campaign_id"],
        "source_fields": {
            "sends": "sends",
            "opens": "opens",
            "date": "date",
            "campaign_id": "campaign_id",
        },
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-key"):
        result = connector.pull_catalog_daily(
            connection_id="conn_kl_cat",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="proj_cat_camp",
            pull_id="pull_cat_camp",
            selection=selection,
            conversion_metric_id="CONV_METRIC_001",
        )

    assert campaign_route.called, "Campaign endpoint must be called"
    assert not flow_route.called, "Flow endpoint must NOT be called (campaign-only selection)"
    assert result["pull_id"] == "pull_cat_camp"


@respx.mock
def test_pull_catalog_daily_flow_only_skips_campaign(connector, tmp_path, monkeypatch):
    """When selection has only flow dims, campaign endpoint is NOT called."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_cat_flow.duckdb"))
    monkeypatch.setenv("KLAVIYO_CONVERSION_METRIC_ID", "CONV_METRIC_001")

    campaign_route = respx.post(_KLAVIYO_CAMPAIGN_URL).mock(
        return_value=httpx.Response(200, json=_EMPTY_RESULT)
    )
    flow_route = respx.post(_KLAVIYO_FLOW_URL).mock(
        return_value=httpx.Response(200, json=_FLOW_RESULT)
    )

    selection = {
        "metrics": ["opens", "clicks"],
        "dimensions": ["date", "flow_id", "flow_name"],
        "source_fields": {
            "opens": "opens",
            "clicks": "clicks",
            "date": "date",
            "flow_id": "flow_id",
            "flow_name": "flow_name",
        },
    }

    with patch("core.nango_client.get_fresh_token", return_value="fake-key"):
        result = connector.pull_catalog_daily(
            connection_id="conn_kl_cat",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="proj_cat_flow",
            pull_id="pull_cat_flow",
            selection=selection,
            conversion_metric_id="CONV_METRIC_001",
        )

    assert not campaign_route.called, "Campaign endpoint must NOT be called (flow-only selection)"
    assert flow_route.called, "Flow endpoint must be called"
    assert result["pull_id"] == "pull_cat_flow"


@respx.mock
def test_pull_catalog_daily_typed_error_propagates(connector, tmp_path, monkeypatch):
    """A 401 from pull_catalog_daily raises AuthExpiredError (typed, not swallowed)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_cat_401.duckdb"))
    monkeypatch.setenv("KLAVIYO_CONVERSION_METRIC_ID", "CONV_METRIC_001")

    _JSONAPI_401 = {
        "errors": [
            {
                "id": "e1",
                "status": "401",
                "code": "not_authenticated",
                "title": "Unauthorized",
                "detail": "Bad key.",
            }
        ]
    }
    respx.post(_KLAVIYO_CAMPAIGN_URL).mock(return_value=httpx.Response(401, json=_JSONAPI_401))
    respx.post(_KLAVIYO_FLOW_URL).mock(return_value=httpx.Response(401, json=_JSONAPI_401))

    selection = {
        "metrics": ["sends"],
        "dimensions": ["date"],
        "source_fields": {"sends": "sends", "date": "date"},
    }

    from core.pull_errors import AuthExpiredError

    with patch("core.nango_client.get_fresh_token", return_value="bad-key"):
        with pytest.raises(AuthExpiredError) as exc_info:
            connector.pull_catalog_daily(
                connection_id="conn_kl_cat",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="proj_cat_401",
                pull_id="pull_cat_401",
                selection=selection,
                conversion_metric_id="CONV_METRIC_001",
            )

    assert exc_info.value.error_class == "auth_expired"


def test_pull_catalog_daily_missing_conversion_metric_raises(connector, tmp_path, monkeypatch):
    """pull_catalog_daily raises ValueError when conversion_metric_id is absent."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_cat_nomid.duckdb"))
    monkeypatch.delenv("KLAVIYO_CONVERSION_METRIC_ID", raising=False)

    with patch("core.nango_client.get_fresh_token", return_value="fake-key"):
        with pytest.raises(ValueError, match="KLAVIYO_CONVERSION_METRIC_ID"):
            connector.pull_catalog_daily(
                connection_id="conn_kl_cat",
                date_from="2026-07-01",
                date_to="2026-07-01",
                project_id="proj_cat_nomid",
                pull_id="pull_cat_nomid",
                selection=None,
                conversion_metric_id=None,
            )


# ---------------------------------------------------------------------------
# AD-22: existing profiles unaffected (legacy dispatch green)
# ---------------------------------------------------------------------------


@respx.mock
def test_ad22_pull_campaign_daily_still_callable(connector, tmp_path, monkeypatch):
    """AD-22: pull_campaign_daily (legacy) still callable; pull_catalog_daily is additive."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_ad22_camp.duckdb"))
    monkeypatch.setenv("KLAVIYO_CONVERSION_METRIC_ID", "CONV_METRIC_001")

    respx.post(_KLAVIYO_CAMPAIGN_URL).mock(return_value=httpx.Response(200, json=_CAMPAIGN_RESULT))
    respx.post(_KLAVIYO_FLOW_URL).mock(return_value=httpx.Response(200, json=_FLOW_RESULT))

    with patch("core.nango_client.get_fresh_token", return_value="fake-key"):
        result = connector.pull_campaign_daily(
            connection_id="conn_kl_ad22",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="proj_ad22",
            pull_id="pull_ad22_camp",
            conversion_metric_id="CONV_METRIC_001",
        )

    assert result["pull_id"] == "pull_ad22_camp"


@respx.mock
def test_ad22_pull_flow_daily_still_callable(connector, tmp_path, monkeypatch):
    """AD-22: pull_flow_daily (legacy) still callable."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "kl_ad22_flow.duckdb"))
    monkeypatch.setenv("KLAVIYO_CONVERSION_METRIC_ID", "CONV_METRIC_001")

    respx.post(_KLAVIYO_CAMPAIGN_URL).mock(return_value=httpx.Response(200, json=_CAMPAIGN_RESULT))
    respx.post(_KLAVIYO_FLOW_URL).mock(return_value=httpx.Response(200, json=_FLOW_RESULT))

    with patch("core.nango_client.get_fresh_token", return_value="fake-key"):
        result = connector.pull_flow_daily(
            connection_id="conn_kl_ad22",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="proj_ad22_flow",
            pull_id="pull_ad22_flow",
            conversion_metric_id="CONV_METRIC_001",
        )

    assert result["pull_id"] == "pull_ad22_flow"


# ---------------------------------------------------------------------------
# AI-58: pull_catalog_daily dispatch target callable standalone
# ---------------------------------------------------------------------------


def test_ai58_pull_catalog_daily_is_callable_attribute(connector):
    """AI-58: pull_catalog_daily is a module-level callable (dispatch convention)."""
    assert callable(getattr(connector, "pull_catalog_daily", None)), (
        "pull_catalog_daily must exist as a callable on the connector module "
        "(AI-58 dispatch naming convention)"
    )
