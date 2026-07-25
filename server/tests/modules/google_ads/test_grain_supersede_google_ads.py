"""Story 26.6 -- grain de supersede vs attributs mutables (Part A) + core-
resolved default recognition (Part B), applied to google-ads.

Part A: descriptive-mutable status/type enums (campaign_status, ...) land in a
SEPARATE attributes_json column, OUTSIDE segments_json and the dbt supersede
grain. A re-pull of the SAME window with a CHANGED status (ENABLED -> PAUSED)
must leave ONE row per grain after the QUALIFY supersede -- metrics NOT doubled,
attributes = latest-wins. The staging QUALIFY is rendered from the committed
stg_google_ads_daily.sql (jinja stripped) and run against the DuckDB the
connector actually wrote, so the test exercises the REAL partition.

Part B: core/queue.py resolves selection=None to the tier-core default and
passes it as an EXPLICIT selection; the module must recognise it and prune it
the same way it prunes None (a valid GAQL, no refusal). A genuine user selection
stays never-pruned.
"""

from __future__ import annotations

import json
import re
from unittest.mock import patch

import duckdb
import httpx
import pytest
import respx

from .conftest import API_BASE, MODULE_DIR

_CID = "9861234567"
_SEARCH_URL = f"{API_BASE}/customers/{_CID}/googleAds:search"

_STAGING_SQL = (
    MODULE_DIR / "dbt" / "staging" / "stg_google_ads_daily.sql"
)


def _campaign_row(status: str) -> dict:
    """One campaign_daily result row with a given (mutable) campaign.status."""
    return {
        "customer": {"id": _CID},
        "campaign": {
            "id": "22334455",
            "name": "Brand - Search - FR",
            "status": status,
            "advertisingChannelType": "SEARCH",
            "biddingStrategyType": "TARGET_ROAS",
        },
        "metrics": {
            "costMicros": "118400000",
            "impressions": "12040",
            "clicks": "640",
            "conversions": 42.5,
            "conversionsValue": 3120.75,
            "allConversions": 48.0,
            "viewThroughConversions": "5",
        },
        "segments": {"date": "2026-07-01"},
    }


def _response(status: str) -> dict:
    return {"results": [_campaign_row(status)]}


def _render_staging(view_name: str, raw_table: str) -> str:
    """Render the committed staging model to runnable DuckDB SQL.

    Strips the dbt jinja ({{ config(...) }}, {{ source(...) }}) and wraps the
    SELECT in a CREATE VIEW so the test exercises the ACTUAL committed QUALIFY
    partition -- not a hand-copied one that could drift.
    """
    sql = _STAGING_SQL.read_text(encoding="utf-8")
    sql = re.sub(r"\{\{\s*config\([^}]*\)\s*\}\}", "", sql)
    sql = re.sub(
        r"\{\{\s*source\([^}]*\)\s*\}\}", raw_table, sql
    )
    return f"CREATE VIEW {view_name} AS\n{sql}"


# ---------------------------------------------------------------------------
# Part A -- landing split + re-pull-status-changed supersede.
# ---------------------------------------------------------------------------


@respx.mock
def test_mutable_status_lands_in_attributes_not_segments(
    connector, tmp_path, monkeypatch
):
    """campaign_status/type enums land in attributes_json; segments_json is NULL
    (no row-bearing segment selected). Grain columns keep the ids."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gads_attr.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.post(_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_response("ENABLED"))
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_campaign_daily(
            connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
            project_id="p", pull_id="pull_gads_attr1", customer_id=_CID,
        )

    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(
        "SELECT segments_json, attributes_json, campaign_id "
        "FROM raw_google_ads_daily WHERE pull_id = 'pull_gads_attr1' LIMIT 1"
    ).fetchall()
    con.close()
    segments_json, attributes_json, campaign_id = rows[0]
    # The mutable status/type enums are OUT of segments_json...
    assert segments_json is None
    # ...and IN attributes_json (latest-wins), with campaign_name too.
    attrs = json.loads(attributes_json)
    assert attrs["campaign_status"] == "ENABLED"
    assert attrs["campaign_advertising_channel_type"] == "SEARCH"
    assert attrs["campaign_bidding_strategy_type"] == "TARGET_ROAS"
    # Grain id stays a dedicated column.
    assert campaign_id == "22334455"


@respx.mock
def test_repull_with_changed_status_does_not_double_count(
    connector, tmp_path, monkeypatch
):
    """THE 26.6 scenario: re-pull the SAME window (2026-07-01) with the campaign
    now PAUSED. After the dbt supersede QUALIFY, ONE row per grain x metric
    survives (metrics NOT doubled) and attributes_json = the LATEST status."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "gads_repull.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    # First pull: ENABLED. Second pull (later pull_id): PAUSED, same window.
    route = respx.post(_SEARCH_URL)
    route.side_effect = [
        httpx.Response(200, json=_response("ENABLED")),
        httpx.Response(200, json=_response("PAUSED")),
    ]
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_campaign_daily(
            connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
            project_id="p", pull_id="pull_gads_0001_enabled", customer_id=_CID,
        )
        connector.pull_campaign_daily(
            connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
            project_id="p", pull_id="pull_gads_0002_paused", customer_id=_CID,
        )

    con = duckdb.connect(db_path)
    # Two physical raw rows per metric (append-only, AD-7): 7 metrics x 2 pulls.
    raw_cost = con.execute(
        "SELECT COUNT(*) FROM raw_google_ads_daily WHERE metric='cost'"
    ).fetchone()[0]
    assert raw_cost == 2

    # Render the REAL staging supersede and materialise it.
    con.execute(_render_staging("stg", "raw_google_ads_daily"))

    # ONE row per grain x metric survives the QUALIFY (grain unchanged by the
    # status change -- attributes_json is NOT in the partition).
    cost_rows = con.execute(
        "SELECT value_num, attributes_json, pull_id FROM stg WHERE metric='cost'"
    ).fetchall()
    con.close()
    assert len(cost_rows) == 1, "status change must NOT fork the grain"
    value_num, attributes_json, pull_id = cost_rows[0]
    # Metric NOT doubled (still one day's cost, not 2x).
    assert value_num == pytest.approx(118.4)
    # Latest-wins: the surviving row is the newest pull carrying PAUSED.
    assert pull_id == "pull_gads_0002_paused"
    assert json.loads(attributes_json)["campaign_status"] == "PAUSED"


