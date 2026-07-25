"""Story 26.6 -- pinterest-ads grain-supersede vs descriptive-mutable attributes.

Part A: entity STATE/TYPE dimensions (statuses, objective/budget types) land in
attributes_json (latest-wins, HORS the QUALIFY partition), NOT segments_json (the
supersede grain key). Proof: a refetch that re-reads the SAME window with a
CHANGED status keeps ONE row per grain (metrics not doubled) with the LATEST
attributes -- the exact failure the 26.1 refetch ladder would otherwise make
systematic.

Part B: core/queue.py resolves a selection=None catalog_driven job to the
tier-core default and passes it as an EXPLICIT selection. The module must treat
that resolved default like None (same prune) -- a scheduled default run must
NOT die on the deeper-level pre-call refusals. A genuine user selection stays
never-pruned (typed refusal on incompatibility).

respx-mocked HTTP; fake-clock async socle; no real API / sleeps (AI-13 / 25.6).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
import respx
from core.async_reports import InMemoryReportRefStore

from .conftest import AD_ACCOUNT, API_BASE

_ANALYTICS_URL = f"{API_BASE}/ad_accounts/{AD_ACCOUNT}/campaigns/analytics"
_CAMPAIGNS_URL = f"{API_BASE}/ad_accounts/{AD_ACCOUNT}/campaigns"
_REPORTS_URL = f"{API_BASE}/ad_accounts/{AD_ACCOUNT}/reports"
_DOWNLOAD_URL = "https://pinterest-report-export.test/report.json"

_DAY = (date.today() - timedelta(days=2)).isoformat()

_CAMPAIGN_LIST = {"items": [{"id": "626744128982"}], "bookmark": None}


def _analytics_row(status: str) -> dict:
    """One sync analytics row for the fixed grain, parameterised by status."""
    return {
        "DATE": _DAY,
        "CAMPAIGN_ID": "626744128982",
        "CAMPAIGN_NAME": "Prospecting - FR",
        "CAMPAIGN_ENTITY_STATUS": status,
        "CAMPAIGN_OBJECTIVE_TYPE": "CONVERSIONS",
        "SPEND_IN_MICRO_DOLLAR": 118400000,
        "TOTAL_IMPRESSION": 12040,
        "TOTAL_CLICKTHROUGH": 640,
        "TOTAL_ENGAGEMENT": 810,
        "TOTAL_CONVERSIONS": 42,
        "TOTAL_CHECKOUT": 17,
        "TOTAL_CHECKOUT_VALUE_IN_MICRO_DOLLAR": 3120750000,
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# Part A -- descriptive_mutable marking + attributes_json split.
# ---------------------------------------------------------------------------


def test_descriptive_mutable_landed_names_map_enum_and_canonical(connector):
    """The mutable set is read from catalog_sources and expanded to BOTH the
    enum id AND its canonical landed name (so the split matches whichever the
    row carries)."""
    names = connector._descriptive_mutable_landed_names()
    # canonical landed names (what transform() renames these enum ids to)
    assert "campaign_status" in names        # <- CAMPAIGN_ENTITY_STATUS
    assert "campaign_objective_type" in names
    assert "ad_group_status" in names        # <- AD_GROUP_ENTITY_STATUS
    # enum ids too (a mutable dim without a canonical rename lands under its id)
    assert "AD_STATUS" in names
    assert "PIN_PROMOTION_STATUS" in names
    assert "PRODUCT_GROUP_STATUS" in names
    # the original enum ids of the renamed ones are also covered (belt and
    # suspenders: the split matches whichever key the row carries)
    assert "CAMPAIGN_ENTITY_STATUS" in names
    # grain-bearing ids are NEVER mutable attributes
    assert "campaign_id" not in names
    assert "CAMPAIGN_ID" not in names
    assert "ad_id" not in names


@respx.mock
def test_status_lands_in_attributes_not_segments(connector, tmp_path, monkeypatch):
    """Part A landing: campaign status/objective are entity attributes ->
    attributes_json; segments_json holds only grain-bearing breakdowns (none
    here)."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "pin_attr.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(200, json=_CAMPAIGN_LIST)
    )
    respx.get(_ANALYTICS_URL).mock(
        return_value=httpx.Response(200, json=[_analytics_row("ACTIVE")])
    )
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_campaign_daily(
            connection_id="c", date_from=_DAY, date_to=_DAY, project_id="p",
            pull_id="pull_a", ad_account_id=AD_ACCOUNT,
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    seg, attr = con.execute(
        "SELECT segments_json, attributes_json FROM raw_pinterest_ads_daily"
        " WHERE metric = 'cost' AND pull_id = 'pull_a'"
    ).fetchone()
    con.close()
    assert seg is None  # no grain-bearing dimension in this selection
    attributes = json.loads(attr)
    assert attributes["campaign_status"] == "ACTIVE"
    assert attributes["campaign_objective_type"] == "CONVERSIONS"


@respx.mock
def test_refetch_with_changed_status_does_not_double_count(
    connector, tmp_path, monkeypatch
):
    """THE Part A scenario: re-pull the SAME window with a CHANGED status. The
    dbt staging QUALIFY (segments_json in the grain, attributes_json NOT) keeps
    ONE row per grain -- metrics are NOT doubled and the surviving attributes
    are the LATEST known. Without the split, the ACTIVE and PAUSED rows would
    carry different segments_json and both survive => cost counted 2x.
    """
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "pin_refetch.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(200, json=_CAMPAIGN_LIST)
    )
    analytics = respx.get(_ANALYTICS_URL)

    # Pull 1: status ACTIVE. Pull 2 (later ULID): SAME window, status PAUSED.
    analytics.mock(return_value=httpx.Response(200, json=[_analytics_row("ACTIVE")]))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_campaign_daily(
            connection_id="c", date_from=_DAY, date_to=_DAY, project_id="p",
            pull_id="pull_0001_active", ad_account_id=AD_ACCOUNT,
        )
    analytics.mock(return_value=httpx.Response(200, json=[_analytics_row("PAUSED")]))
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        connector.pull_campaign_daily(
            connection_id="c", date_from=_DAY, date_to=_DAY, project_id="p",
            pull_id="pull_0002_paused", ad_account_id=AD_ACCOUNT,
        )

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    # Two physical rows landed for 'cost' (one per pull), with DIFFERENT
    # attributes_json but IDENTICAL segments_json (both NULL == same grain).
    raw = con.execute(
        "SELECT segments_json, attributes_json, pull_id FROM"
        " raw_pinterest_ads_daily WHERE metric = 'cost' ORDER BY pull_id"
    ).fetchall()
    assert len(raw) == 2
    assert raw[0][0] == raw[1][0]  # segments_json identical (same grain key)
    assert raw[0][1] != raw[1][1]  # attributes_json differs (status flip)

    # Apply the EXACT staging supersede (segments_json in the partition,
    # attributes_json NOT; latest pull_id wins).
    superseded = con.execute(
        """
        SELECT metric, value_num, attributes_json FROM (
            SELECT metric, value_num, attributes_json,
                   ROW_NUMBER() OVER (
                       PARTITION BY project_id, date, data_level, ad_account_id,
                           campaign_id, ad_group_id, ad_id, pin_id,
                           product_group_id, COALESCE(segments_json, ''), metric
                       ORDER BY pull_id DESC
                   ) AS rn
            FROM raw_pinterest_ads_daily
            WHERE metric = 'cost'
        ) WHERE rn = 1
        """
    ).fetchall()
    con.close()

    # ONE surviving row, cost NOT doubled, attributes = the LATEST (PAUSED).
    assert len(superseded) == 1
    assert superseded[0][1] == pytest.approx(118.4)  # not 236.8
    assert json.loads(superseded[0][2])["campaign_status"] == "PAUSED"


