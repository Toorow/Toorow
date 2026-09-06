"""AI-310 -- one report grain per answer, proven against a real relation.

WHAT WAS BROKEN, AND WHY NO EXISTING TEST SAW IT. `get_youtube_analytics_report`
declared a `report_profile` argument and never passed it anywhere:
`_query_mart(date_from, date_to, project_id)` read the whole mart. Two report
profiles land in `raw_youtube_daily` -- `channel_daily` is one row per day for the
channel, `video_daily` one row per day per video -- so an answer to
"channel_daily" carried both, and summing it gave exactly twice the channel's
views. Measured on production 2026-08-22 (project proj_01KZGCRSV2XACWRP3RSVNWWGBK,
metric `views`): 2026-08-19 channel 390 / videos 390 / naive sum 780, ratio 2.000
on every day of the window.

Nothing caught it because every existing proof looks one layer away from where
the defect lives: the golden pull asserts what the connector RETURNS, the dbt
build asserts what the model BUILDS -- and the module's own seed fixture holds
channel rows only, so even a build with the new column exercises one grain. The
double-count only exists when BOTH grains sit in one relation, which is what this
file constructs and no other test in the repository does.

So the relation here is REAL -- a DuckDB view over rows in the exact shape
`_insert_raw_rows()` lands and `stg_youtube_daily` selects -- and not a mock of
the query. A mocked `_query_mart` would have passed against the broken code.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import duckdb
import pytest

_SERVER_DIR = Path(__file__).parents[3]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

_PROJECT = "proj_ai310"
_DAY = "2026-08-19"

#: The two grains of one day, in the proportion production showed: the channel
#: reports 390 views, and its videos report 390 between them. A reader that sums
#: both answers 780 -- the number the screen showed.
_CHANNEL_VIEWS = 390.0
_VIDEO_VIEWS = (250.0, 140.0)


def _load_connector():
    path = Path(__file__).parents[1] / "connector.py"
    spec = importlib.util.spec_from_file_location("youtube_grain_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def connector(tmp_path, monkeypatch):
    """A connector whose mart is a real relation holding BOTH report grains."""
    db_path = tmp_path / "ai310.duckdb"
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE raw_youtube_daily (
                date VARCHAR, channel_id VARCHAR, video VARCHAR, metric VARCHAR,
                value DOUBLE, pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
            )
            """
        )
        rows = [(_DAY, "UC_AI310", "", "views", _CHANNEL_VIEWS, "pull_01A", "", _PROJECT)]
        rows += [
            (_DAY, "UC_AI310", f"vid_{index}", "views", value, "pull_01A", "", _PROJECT)
            for index, value in enumerate(_VIDEO_VIEWS)
        ]
        con.executemany(
            "INSERT INTO raw_youtube_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows
        )
        # The mart, in the shape `fact_youtube_daily.sql` builds -- `data_level`
        # derived exactly as `stg_youtube_daily.sql` derives it.
        con.execute(
            """
            CREATE VIEW fact_youtube_daily AS
            SELECT project_id, date, channel_id, video,
                   CASE WHEN video IS NULL OR video = '' THEN 'CHANNEL' ELSE 'VIDEO' END
                       AS data_level,
                   metric, value, pull_id, loaded_at
            FROM raw_youtube_daily
            """
        )
    finally:
        con.close()

    module = _load_connector()
    monkeypatch.setattr(module, "_get_db_mode", lambda: "duckdb")
    monkeypatch.setattr(module, "_get_duckdb_path", lambda: str(db_path))
    monkeypatch.setattr(module, "_get_mart_table", lambda *_a, **_kw: "fact_youtube_daily")
    return module


def _views(envelope) -> float:
    return sum(row["value"] for row in envelope["data"]["rows"] if row["metric"] == "views")


def test_channel_daily_answers_the_channel_and_not_its_videos(connector):
    envelope = connector.get_youtube_analytics_report(
        project_id=_PROJECT, report_profile="channel_daily", date_from=_DAY, date_to=_DAY
    )
    assert envelope["meta"]["alerts"] == []
    # 390, not 780. This assertion FAILS on the code before the repair.
    assert _views(envelope) == _CHANNEL_VIEWS
    assert {row["data_level"] for row in envelope["data"]["rows"]} == {"CHANNEL"}


