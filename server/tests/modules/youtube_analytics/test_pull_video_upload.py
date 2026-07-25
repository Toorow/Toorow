"""Tests for the YouTube Analytics MIXED connector event path (Epic 31.3, C2).

youtube-analytics is the reference MIXED connector: it has kpi profiles
(channel_daily/video_daily -> fact_daily_kpi) AND an event profile
(video_upload -> context_events). This module covers the event path, which had
no test before Epic 31.3:

  (a) transform_events(golden_events) == expected_events  -- the pure canonical
      event-mapping contract (symmetric to the golden_pull/expected_facts test),
      plus the H1 date-window filter and the M1 invalid-publishedAt skip.
  (b) pull_video_upload dispatch: httpx (Data API v3 channels.list + the uploads
      playlistItems.list) mocked via respx; asserts the exact kwargs handed to
      persist_context_event (canonical video_upload type, event_date, label,
      platform='youtube', source='youtube-analytics', value=None) and that
      idempotence (delete-by-source-window) runs before the inserts (H1).

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

_MODULE_DIR = (
    Path(__file__).parents[4] / "server" / "modules" / "youtube-analytics"
)
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_FIXTURES_DIR = _MODULE_DIR / "tests" / "fixtures"

_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_PLAYLIST_URL = "https://www.googleapis.com/youtube/v3/playlistItems"

_UPLOADS_PLAYLIST_ID = "UUxxxxxxxxxxxxxxxxxxxxxx"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_yt_events", _CONNECTOR_PATH)
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

    actual = connector.transform_events(golden)

    assert actual == expected


def test_transform_events_date_window_filters_out_of_range(connector):
    """H1: items outside [date_from, date_to] are dropped (unbounded playlist)."""
    golden = _load_fixture("golden_events.json")  # dates 2026-07-01 and 2026-07-15

    # Window that excludes the 2026-07-15 upload.
    windowed = connector.transform_events(
        golden, date_from="2026-07-01", date_to="2026-07-10"
    )
    assert [e["event_date"] for e in windowed] == ["2026-07-01"]

    # Window excluding both -> empty.
    none_in = connector.transform_events(
        golden, date_from="2026-08-01", date_to="2026-08-31"
    )
    assert none_in == []


def test_transform_events_skips_invalid_published_at(connector):
    """M1: an item with no valid publishedAt (>=10 chars) is skipped, not emitted."""
    rows = [
        {"snippet": {"publishedAt": "", "title": "no date"}},
        {"snippet": {"title": "missing publishedAt key"}},
        {"snippet": {"publishedAt": "2026-0", "title": "too short"}},
        {"snippet": {"publishedAt": "2026-07-01T10:00:00Z", "title": "valid"}},
    ]
    out = connector.transform_events(rows)
    assert len(out) == 1
    assert out[0]["event_date"] == "2026-07-01"
    assert out[0]["label"] == "valid"


def test_transform_events_stamps_canonical_identity(connector):
    """Every event carries the canonical platform/source/type stamps (AD-2)."""
    golden = _load_fixture("golden_events.json")
    for ev in connector.transform_events(golden):
        assert ev["event_type"] == "video_upload"
        assert ev["platform"] == "youtube"
        assert ev["source"] == "youtube-analytics"


# ---------------------------------------------------------------------------
# (b) pull_video_upload() -- Data API v3 dispatch + persist kwargs
# ---------------------------------------------------------------------------


def _channels_payload() -> dict:
    return {
        "items": [
            {
                "contentDetails": {
                    "relatedPlaylists": {"uploads": _UPLOADS_PLAYLIST_ID}
                }
            }
        ]
    }


def _playlist_payload() -> dict:
    """Two uploads, no nextPageToken (single page)."""
    return {
        "items": [
            {
                "snippet": {
                    "publishedAt": "2026-07-01T10:00:00Z",
                    "title": "Toorow launch highlight",
                    "resourceId": {"kind": "youtube#video", "videoId": "dQw4w9WgXcQ"},
                }
            },
            {
                "snippet": {
                    "publishedAt": "2026-07-15T14:30:00Z",
                    "title": "Behind the scenes -- toorow connector",
                    "resourceId": {"kind": "youtube#video", "videoId": "xvFZjo5PgG0"},
                }
            },
        ]
    }


@respx.mock
def test_pull_video_upload_dispatch_persists_canonical_events(connector):
    """pull_video_upload -> persist_context_event with canonical kwargs (C2)."""
    respx.get(_CHANNELS_URL).mock(
        return_value=httpx.Response(200, json=_channels_payload())
    )
    respx.get(_PLAYLIST_URL).mock(
        return_value=httpx.Response(200, json=_playlist_payload())
    )

    persisted: list[dict] = []
    deleted: list[dict] = []

    def _capture_persist(**kwargs) -> str:
        persisted.append(kwargs)
        return "evt_stub"

    def _capture_delete(**kwargs) -> int:
        deleted.append(kwargs)
        return 0

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with patch(
            "core.context_events.persist_context_event", side_effect=_capture_persist
        ):
            with patch(
                "core.context_events.delete_connector_events_in_window",
                side_effect=_capture_delete,
            ):
                result = connector.pull_video_upload(
                    connection_id="conn_test",
                    date_from="2026-07-01",
                    date_to="2026-07-31",
                    project_id="proj-test",
                    pull_id="pull_yt_events_001",
                )

    # Both uploads are inside the window -> 2 persisted events.
    assert result["event_count"] == 2
    assert len(persisted) == 2

    # H1: delete-by-source-window ran ONCE, before inserts, scoped to this
    # project/source/type/window (idempotence).
    assert len(deleted) == 1
    d = deleted[0]
    assert d["project_id"] == "proj-test"
    assert d["source"] == "youtube-analytics"
    assert d["event_type"] == "video_upload"
    assert d["date_from"] == "2026-07-01"
    assert d["date_to"] == "2026-07-31"

    # First event kwargs -- canonical identity (AD-2), value=None pulse (AD-9).
    first = persisted[0]
    assert first["type"] == "video_upload"
    assert first["event_date"] == "2026-07-01"
    assert first["label"] == "Toorow launch highlight"
    assert first["platform"] == "youtube"
    assert first["source"] == "youtube-analytics"
    assert first["value"] is None
    assert first["project_id"] == "proj-test"
    # description carries the watch URL for traceability.
    assert first["description"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    second = persisted[1]
    assert second["event_date"] == "2026-07-15"
    assert second["label"] == "Behind the scenes -- toorow connector"
    assert second["value"] is None


@respx.mock
def test_pull_video_upload_applies_date_window(connector):
    """H1: the unbounded playlist is filtered to the requested window at pull time."""
    respx.get(_CHANNELS_URL).mock(
        return_value=httpx.Response(200, json=_channels_payload())
    )
    respx.get(_PLAYLIST_URL).mock(
        return_value=httpx.Response(200, json=_playlist_payload())
    )

    persisted: list[dict] = []

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with patch(
            "core.context_events.persist_context_event",
            side_effect=lambda **kw: persisted.append(kw) or "evt_stub",
        ):
            with patch(
                "core.context_events.delete_connector_events_in_window",
                return_value=0,
            ):
                result = connector.pull_video_upload(
                    connection_id="conn_test",
                    date_from="2026-07-01",
                    date_to="2026-07-10",  # excludes the 2026-07-15 upload
                    project_id="proj-test",
                    pull_id="pull_yt_events_002",
                )

    assert result["event_count"] == 1
    assert [p["event_date"] for p in persisted] == ["2026-07-01"]


@respx.mock
def test_pull_video_upload_no_channel_returns_zero(connector):
    """No channel found -> 0 events, no persist call (graceful, AD-9)."""
    respx.get(_CHANNELS_URL).mock(return_value=httpx.Response(200, json={"items": []}))

    with patch("core.nango_client.get_fresh_token", return_value="fake-token"):
        with patch(
            "core.context_events.persist_context_event",
            side_effect=AssertionError("must not persist when no channel"),
        ):
            with patch(
                "core.context_events.delete_connector_events_in_window",
                side_effect=AssertionError("must not delete when no channel"),
            ):
                result = connector.pull_video_upload(
                    connection_id="conn_test",
                    date_from="2026-07-01",
                    date_to="2026-07-31",
                    project_id="proj-test",
                    pull_id="pull_yt_events_003",
                )

    assert result["event_count"] == 0
