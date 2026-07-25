from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "x-ads"


@pytest.fixture()
def connector():
    spec = importlib.util.spec_from_file_location("connector_x_ads", MODULE_DIR / "connector.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Response:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self.payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self.payload


def test_oauth1_manifest_and_v12_are_honest():
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text())
    assert manifest["auth_type"] == "oauth1"
    assert manifest["auth"]["request_signing"] == "nango_proxy"
    assert manifest["provider_api_version"] == "ads-api-v12"


def test_discovery_preserves_timezone_currency(connector):
    proxy = MagicMock(
        return_value=Response(
            payload={
                "data": [{"id": "a1", "name": "A", "timezone": "Europe/Paris", "currency": "EUR"}]
            }
        )
    )
    row = connector.discover_accounts("c", _proxy=proxy)[0]
    assert row["timezone"] == "Europe/Paris"
    assert row["currency"] == "EUR"
    assert proxy.call_args.kwargs["base_url_override"] == "https://ads-api.x.com"


def test_empty_discovery_is_typed(connector):
    with pytest.raises(connector.XAdsOnboardingError):
        connector.discover_accounts("c", _proxy=MagicMock(return_value=Response(payload={})))


def test_illegal_segmentation_fails_before_call(connector):
    with pytest.raises(connector.XAdsCompatibilityError):
        connector.build_stats_request(
            "campaign_daily", ["impressions"], "2026-01-01", "2026-01-02", segmentation="AGE"
        )


def test_window_bounds(connector):
    assert len(connector.split_date_windows("2026-01-01", "2026-04-01", 90)) == 2
    assert len(connector.split_date_windows("2026-01-01", "2026-04-01", 45)) == 3
    with pytest.raises(connector.XAdsCompatibilityError):
        connector.sync_stats(
            "c",
            "a",
            {"start_time": "2026-01-01T00:00:00Z", "end_time": "2026-01-08T23:59:59Z"},
            _proxy=MagicMock(),
        )


def test_sync_disallows_segmentation(connector):
    request = connector.build_stats_request(
        "segmented_daily", ["impressions"], "2026-01-01", "2026-01-02", segmentation="AGE"
    )
    with pytest.raises(connector.XAdsCompatibilityError):
        connector.sync_stats("c", "a", request, _proxy=MagicMock())


def test_async_states_and_processing_cap(connector):
    proxy = MagicMock(return_value=Response(payload={"data": {"status": "PROCESSING"}}))
    flow = connector._AsyncStatsFlow("c", "a", {}, proxy)
    assert flow.poll("j") == "processing"
    proxy = MagicMock(return_value=Response(payload={"data": [{"id": str(i)} for i in range(100)]}))
    flow = connector._AsyncStatsFlow("c", "a", {}, proxy)
    with pytest.raises(Exception, match="100 processing jobs"):
        flow.submit()


def test_async_resume_avoids_duplicate_submit(connector):
    from core.async_reports import InMemoryReportRefStore

    proxy = MagicMock(
        side_effect=[
            Response(payload={"data": []}),
            Response(payload={"data": {"id": "j1"}}),
            Response(payload={"data": {"status": "PROCESSING"}}),
            Response(payload={"data": {"status": "PROCESSING"}}),
            Response(payload={"data": {"status": "SUCCESS"}}),
            Response(payload={"data": {"result": [{"id": "e1", "metrics": {"impressions": "1"}}]}}),
        ]
    )
    now = [0.0]
    store = InMemoryReportRefStore(clock=lambda: now[0])

    def sleep(s):
        now[0] += s

    first = connector.run_async_stats(
        "c",
        "a",
        {"shape": 1},
        _proxy=proxy,
        store=store,
        clock=lambda: now[0],
        sleeper=sleep,
        deadline_seconds=1,
    )
    second = connector.run_async_stats(
        "c",
        "a",
        {"shape": 1},
        _proxy=proxy,
        store=store,
        clock=lambda: now[0],
        sleeper=sleep,
        deadline_seconds=60,
    )
    assert first["status"] == "deferred" and second["status"] == "completed"
    assert len([c for c in proxy.call_args_list if c.args[2] == "POST"]) == 1