def test_video_daily_answers_the_videos_and_not_the_channel_roll_up(connector):
    envelope = connector.get_youtube_analytics_report(
        project_id=_PROJECT, report_profile="video_daily", date_from=_DAY, date_to=_DAY
    )
    assert _views(envelope) == sum(_VIDEO_VIEWS)
    assert {row["data_level"] for row in envelope["data"]["rows"]} == {"VIDEO"}
    assert {row["video"] for row in envelope["data"]["rows"]} == {"vid_0", "vid_1"}


def test_the_two_answers_are_disjoint_and_together_are_the_whole_relation(connector):
    """Neither grain is dropped, and no row is served twice.

    The repair restricts; a restriction that silently lost rows would be a second
    defect wearing the first one's fix. The two answers must partition the day.
    """
    channel = connector.get_youtube_analytics_report(
        project_id=_PROJECT, report_profile="channel_daily", date_from=_DAY, date_to=_DAY
    )
    video = connector.get_youtube_analytics_report(
        project_id=_PROJECT, report_profile="video_daily", date_from=_DAY, date_to=_DAY
    )
    assert len(channel["data"]["rows"]) + len(video["data"]["rows"]) == 3
    assert _views(channel) + _views(video) == _CHANNEL_VIEWS + sum(_VIDEO_VIEWS)


def test_an_unread_profile_is_refused_by_name_with_the_ones_that_are_read(connector):
    """A profile this report does not read is NOT quietly widened to everything.

    That is the shape the defect had: an unused argument behaves like "any", and
    the caller cannot tell an answer about their question from an answer about
    all of them.
    """
    envelope = connector.get_youtube_analytics_report(
        project_id=_PROJECT, report_profile="audience_geography",
        date_from=_DAY, date_to=_DAY,
    )
    assert envelope["data"]["rows"] == []
    message = envelope["meta"]["alerts"][0]["message"]
    assert "audience_geography" in message
    assert "channel_daily" in message and "video_daily" in message


def test_the_map_names_only_profiles_that_really_land_in_this_relation(connector):
    """The map is the tool's half of a declaration the manifest already makes.

    Pinned to the manifest rather than kept in step by hand: a profile named here
    that does not land in `raw_youtube_daily` would filter a relation it is not in
    and answer an honest-looking empty.

    NOT an equality. `channel_snapshot` lands in the same relation -- its
    declaration said `raw_youtube_breakdown` and that was the wrong address,
    corrected under AI-310 -- but the tool does not answer for it: it is the same
    CHANNEL grain as `channel_daily`, so a grain filter alone cannot separate the
    two. What separates them is their metric set (stocks vs flows), and building
    that is a decision, not a repair. Until it is made, the profile is refused by
    name, which is what the test below holds.
    """
    assert set(connector._DATA_LEVEL_BY_PROFILE) <= _declared_on_the_daily_landing()


def test_the_snapshot_profile_is_refused_by_name_rather_than_answered_wrongly(connector):
    """It shares the landing and the grain -- so it must not be silently served.

    Mapping it to CHANNEL would return `channel_daily`'s rows under its name: two
    profiles, one answer, and a caller with no way to tell.
    """
    assert "channel_snapshot" in _declared_on_the_daily_landing()
    envelope = connector.get_youtube_analytics_report(
        project_id=_PROJECT, report_profile="channel_snapshot",
        date_from=_DAY, date_to=_DAY,
    )
    assert envelope["data"]["rows"] == []
    assert "channel_snapshot" in envelope["meta"]["alerts"][0]["message"]


def _declared_on_the_daily_landing() -> set[str]:
    import json

    manifest = json.loads(
        (Path(__file__).parents[1] / "manifest.json").read_text(encoding="utf-8")
    )
    return {
        profile["id"]
        for profile in manifest.get("report_profiles") or []
        if profile.get("raw_relation") == "raw_youtube_daily"
    }
