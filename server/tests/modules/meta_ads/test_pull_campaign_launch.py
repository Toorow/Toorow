"""Tests for the Meta Ads MIXED connector event path (Epic 31.6).

meta-ads becomes a MIXED connector: kpi profiles (campaign/adset/creative/catalog
daily -> fact_daily_kpi) AND an event profile (campaign_launch -> context_events).
This module covers the event path, generalising the YouTube 31.3 reference:

  (a) transform_events(golden_events) == expected_events -- the pure canonical
      event-mapping contract (symmetric to golden_pull/expected_facts), plus the
      H1 date-window filter and the M1 invalid-start_time skip.
  (b) pull_campaign_launch dispatch: httpx (Graph /act_<id>/campaigns, paged via
      paging.next) mocked via respx; asserts the exact kwargs handed to
      persist_context_event (canonical campaign_launch type, event_date, label,
      platform='meta', source='meta-ads', value=None) and that idempotence
      (delete-by-source-window) runs before the inserts (H1).

No test contacts the real API (respx) or a real DB (persist/delete mocked).
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
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "meta-ads"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_FIXTURES_DIR = _MODULE_DIR / "tests" / "fixtures"

_ACCOUNT = "123456789"
_CAMPAIGNS_URL = f"https://graph.facebook.com/v20.0/act_{_ACCOUNT}/campaigns"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_meta_events", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


def _load_fixture(name: str):
    return json.loads((_FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# (a) transform_events() -- pure canonical event mapping
# ---------------------------------------------------------------------------


def test_transform_events_matches_expected_events(connector):
    """transform_events(golden_events) == expected_events (golden replay)."""
    golden = _load_fixture("golden_events.json")
    expected = _load_fixture("expected_events.json")

    assert connector.transform_events(golden) == expected


def test_transform_events_date_window_filters_out_of_range(connector):
    """H1: campaigns outside [date_from, date_to] are dropped (unbounded edge)."""
    golden = _load_fixture("golden_events.json")  # dates 2026-07-01 and 2026-07-15

    windowed = connector.transform_events(
        golden, date_from="2026-07-01", date_to="2026-07-10"
    )
    assert [e["event_date"] for e in windowed] == ["2026-07-01"]

    none_in = connector.transform_events(
        golden, date_from="2026-08-01", date_to="2026-08-31"
    )
    assert none_in == []


def test_transform_events_falls_back_to_created_time(connector):
    """A campaign with no start_time uses created_time; none at all is skipped."""
    rows = [
        {"name": "no start_time", "created_time": "2026-07-02T08:00:00+0000"},
        {"name": "no dates at all"},
        {"name": "too short", "start_time": "2026-0"},
        {"name": "valid", "start_time": "2026-07-05T00:00:00+0000"},
    ]
    out = connector.transform_events(rows)
    assert [(e["event_date"], e["label"]) for e in out] == [
        ("2026-07-02", "no start_time"),
        ("2026-07-05", "valid"),
    ]


def test_transform_events_stamps_canonical_identity(connector):
    """Every event carries the canonical platform/source/type stamps (AD-2)."""
    for ev in connector.transform_events(_load_fixture("golden_events.json")):
        assert ev["event_type"] == "campaign_launch"
        assert ev["platform"] == "meta"
        assert ev["source"] == "meta-ads"


# ---------------------------------------------------------------------------
# (b) pull_campaign_launch() -- Graph campaigns dispatch + persist kwargs
# ---------------------------------------------------------------------------


def _campaigns_payload() -> dict:
    """Two campaigns, no paging.next (single page)."""
    return {
        "data": [
            {
                "id": "23851234567890123",
                "name": "Summer Launch — Prospecting",
                "start_time": "2026-07-01T09:00:00+0000",
                "created_time": "2026-06-28T14:12:00+0000",
                "status": "ACTIVE",
            },
            {
                "id": "23851234567890124",
                "name": "Retargeting — Q3",
                "start_time": "2026-07-15T08:30:00+0000",
                "created_time": "2026-07-14T11:00:00+0000",
                "status": "ACTIVE",
            },
        ]
    }


@respx.mock
def test_pull_campaign_launch_persists_canonical_events(connector):
    """pull_campaign_launch -> persist_context_event with canonical kwargs."""
    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(200, json=_campaigns_payload())
    )

    persisted: list[dict] = []
    deleted: list[dict] = []

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with patch(
            "core.context_events.persist_context_event",
            side_effect=lambda **kw: persisted.append(kw) or "evt_stub",
        ):
            with patch(
                "core.context_events.delete_connector_events_in_window",
                side_effect=lambda **kw: deleted.append(kw) or 0,
            ):
                result = connector.pull_campaign_launch(
                    connection_id="conn_test",
                    date_from="2026-07-01",
                    date_to="2026-07-31",
                    project_id="proj-test",
                    pull_id="pull_meta_events_001",
                    ad_account_id=_ACCOUNT,
                )

    assert result["event_count"] == 2
    assert len(persisted) == 2

    # H1: delete-by-source-window ran once, scoped to project/source/type/window.
    assert len(deleted) == 1
    d = deleted[0]
    assert d["project_id"] == "proj-test"
    assert d["source"] == "meta-ads"
    assert d["event_type"] == "campaign_launch"
    assert d["date_from"] == "2026-07-01"
    assert d["date_to"] == "2026-07-31"

    first = persisted[0]
    assert first["type"] == "campaign_launch"
    assert first["event_date"] == "2026-07-01"
    assert first["label"] == "Summer Launch — Prospecting"
    assert first["platform"] == "meta"
    assert first["source"] == "meta-ads"
    assert first["value"] is None
    assert first["project_id"] == "proj-test"
    assert first["created_by"] == "meta-ads_pull:pull_meta_events_001"


@respx.mock
def test_pull_campaign_launch_accepts_act_prefix(connector):
    """ad_account_id given as 'act_<digits>' is normalised to the same edge."""
    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(200, json=_campaigns_payload())
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with patch(
            "core.context_events.persist_context_event", return_value="evt_stub"
        ):
            with patch(
                "core.context_events.delete_connector_events_in_window", return_value=0
            ):
                result = connector.pull_campaign_launch(
                    connection_id="conn_test",
                    date_from="2026-07-01",
                    date_to="2026-07-31",
                    project_id="proj-test",
                    pull_id="pull_meta_events_002",
                    ad_account_id=f"act_{_ACCOUNT}",
                )

    assert result["event_count"] == 2


@respx.mock
def test_pull_campaign_launch_applies_date_window(connector):
    """H1: campaigns are filtered to the requested window at pull time."""
    respx.get(_CAMPAIGNS_URL).mock(
        return_value=httpx.Response(200, json=_campaigns_payload())
    )

    persisted: list[dict] = []

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with patch(
            "core.context_events.persist_context_event",
            side_effect=lambda **kw: persisted.append(kw) or "evt_stub",
        ):
            with patch(
                "core.context_events.delete_connector_events_in_window", return_value=0
            ):
                result = connector.pull_campaign_launch(
                    connection_id="conn_test",
                    date_from="2026-07-01",
                    date_to="2026-07-10",  # excludes the 2026-07-15 campaign
                    project_id="proj-test",
                    pull_id="pull_meta_events_003",
                    ad_account_id=_ACCOUNT,
                )

    assert result["event_count"] == 1
    assert [p["event_date"] for p in persisted] == ["2026-07-01"]