def test_staging_partition_excludes_attributes_json(connector):
    """Guard: the committed QUALIFY partition NEVER lists attributes_json."""
    sql = _STAGING_SQL.read_text(encoding="utf-8")
    partition = sql[sql.index("PARTITION BY"): sql.index("ORDER BY")]
    assert "attributes_json" not in partition
    # But the model still SELECTs the column (queryable downstream, latest-wins).
    assert "attributes_json" in sql.split("PARTITION BY")[0]


def test_catalog_declares_mutable_attrs(connector):
    """The catalog (official_fields.json) is the SOURCE of the split -- the
    connector reads descriptive_mutable, never a name/section heuristic."""
    names = connector._descriptive_mutable_landed_names()
    for expected in (
        "campaign_status", "campaign_advertising_channel_type",
        "campaign_bidding_strategy_type", "ad_group_status", "ad_group_type",
        "ad_group_ad_status", "ad_type", "ad_approval_status", "keyword_status",
        "search_term_status",
    ):
        assert expected in names, f"{expected} must be descriptive_mutable"
    # Grain ids/names and row-bearing segments are NOT mutable attributes.
    for not_mutable in ("campaign_id", "campaign_name", "device", "date",
                        "keyword_match_type"):
        assert not_mutable not in names


# ---------------------------------------------------------------------------
# Part B -- recognise the core-resolved default.
# ---------------------------------------------------------------------------

_CATALOG_RESPONSE = {
    "results": [
        {
            "customer": {"id": _CID},
            "campaign": {"id": "22334455", "name": "Brand - Search - FR"},
            "metrics": {"costMicros": "118400000", "clicks": "640"},
            "segments": {"date": "2026-07-01"},
        }
    ]
}


@respx.mock
def test_queue_resolved_default_is_pruned_like_none(
    connector, tmp_path, monkeypatch
):
    """Story 26.6 Part B: core/queue.py resolves selection=None to the tier-core
    default and passes it EXPLICITLY. Passing that exact resolved default builds
    a valid GAQL (no refusal) IDENTICALLY to passing None -- the grain-exploding
    TIME/row-restricting/non-numeric fields are pruned the same way."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gads_qdef.duckdb"))

    route = respx.post(_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_CATALOG_RESPONSE)
    )
    # This is EXACTLY what core/queue.py hands the module in place of None.
    resolved_default = connector._resolved_core_default_selection()
    assert connector._is_resolved_core_default(resolved_default) is True

    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
            project_id="p", pull_id="pull_gads_qdef", selection=resolved_default,
            customer_id=_CID,
        )
    query = json.loads(route.calls.last.request.content)["query"]
    # Same pruning as the None path: grain-exploding fields never reach the GAQL.
    for banned in (
        "segments.hour", "segments.day_of_week", "segments.week",
        "segments.click_type", "segments.keyword",
        "metrics.interaction_event_types",
    ):
        assert banned not in query, f"resolved default must prune {banned}"
    # Tier-core additive metrics DO survive.
    assert "metrics.cost_micros" in query
    assert "metrics.clicks" in query


@respx.mock
def test_none_and_resolved_default_build_identical_gaql(
    connector, tmp_path, monkeypatch
):
    """The None path and the resolved-default path must produce the SAME GAQL
    (Part B closes the divergence that killed scheduled default pulls)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "gads_eq.duckdb"))

    route = respx.post(_SEARCH_URL).mock(
        return_value=httpx.Response(200, json=_CATALOG_RESPONSE)
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_catalog_daily(
            connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
            project_id="p", pull_id="pull_gads_eq_none", selection=None,
            customer_id=_CID,
        )
        none_query = json.loads(route.calls.last.request.content)["query"]
        connector.pull_catalog_daily(
            connection_id="c", date_from="2026-07-01", date_to="2026-07-01",
            project_id="p", pull_id="pull_gads_eq_def", customer_id=_CID,
            selection=connector._resolved_core_default_selection(),
        )
        default_query = json.loads(route.calls.last.request.content)["query"]
    assert none_query == default_query


def test_genuine_user_selection_is_never_pruned(connector, catalog):
    """A real user selection with a grain-exploding segment is NOT silently
    pruned -- it stays a typed refusal (never treated as the prunable default)."""
    from core.catalog_contract import validate_selection as core_validate

    resolved, issues = core_validate(
        catalog, {"metrics": ["cost_micros"], "dimensions": ["date", "click_type"]}
    )
    assert issues == []
    # Different field membership than the default -> not the prunable default.
    assert connector._is_resolved_core_default(resolved) is False
