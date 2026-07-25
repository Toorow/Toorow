"""Tests for the GBP MIXED connector event path (Epic 31.6, live-deferred).

google-business-profile becomes a MIXED connector: a kpi profile (location_daily
-> fact_daily_kpi) AND an event profile (social_post -> context_events). Local
posts live on the LEGACY v4 host, which is allowlist-gated (0-QPM by default), so
this profile is transform-proven / live-deferred: the pure mapping is fully
tested here; the live pull is ratified once the v4 host is granted.

  (a) transform_events(golden_events) == expected_events -- the pure canonical
      event-mapping contract, plus the H1 window filter and the M1 invalid-
      createTime skip.
  (b) pull_social_post dispatch: httpx (v4 localPosts) mocked via respx; asserts
      the exact kwargs handed to persist_context_event (canonical social_post
      type, event_date, label, platform='gbp', source='google-business-profile',
      value=None) and that idempotence (delete-by-source-window) runs before the
      inserts (H1).

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

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "google-business-profile"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_FIXTURES_DIR = _MODULE_DIR / "tests" / "fixtures"

_PARENT = "accounts/1111/locations/2222"
_LOCALPOSTS_URL = f"https://mybusiness.googleapis.com/v4/{_PARENT}/localPosts"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_gbp_events", _CONNECTOR_PATH)
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
    """H1: posts outside [date_from, date_to] are dropped."""
    golden = _load_fixture("golden_events.json")  # 2026-07-04 and 2026-07-20

    windowed = connector.transform_events(
        golden, date_from="2026-07-01", date_to="2026-07-10"
    )
    assert [e["event_date"] for e in windowed] == ["2026-07-04"]

    assert connector.transform_events(
        golden, date_from="2026-08-01", date_to="2026-08-31"
    ) == []


def test_transform_events_skips_invalid_create_time(connector):
    """M1: a post with no valid createTime (>=10 chars) is skipped, not emitted."""
    rows = [
        {"summary": "no createTime"},
        {"summary": "empty", "createTime": ""},
        {"summary": "too short", "createTime": "2026-0"},
        {"summary": "valid", "createTime": "2026-07-04T09:00:00Z"},
    ]
    out = connector.transform_events(rows)
    assert len(out) == 1
    assert out[0]["event_date"] == "2026-07-04"
    assert out[0]["label"] == "valid"


def test_transform_events_stamps_canonical_identity(connector):
    """Every event carries the canonical platform/source/type stamps (AD-2)."""
    for ev in connector.transform_events(_load_fixture("golden_events.json")):
        assert ev["event_type"] == "social_post"
        assert ev["platform"] == "gbp"
        assert ev["source"] == "google-business-profile"


# ---------------------------------------------------------------------------
# (b) pull_social_post() -- v4 localPosts dispatch + persist kwargs
# ---------------------------------------------------------------------------


def _localposts_payload() -> dict:
    """Two posts, no nextPageToken (single page)."""
    return {"localPosts": _load_fixture("golden_events.json")}


@respx.mock
def test_pull_social_post_persists_canonical_events(connector):
    """pull_social_post -> persist_context_event with canonical kwargs.

    location_id is passed as the full accounts/*/locations/* parent so account
    resolution is a no-op (verbatim) and no discover_accounts call is needed.
    """
    respx.get(_LOCALPOSTS_URL).mock(
        return_value=httpx.Response(200, json=_localposts_payload())
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
                result = connector.pull_social_post(
                    connection_id="conn_test",
                    date_from="2026-07-01",
                    date_to="2026-07-31",
                    project_id="proj-test",
                    pull_id="pull_gbp_events_001",
                    location_id=_PARENT,
                )

    assert result["event_count"] == 2
    assert len(persisted) == 2

    assert len(deleted) == 1
    d = deleted[0]
    assert d["project_id"] == "proj-test"
    assert d["source"] == "google-business-profile"
    assert d["event_type"] == "social_post"
    assert d["date_from"] == "2026-07-01"
    assert d["date_to"] == "2026-07-31"

    first = persisted[0]
    assert first["type"] == "social_post"
    assert first["event_date"] == "2026-07-04"
    assert first["label"] == "Now open Sundays — come visit our downtown store!"
    assert first["platform"] == "gbp"
    assert first["source"] == "google-business-profile"
    assert first["value"] is None
    assert first["project_id"] == "proj-test"
    assert first["created_by"] == "google-business-profile_pull:pull_gbp_events_001"


@respx.mock
def test_pull_social_post_resolves_account_from_topology(connector):
    """A bare locations/<id> resolves its owning account via discover_accounts."""
    respx.get(_LOCALPOSTS_URL).mock(
        return_value=httpx.Response(200, json=_localposts_payload())
    )

    fake_topology = [
        {"id": "locations/2222", "label": "Downtown", "parent": "accounts/1111"}
    ]

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with patch.object(connector, "discover_accounts", return_value=fake_topology):
            with patch(
                "core.context_events.persist_context_event", return_value="evt_stub"
            ):
                with patch(
                    "core.context_events.delete_connector_events_in_window",
                    return_value=0,
                ):
                    result = connector.pull_social_post(
                        connection_id="conn_test",
                        date_from="2026-07-01",
                        date_to="2026-07-31",
                        project_id="proj-test",
                        pull_id="pull_gbp_events_002",
                        location_id="locations/2222",
                    )

    assert result["event_count"] == 2


def test_pull_social_post_requires_location_id(connector):
    """No location_id -> ValueError (topology selection required, no env fallback)."""
    with pytest.raises(ValueError, match="requires a selected location_id"):
        connector.pull_social_post(
            connection_id="conn_test",
            date_from="2026-07-01",
            date_to="2026-07-31",
            project_id="proj-test",
            pull_id="pull_gbp_events_003",
            location_id=None,
        )
