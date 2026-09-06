"""The Competitors outbound path of youtube-analytics (2026-09-01).

Two profiles driven by the tracked-entity binding parameter ``channel_ids``:

  * ``competitor_channel_snapshot`` -- channels.list stocks for bound PUBLIC
    channels, landing the same long rows as ``channel_snapshot``;
  * ``channel_video_directory`` -- the 50 most recent uploads of each bound
    channel with title, publication date, duration and public lifetime views,
    landing ``raw_youtube_video_directory``.

Plus the latent defect this wave fixed: ``_METRIC_IDS`` skipped every STOCK
metric, so ``pull_channel_snapshot`` reported success while landing ZERO rows.

No test contacts the real API (respx) and none needs a warehouse: landings are
asserted through dry_run rows or the values handed to the landing helper.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "youtube-analytics"
_CONNECTOR_PATH = _MODULE_DIR / "connector.py"

_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
_PLAYLIST_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

CH_OWN = "UCownxxxxxxxxxxxxxxxxxxx"
CH_RIVAL = "UCrivalxxxxxxxxxxxxxxxxx"


def _import_connector():
    spec = importlib.util.spec_from_file_location("connector_yt_tracked", _CONNECTOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def connector():
    return _import_connector()


@pytest.fixture(autouse=True)
def _token(connector):
    with patch("core.nango_client.get_fresh_token", return_value="tok_EXAMPLE"):
        yield


def _channels_payload(*ids_stats):
    return {"items": [
        {"id": cid,
         "snippet": {"title": f"Channel {cid[-5:]}"},
         "statistics": {"subscriberCount": str(subs), "viewCount": str(views),
                        "videoCount": str(vids)},
         "contentDetails": {"relatedPlaylists": {"uploads": f"UU{cid[2:]}"}}}
        for (cid, subs, views, vids) in ids_stats
    ]}


# ---------------------------------------------------------------------------
# competitor_channel_snapshot
# ---------------------------------------------------------------------------


@respx.mock
def test_competitor_snapshot_reads_the_bound_ids_and_stamps_the_reading_day(connector):
    route = respx.get(_CHANNELS_URL).mock(
        return_value=httpx.Response(200, json=_channels_payload(
            (CH_OWN, 1000, 500000, 570), (CH_RIVAL, 20000, 4000000, 300)))
    )

    out = connector.pull_competitor_channel_snapshot(
        "conn_EXAMPLE", "2026-08-30", "2026-08-31", "proj_EXAMPLE", "pull_EXAMPLE",
        channel_ids=[CH_OWN, CH_RIVAL], own_channel_ids=[CH_OWN], dry_run=True,
    )

    assert route.called
    assert route.calls[0].request.url.params["id"] == f"{CH_OWN},{CH_RIVAL}"
    assert out["row_count"] == 2
    by_id = {r["channel_id"]: r for r in out["rows"]}
    assert by_id[CH_RIVAL]["subscriber_count"] == "20000"
    assert by_id[CH_RIVAL]["lifetime_view_count"] == "4000000"
    assert all(r["date"] == "2026-08-31" for r in out["rows"]), (
        "a stock is stamped on the day the reading was taken"
    )


def test_competitor_snapshot_with_no_bound_ids_lands_zero_rows(connector):
    out = connector.pull_competitor_channel_snapshot(
        "conn_EXAMPLE", "2026-08-30", "2026-08-31", "proj_EXAMPLE", "pull_EXAMPLE",
        channel_ids=[],
    )
    assert out == {"pull_id": "pull_EXAMPLE", "row_count": 0,
                   "date_from": "2026-08-30", "date_to": "2026-08-31"}


# ---------------------------------------------------------------------------
# the latent zero-row landing of every stock metric
# ---------------------------------------------------------------------------


def test_stock_metrics_actually_land_instead_of_being_skipped(connector):
    """`transform` renames subscriberCount -> subscriber_count; the landing loop
    then iterated a list that did not contain it, so the row count was 0 and the
    pull still said success. The stocks are in `_METRIC_IDS` now, and this pins
    the unpivot: one canonical row must land its three stock values."""
    landed = []
    with patch.object(connector, "_get_db_mode", return_value="duckdb"), \
         patch("core.warehouse_write.open_raw_writer") as writer:
        con = writer.return_value
        con.executemany.side_effect = lambda _sql, values: landed.extend(values)
        count = connector._insert_raw_rows(
            [{"date": "2026-08-31", "channel_id": CH_RIVAL,
              "subscriber_count": "20000", "lifetime_view_count": "4000000",
              "video_count": "300"}],
            "pull_EXAMPLE", "proj_EXAMPLE",
        )
    metrics = {v[3] for v in landed}
    assert count == 3, f"three stock metrics must land, landed={landed}"
    assert metrics == {"subscriber_count", "lifetime_view_count", "video_count"}


# ---------------------------------------------------------------------------
# channel_video_directory
# ---------------------------------------------------------------------------


def _directory_routes(connector):
    respx.get(_CHANNELS_URL).mock(
        return_value=httpx.Response(200, json=_channels_payload(
            (CH_RIVAL, 20000, 4000000, 300)))
    )
    respx.get(_PLAYLIST_URL).mock(
        return_value=httpx.Response(200, json={"items": [
            {"contentDetails": {"videoId": "vidAAAAAAAA"}},
            {"contentDetails": {"videoId": "vidBBBBBBBB"}},
        ]})
    )
    respx.get(_VIDEOS_URL).mock(
        return_value=httpx.Response(200, json={"items": [
            {"id": "vidAAAAAAAA",
             "snippet": {"title": "Recette exemple", "publishedAt": "2026-08-28T17:00:00Z"},
             "statistics": {"viewCount": "4142"},
             "contentDetails": {"duration": "PT12M34S"}},
            {"id": "vidBBBBBBBB",
             "snippet": {"title": "Short exemple", "publishedAt": "2026-08-20T10:00:00Z"},
             "statistics": {"viewCount": "755"},
             "contentDetails": {"duration": "PT59S"}},
        ]})
    )


@respx.mock
def test_directory_names_dates_and_measures_each_upload(connector):
    _directory_routes(connector)

    out = connector.pull_channel_video_directory(
        "conn_EXAMPLE", "2026-08-30", "2026-08-31", "proj_EXAMPLE", "pull_EXAMPLE",
        channel_ids=[CH_RIVAL], own_channel_ids=[], dry_run=True,
    )

    assert out["row_count"] == 2
    row = next(r for r in out["rows"] if r["video"] == "vidAAAAAAAA")
    assert row["video_title"] == "Recette exemple"
    assert row["published_at"] == "2026-08-28"
    assert row["duration_seconds"] == 754.0
    assert row["lifetime_views"] == "4142"
    assert row["is_own_channel"] is False
    assert row["channel_title"].startswith("Channel ")
    assert row["date"] == "2026-08-31", "a directory row is a dated snapshot"
    assert out["unreachable_channel_ids"] == []


@respx.mock
def test_directory_stamps_the_own_channel_marker(connector):
    _directory_routes(connector)

    out = connector.pull_channel_video_directory(
        "conn_EXAMPLE", "2026-08-30", "2026-08-31", "proj_EXAMPLE", "pull_EXAMPLE",
        channel_ids=[CH_RIVAL], own_channel_ids=[CH_RIVAL], dry_run=True,
    )

    assert all(r["is_own_channel"] is True for r in out["rows"])


def test_directory_with_no_bound_ids_lands_zero_rows(connector):
    out = connector.pull_channel_video_directory(
        "conn_EXAMPLE", "2026-08-30", "2026-08-31", "proj_EXAMPLE", "pull_EXAMPLE",
    )
    assert out["row_count"] == 0


def test_duration_parser_refuses_what_it_cannot_read(connector):
    assert connector._iso8601_duration_seconds("PT1H2M3S") == 3723.0
    assert connector._iso8601_duration_seconds("PT59S") == 59.0
    assert connector._iso8601_duration_seconds("P1DT1S") == 86401.0
    assert connector._iso8601_duration_seconds("") is None
    assert connector._iso8601_duration_seconds(None) is None
    assert connector._iso8601_duration_seconds("not-a-duration") is None, (
        "AD-9: an unreadable duration is NULL, never a fake zero"
    )
