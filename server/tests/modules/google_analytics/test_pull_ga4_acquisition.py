"""Tests for the GA4 acquisition pulls -- Epic 16, story 16.1.

Covers pull_acquisition_daily_session (last click) and
pull_acquisition_daily_first_user (first click). Uses respx to mock the GA4 Data API.
No test contacts the real API. Live GA4 confirmation of the dimension names is BLOCKED
Phase B (AI-13 / AI-53).

Assertions (AC2 / AC7):
  * request body dimensions lead with 'date' (manifest contract -- without it every row
    lands with date='' and the window collapses, review-10-6 F-1);
  * orderBys conversions DESC + limit=top_n (top-N bound);
  * multi-date rows land with DISTINCT dates (never '');
  * (not set) -> unknown, (direct) -> direct mapping applied;
  * dispatch AI-58: get_module_pull_fn resolves both shims (not pull()).

ASCII-only (AI-03).
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
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_TOOROW_PATH = (
    Path(__file__).parents[4]
    / "server"
    / "modules"
    / "google-analytics"
    / "connector.py"
)

_RUNREPORT_URL = (
    "https://analyticsdata.googleapis.com/v1beta/properties/TEST123:runReport"
)


def _import_connector():
    """Dynamically import connector.py from the hyphenated module directory."""
    spec = importlib.util.spec_from_file_location(
        "connector_ga4_acquisition", _TOOROW_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# GA4 last-click response: 3 rows over 2 distinct dates, incl. (not set) + (direct).
_SESSION_RESPONSE = {
    "dimensionHeaders": [
        {"name": "date"},
        {"name": "sessionSourceMedium"},
        {"name": "sessionCampaignName"},
    ],
    "metricHeaders": [
        {"name": "conversions", "type": "TYPE_INTEGER"},
        {"name": "sessions", "type": "TYPE_INTEGER"},
    ],
    "rows": [
        {
            "dimensionValues": [
                {"value": "20260701"},
                {"value": "google / cpc"},
                {"value": "brand-search"},
            ],
            "metricValues": [{"value": "20"}, {"value": "440"}],
        },
        {
            "dimensionValues": [
                {"value": "20260701"},
                {"value": "(direct)"},
                {"value": "(not set)"},
            ],
            "metricValues": [{"value": "8"}, {"value": "150"}],
        },
        {
            "dimensionValues": [
                {"value": "20260702"},
                {"value": "facebook / cpc"},
                {"value": "retargeting"},
            ],
            "metricValues": [{"value": "5"}, {"value": "90"}],
        },
    ],
}

# GA4 first-click response: 2 rows over 2 distinct dates, incl. (not set).
_FIRST_USER_RESPONSE = {
    "dimensionHeaders": [
        {"name": "date"},
        {"name": "firstUserSourceMedium"},
    ],
    "metricHeaders": [{"name": "conversions", "type": "TYPE_INTEGER"}],
    "rows": [
        {
            "dimensionValues": [{"value": "20260701"}, {"value": "google / organic"}],
            "metricValues": [{"value": "30"}],
        },
        {
            "dimensionValues": [{"value": "20260702"}, {"value": "(not set)"}],
            "metricValues": [{"value": "4"}],
        },
    ],
}


@respx.mock
def test_pull_acquisition_session_lands_multi_date_rows(connector, tmp_path, monkeypatch):
    """Last-click pull: dims lead with date, top-N orderBys, multi-date rows, mapping."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "acq_session.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    route = respx.post(_RUNREPORT_URL).mock(
        return_value=httpx.Response(200, json=_SESSION_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_acquisition_daily_session(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-ga4",
            pull_id="pull_acq_sess",
        )

    assert route.called
    body = json.loads(route.calls.last.request.read())
    # date MUST lead the dimensions (manifest contract / review-10-6 F-1).
    assert [d["name"] for d in body["dimensions"]] == [
        "date",
        "sessionSourceMedium",
        "sessionCampaignName",
    ]
    assert [m["name"] for m in body["metrics"]] == ["conversions", "sessions"]
    # Top-N bound: orderBys conversions DESC + limit.
    assert body["orderBys"] == [
        {"metric": {"metricName": "conversions"}, "desc": True}
    ]
    assert int(body["limit"]) == connector._get_page_top_n()

    assert result["pull_id"] == "pull_acq_sess"
    assert result["row_count"] == 3

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    landed = con.execute(
        "SELECT DISTINCT date FROM raw_ga4_acquisition_session "
        "WHERE pull_id = 'pull_acq_sess' ORDER BY date"
    ).fetchall()
    # (not set) -> unknown, (direct) -> direct mapping applied.
    mapped = con.execute(
        "SELECT session_source_medium, session_campaign FROM raw_ga4_acquisition_session "
        "WHERE pull_id = 'pull_acq_sess' AND date = '2026-07-01' "
        "AND session_source_medium = 'direct'"
    ).fetchall()
    con.close()

    # Distinct per-day dates landed, never '' (daily grain preserved end-to-end).
    assert [d[0] for d in landed] == ["2026-07-01", "2026-07-02"]
    assert mapped == [("direct", "unknown")], mapped


@respx.mock
def test_pull_acquisition_first_user_lands_multi_date_rows(connector, tmp_path, monkeypatch):
    """First-click pull: dims lead with date, top-N orderBys, multi-date rows, mapping."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "acq_fu.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    route = respx.post(_RUNREPORT_URL).mock(
        return_value=httpx.Response(200, json=_FIRST_USER_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_acquisition_daily_first_user(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-ga4",
            pull_id="pull_acq_fu",
        )

    assert route.called
    body = json.loads(route.calls.last.request.read())
    assert [d["name"] for d in body["dimensions"]] == [
        "date",
        "firstUserSourceMedium",
    ]
    assert [m["name"] for m in body["metrics"]] == ["conversions"]
    assert body["orderBys"] == [
        {"metric": {"metricName": "conversions"}, "desc": True}
    ]
    assert int(body["limit"]) == connector._get_page_top_n()

    assert result["pull_id"] == "pull_acq_fu"
    assert result["row_count"] == 2

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    landed = con.execute(
        "SELECT DISTINCT date FROM raw_ga4_acquisition_first_user "
        "WHERE pull_id = 'pull_acq_fu' ORDER BY date"
    ).fetchall()
    unknown = con.execute(
        "SELECT first_user_source_medium FROM raw_ga4_acquisition_first_user "
        "WHERE pull_id = 'pull_acq_fu' AND date = '2026-07-02'"
    ).fetchall()
    con.close()

    assert [d[0] for d in landed] == ["2026-07-01", "2026-07-02"]
    assert unknown == [("unknown",)], unknown


def test_pull_acquisition_session_requires_property_id(connector, monkeypatch):
    """Shim raises a clear ValueError when neither arg nor GA4_PROPERTY_ID is set."""
    monkeypatch.delenv("GA4_PROPERTY_ID", raising=False)
    with pytest.raises(ValueError, match="GA4_PROPERTY_ID"):
        connector.pull_acquisition_daily_session(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-ga4",
            pull_id="pull_acq_noprop",
        )


def test_pull_acquisition_first_user_requires_property_id(connector, monkeypatch):
    monkeypatch.delenv("GA4_PROPERTY_ID", raising=False)
    with pytest.raises(ValueError, match="GA4_PROPERTY_ID"):
        connector.pull_acquisition_daily_first_user(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-ga4",
            pull_id="pull_acq_fu_noprop",
        )


# ---------------------------------------------------------------------------
# AI-58 dispatch: get_module_pull_fn resolves the two acquisition shims, not pull().
# ---------------------------------------------------------------------------


def test_acquisition_dispatch_resolves_profile_fns():
    """AI-58: get_module_pull_fn('google-analytics', '<profile>') resolves each shim.

    The profile-aware dispatch keys off the ``pull_<profile_id>`` naming contract. This
    guards that both shims exist and are picked up (fell through to pull() otherwise).
    """
    import types

    from core.main import get_module_pull_fn

    ga4_mod = _import_connector()
    assert callable(getattr(ga4_mod, "pull_acquisition_daily_session", None))
    assert callable(getattr(ga4_mod, "pull_acquisition_daily_first_user", None))

    loaded = types.SimpleNamespace(
        name="google-analytics",
        connector_module=ga4_mod,
        manifest=json.loads(
            (_TOOROW_PATH.parent / "manifest.json").read_text(encoding="utf-8")
        ),
    )
    with patch("core.main._loaded_modules", [loaded]):
        default_fn = get_module_pull_fn("google-analytics")
        session_fn = get_module_pull_fn(
            "google-analytics", profile_id="acquisition_daily_session"
        )
        first_user_fn = get_module_pull_fn(
            "google-analytics", profile_id="acquisition_daily_first_user"
        )

    assert default_fn is ga4_mod.pull
    assert session_fn is ga4_mod.pull_acquisition_daily_session
    assert first_user_fn is ga4_mod.pull_acquisition_daily_first_user
    assert session_fn is not default_fn
    assert first_user_fn is not default_fn
    assert session_fn is not first_user_fn