def test_retry_headers_are_respected(connector):
    from core.quota import RateLimitError

    reset = str(int(datetime.now(UTC).timestamp()) + 30)
    with pytest.raises(RateLimitError) as raised:
        connector._raise_response(Response(429, headers={"x-rate-limit-reset": reset}))
    assert raised.value.retry_after == 30
    with pytest.raises(RateLimitError) as raised:
        connector._raise_response(Response(503, headers={"retry-after": "7"}))
    assert raised.value.retry_after == 7


def test_catalog_zero_planned_and_refetch(connector):
    catalog = json.loads((MODULE_DIR / "api_catalog.json").read_text())
    assert len(catalog["fields"]) == 16
    assert not [f for f in catalog["fields"] if f["exposure"] == "planned"]
    assert connector.REFETCH_DAYS == (3, 14)


def test_prefer_sync_routes_short_window_to_sync_path(connector, tmp_path, monkeypatch):
    """M-1: prefer_sync=True on a ≤7-day non-segmented window uses sync_stats, not async.

    This test explicitly exercises the escape hatch so its behavior is locked.
    prefer_sync is not a public API contract — it is for tests/dev only (see ROLLOUT_NOTES.md).
    """
    sync_rows = [{"id": "e1", "metrics": {"impressions": "10"}}]

    # Track which code path was taken
    calls = {"sync": 0, "async": 0}

    def fake_sync(connection_id, account_id, request, *, _proxy=None):
        calls["sync"] += 1
        return sync_rows

    def fake_async(connection_id, account_id, request, *, _proxy=None, **kw):
        calls["async"] += 1
        return {"status": "completed", "rows": sync_rows}

    # Route DuckDB to a temp file so _land does not write to the module seeds dir
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "test.duckdb"))
    monkeypatch.setattr(connector, "sync_stats", fake_sync)
    monkeypatch.setattr(connector, "run_async_stats", fake_async)

    result = connector._pull_profile(
        "conn-test",
        "2026-01-01",
        "2026-01-03",  # 3 days — within SYNC_MAX_DAYS=7
        "proj-1",
        "pull-1",
        "campaign_daily",
        {"account_id": "acc1", "prefer_sync": True},
    )
    # prefer_sync=True must have taken the sync branch, not async
    assert calls["sync"] == 1, "prefer_sync=True should route to sync_stats"
    assert calls["async"] == 0, "prefer_sync=True should NOT call run_async_stats"
    assert result["row_count"] >= 0


def test_prefer_sync_false_takes_async_path(connector, tmp_path, monkeypatch):
    """M-1 complement: without prefer_sync, even a ≤7-day window goes through async."""
    sync_rows = [{"id": "e1", "metrics": {"impressions": "5"}}]
    calls = {"sync": 0, "async": 0}

    def fake_sync(connection_id, account_id, request, *, _proxy=None):
        calls["sync"] += 1
        return sync_rows

    def fake_async(connection_id, account_id, request, *, _proxy=None, **kw):
        calls["async"] += 1
        return {"status": "completed", "rows": sync_rows}

    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "test2.duckdb"))
    monkeypatch.setattr(connector, "sync_stats", fake_sync)
    monkeypatch.setattr(connector, "run_async_stats", fake_async)

    connector._pull_profile(
        "conn-test",
        "2026-01-01",
        "2026-01-03",
        "proj-1",
        "pull-2",
        "campaign_daily",
        {"account_id": "acc1"},  # no prefer_sync → defaults to async
    )
    assert calls["async"] == 1, "Without prefer_sync, short window must still use async"
    assert calls["sync"] == 0


def test_prefer_sync_absent_from_manifest_profiles():
    """M-1: no report_profile or production fixture should include prefer_sync."""
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text())
    for profile in manifest.get("report_profiles", []):
        assert "prefer_sync" not in profile, (
            f"prefer_sync must not appear in manifest profile '{profile['id']}'"
        )
    for report in manifest.get("source_capabilities", {}).get("reports", []):
        assert "prefer_sync" not in report, (
            f"prefer_sync must not appear in source_capabilities report '{report['id']}'"
        )
