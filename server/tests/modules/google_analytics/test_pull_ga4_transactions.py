"""Tests for the GA4 transactions pull -- Epic 17, story 17.4.

Covers pull_transactions_daily (GA4 transactionId e-commerce). Uses respx to mock the
GA4 Data API. No test contacts the real API. Live GA4 confirmation of the transactionId
dimension + purchaseRevenue metric is BLOCKED Phase B (AI-13 / AI-53).

Assertions:
  * request body dimensions lead with 'date' (manifest contract -- without it every row
    lands with date='' and the window collapses, review-10-6 F-1);
  * dimensions = [date, transactionId], metrics = [conversions, purchaseRevenue];
  * multi-date rows land with DISTINCT dates (never '');
  * '(not set)' / '' transaction id -> NULL (join key: never a fake match bucket);
  * PAGINATION: a day with MORE rows than one page pages on offset to completeness (the
    join-key denominator is never silently truncated -- the point dur of the story);
  * dispatch AI-58: get_module_pull_fn resolves the shim (not pull()).

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
        "connector_ga4_transactions", _TOOROW_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


# GA4 transactions response: 3 rows over 2 distinct dates, incl. a (not set) id.
_TXN_RESPONSE = {
    "dimensionHeaders": [
        {"name": "date"},
        {"name": "transactionId"},
    ],
    "metricHeaders": [
        {"name": "conversions", "type": "TYPE_INTEGER"},
        {"name": "purchaseRevenue", "type": "TYPE_FLOAT"},
    ],
    "rows": [
        {
            "dimensionValues": [{"value": "20260701"}, {"value": "7000000021"}],
            "metricValues": [{"value": "1"}, {"value": "120.50"}],
        },
        {
            "dimensionValues": [{"value": "20260701"}, {"value": "(not set)"}],
            "metricValues": [{"value": "1"}, {"value": "45.00"}],
        },
        {
            "dimensionValues": [{"value": "20260702"}, {"value": "7000000042"}],
            "metricValues": [{"value": "1"}, {"value": "88.90"}],
        },
    ],
}


@respx.mock
def test_pull_transactions_lands_multi_date_rows(connector, tmp_path, monkeypatch):
    """Transactions pull: dims lead with date, transactionId + purchaseRevenue, multi-date,
    (not set) -> NULL join key."""
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    db_path = str(tmp_path / "txn.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    route = respx.post(_RUNREPORT_URL).mock(
        return_value=httpx.Response(200, json=_TXN_RESPONSE)
    )

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_transactions_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-ga4",
            pull_id="pull_txn",
        )

    assert route.called
    body = json.loads(route.calls.last.request.read())
    # date MUST lead the dimensions (manifest contract / review-10-6 F-1).
    assert [d["name"] for d in body["dimensions"]] == ["date", "transactionId"]
    assert [m["name"] for m in body["metrics"]] == ["conversions", "purchaseRevenue"]
    # Deterministic paging: order by the join key.
    assert body["orderBys"] == [
        {"dimension": {"dimensionName": "transactionId"}}
    ]

    assert result["pull_id"] == "pull_txn"
    assert result["row_count"] == 3
    assert result["truncated"] is False

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    landed = con.execute(
        "SELECT DISTINCT date FROM raw_ga4_transactions_daily "
        "WHERE pull_id = 'pull_txn' ORDER BY date"
    ).fetchall()
    # (not set) -> NULL join key (never the string '(not set)' -- would be a fake match).
    null_ids = con.execute(
        "SELECT COUNT(*) FROM raw_ga4_transactions_daily "
        "WHERE pull_id = 'pull_txn' AND transaction_id IS NULL"
    ).fetchone()[0]
    fake_bucket = con.execute(
        "SELECT COUNT(*) FROM raw_ga4_transactions_daily "
        "WHERE pull_id = 'pull_txn' AND transaction_id = '(not set)'"
    ).fetchone()[0]
    con.close()

    # Distinct per-day dates landed, never '' (daily grain preserved end-to-end).
    assert [d[0] for d in landed] == ["2026-07-01", "2026-07-02"]
    assert null_ids == 1, "the (not set) id must land as NULL"
    assert fake_bucket == 0, "'(not set)' must NEVER survive as a literal id (fake match)"


@respx.mock
def test_pull_transactions_pages_to_completeness(connector, tmp_path, monkeypatch):
    """PAGINATION point dur: a day with MORE transactions than one page pages on offset to
    completeness -- the join-key denominator is never silently truncated.

    We set GA4_TXN_LIMIT=2 so a 3-row day forces a SECOND request (offset=2). The mock
    returns a full page (2 rows) then a partial page (1 row) -> loop stops.
    """
    monkeypatch.setenv("GA4_PROPERTY_ID", "TEST123")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("GA4_TXN_LIMIT", "50")  # will be clamped to min 50; override below
    db_path = str(tmp_path / "txn_page.duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    # Force a tiny page size by monkeypatching the resolver (env min-clamp is 50).
    monkeypatch.setattr(connector, "_get_txn_limit", lambda: 2)

    page1 = {
        "rows": [
            {
                "dimensionValues": [{"value": "20260701"}, {"value": "A1"}],
                "metricValues": [{"value": "1"}, {"value": "10.0"}],
            },
            {
                "dimensionValues": [{"value": "20260701"}, {"value": "A2"}],
                "metricValues": [{"value": "1"}, {"value": "20.0"}],
            },
        ]
    }
    page2 = {
        "rows": [
            {
                "dimensionValues": [{"value": "20260701"}, {"value": "A3"}],
                "metricValues": [{"value": "1"}, {"value": "30.0"}],
            },
        ]
    }
    responses = [
        httpx.Response(200, json=page1),
        httpx.Response(200, json=page2),
    ]
    route = respx.post(_RUNREPORT_URL).mock(side_effect=responses)

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        result = connector.pull_transactions_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-01",
            project_id="jean-ga4",
            pull_id="pull_txn_page",
        )

    assert route.call_count == 2, "a full page must trigger a second (offset) request"
    # Second request carries offset=2.
    body2 = json.loads(route.calls[1].request.read())
    assert int(body2["offset"]) == 2
    assert int(body2["limit"]) == 2

    assert result["row_count"] == 3, "all 3 transactions landed (no silent truncation)"
    assert result["pages"] == 2
    assert result["truncated"] is False

    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    ids = con.execute(
        "SELECT transaction_id FROM raw_ga4_transactions_daily "
        "WHERE pull_id = 'pull_txn_page' ORDER BY transaction_id"
    ).fetchall()
    con.close()
    assert [r[0] for r in ids] == ["A1", "A2", "A3"]


def test_pull_transactions_requires_property_id(connector, monkeypatch):
    """Shim raises a clear ValueError when neither arg nor GA4_PROPERTY_ID is set."""
    monkeypatch.delenv("GA4_PROPERTY_ID", raising=False)
    with pytest.raises(ValueError, match="GA4_PROPERTY_ID"):
        connector.pull_transactions_daily(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-03",
            project_id="jean-ga4",
            pull_id="pull_txn_noprop",
        )


# ---------------------------------------------------------------------------
# AI-58 dispatch: get_module_pull_fn resolves the transactions shim, not pull().
# ---------------------------------------------------------------------------


def test_transactions_dispatch_resolves_profile_fn():
    """AI-58: get_module_pull_fn('google-analytics', 'transactions_daily') resolves the shim.

    The profile-aware dispatch keys off the ``pull_<profile_id>`` naming contract. This
    guards that the shim exists and is picked up (fell through to pull() otherwise).
    """
    import types

    from core.main import get_module_pull_fn

    ga4_mod = _import_connector()
    assert callable(getattr(ga4_mod, "pull_transactions_daily", None))

    loaded = types.SimpleNamespace(
        name="google-analytics",
        connector_module=ga4_mod,
        manifest=json.loads(
            (_TOOROW_PATH.parent / "manifest.json").read_text(encoding="utf-8")
        ),
    )
    with patch("core.main._loaded_modules", [loaded]):
        default_fn = get_module_pull_fn("google-analytics")
        txn_fn = get_module_pull_fn(
            "google-analytics", profile_id="transactions_daily"
        )

    assert default_fn is ga4_mod.pull
    assert txn_fn is ga4_mod.pull_transactions_daily
    assert txn_fn is not default_fn