# ---------------------------------------------------------------------------
# Part B -- recognise the core-resolved tier-core default (amazon-ads pattern).
# ---------------------------------------------------------------------------


def _poll_response(status: str, url: str | None = None) -> httpx.Response:
    payload: dict = {"report_status": status, "token": "tok123"}
    if url is not None:
        payload["url"] = url
    return httpx.Response(200, json=payload)


@respx.mock
def test_queue_resolved_default_is_pruned_like_none(
    connector, tmp_path, monkeypatch
):
    """Part B: the queue resolves selection=None to the tier-core default and
    passes it EXPLICITLY. That resolved default must submit a valid report (the
    deeper-level dims pruned to the CAMPAIGN report shape), exactly like the
    selection=None path -- NOT a pre-call refusal."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "pin_qb.duckdb"))

    resolved_default = connector._resolved_core_default_selection()
    assert connector._is_resolved_core_default(resolved_default) is True

    create = respx.post(_REPORTS_URL).mock(
        return_value=httpx.Response(
            200, json={"report_status": "IN_PROGRESS", "token": "tok123"}
        )
    )
    respx.get(_REPORTS_URL).mock(
        return_value=_poll_response("FINISHED", _DOWNLOAD_URL)
    )
    respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, json=[]))

    clock = FakeClock()
    store = InMemoryReportRefStore(clock=clock)
    with patch("core.nango_client.get_fresh_token", return_value="tok"):
        result = connector.pull_catalog_daily(
            connection_id="c", date_from=_DAY, date_to=_DAY, project_id="p",
            pull_id="pull_qb", selection=resolved_default,
            ad_account_id=AD_ACCOUNT, report_store=store, clock=clock,
            sleeper=clock.sleep, deadline_seconds=300,
        )
    assert result["status"] == "completed"

    body = json.loads(create.calls.last.request.content)
    columns = set(body["columns"])
    # Same pruning as None: campaign-lineage dims kept, deeper-level dims gone.
    assert {"CAMPAIGN_ID", "CAMPAIGN_NAME"} <= columns
    for pruned in ("AD_GROUP_ID", "AD_ID", "PIN_ID", "PIN_PROMOTION_ID"):
        assert pruned not in columns, pruned


@respx.mock
def test_none_and_resolved_default_produce_identical_report(
    connector, tmp_path, monkeypatch
):
    """The None path and the queue-resolved-default path must build the SAME
    report spec (same prune, same request hash) -- proof they share one code
    path."""
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "pin_qb2.duckdb"))

    def _run(selection):
        create = respx.post(_REPORTS_URL).mock(
            return_value=httpx.Response(
                200, json={"report_status": "IN_PROGRESS", "token": "tok123"}
            )
        )
        respx.get(_REPORTS_URL).mock(
            return_value=_poll_response("FINISHED", _DOWNLOAD_URL)
        )
        respx.get(_DOWNLOAD_URL).mock(return_value=httpx.Response(200, json=[]))
        clock = FakeClock()
        store = InMemoryReportRefStore(clock=clock)
        with patch("core.nango_client.get_fresh_token", return_value="tok"):
            out = connector.pull_catalog_daily(
                connection_id="c", date_from=_DAY, date_to=_DAY, project_id="p",
                pull_id="pull_cmp", selection=selection,
                ad_account_id=AD_ACCOUNT, report_store=store, clock=clock,
                sleeper=clock.sleep, deadline_seconds=300,
            )
        return out["request_hash"], sorted(
            json.loads(create.calls.last.request.content)["columns"]
        )

    hash_none, cols_none = _run(None)
    respx.calls.reset()
    hash_default, cols_default = _run(connector._resolved_core_default_selection())

    assert cols_none == cols_default
    assert hash_none == hash_default


def test_real_user_selection_is_never_pruned(connector):
    """A genuine user selection that is NOT the resolved default stays a typed
    refusal on incompatibility -- never routed through the prune path."""
    from core.pull_errors import InvalidRequestError

    # AD_ID is not selectable at level CAMPAIGN: a user asking for it gets a
    # typed refusal (never silently pruned).
    user_selection = {
        "metrics": ["SPEND_IN_MICRO_DOLLAR"],
        "dimensions": ["date", "CAMPAIGN_ID", "AD_ID"],
    }
    assert connector._is_resolved_core_default(user_selection) is False
    with pytest.raises(InvalidRequestError) as exc:
        connector.pull_catalog_daily(
            connection_id="c", date_from=_DAY, date_to=_DAY, project_id="p",
            pull_id="pull_user", selection=user_selection,
            ad_account_id=AD_ACCOUNT, level="CAMPAIGN",
        )
    assert "AD_ID" in str(exc.value)
