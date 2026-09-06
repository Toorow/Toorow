"""The unresolved set: three reasons, a ranking, and an extract that round-trips.

The fixture is the deployment's own case, shrunk. On 2026-08-12 a YouTube
performance flux carried 519 distinct video ids over four weeks and the flux that
names those videos held 3 rows. Here: five videos observed, one of them named, one
blank, one with a confirmed pair -- so all three reasons are exercised on one
relation, which is what the console will show.
"""

from __future__ import annotations

from datetime import date

import pytest

PROJECT = "proj_EXAMPLE"
WINDOW = date(2026, 8, 6)


@pytest.fixture
def warehouse_file(tmp_path, monkeypatch):
    import duckdb

    path = tmp_path / "raw.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE main.raw_youtube_daily ("
        "  project_id VARCHAR, date VARCHAR, channel_id VARCHAR,"
        "  video VARCHAR, views INTEGER)"
    )
    con.executemany(
        "INSERT INTO main.raw_youtube_daily VALUES (?, ?, ?, ?, ?)",
        [
            # Heaviest first is the order the reading must answer in, so the rows
            # are inserted in the WRONG order on purpose.
            (PROJECT, "2026-08-06", "chan_a", "vid_light", 5),
            (PROJECT, "2026-08-06", "chan_a", "vid_heavy", 10),
            (PROJECT, "2026-08-06", "chan_a", "vid_heavy", 11),
            (PROJECT, "2026-08-06", "chan_a", "vid_heavy", 12),
            (PROJECT, "2026-08-06", "chan_a", "vid_named", 7),
            (PROJECT, "2026-08-06", "chan_a", "vid_named", 8),
            (PROJECT, "2026-08-06", "chan_a", "vid_pair", 6),
            (PROJECT, "2026-08-06", "chan_a", "", 1),
            # Another project, same day: never read (AD-5).
            ("proj_OTHER", "2026-08-06", "chan_z", "vid_other", 99),
            # The day before: outside the window.
            (PROJECT, "2026-08-05", "chan_a", "vid_old", 1),
        ],
    )
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    return path


#: The three videos the reference set knows -- the deployment's 3 of 519.
KNOWN = {"vid_named"}
#: The one value a client pair already resolves.
PAIRS = {"vid_pair": "Recette du lundi"}


def _resolver(value, _connector):
    return PAIRS.get(str(value))


def _reference(value, _connector):
    return str(value) in KNOWN


# ---------------------------------------------------------------------------
# The classification itself, offline.
# ---------------------------------------------------------------------------


def test_an_empty_value_is_absent_whatever_its_spelling():
    from core.unresolved_values import REASON_ABSENT_AT_SOURCE, classify, is_absent

    assert is_absent(None) and is_absent("") and is_absent("   ")
    assert is_absent("__country_absent__", ("__country_absent__",))
    assert not is_absent("vid_heavy")
    # Absence wins over everything: a pair that "resolved" an empty string must
    # never make it read as fine.
    assert classify("", resolved=True, in_reference=True) == REASON_ABSENT_AT_SOURCE


def test_the_three_reasons_are_told_apart():
    from core.unresolved_values import (
        REASON_NO_REFERENCE,
        REASON_UNMAPPED,
        classify,
    )

    assert classify("v", resolved=False, in_reference=None) == REASON_UNMAPPED
    assert classify("v", resolved=False, in_reference=False) == REASON_NO_REFERENCE
    # The reference NAMES it: that is the 3 videos of 519 that had a title.
    assert classify("v", resolved=False, in_reference=True) is None
    # A confirmed pair is the person's own answer, and it outranks a set that
    # does not hold the value: they already said what it is called.
    assert classify("v", resolved=True, in_reference=False) is None
    # Tri-state: "no reference consulted" is silence, never a finding.
    assert classify("v", resolved=True, in_reference=None) is None


# ---------------------------------------------------------------------------
# The reading, on a real DuckDB file.
# ---------------------------------------------------------------------------


def test_the_set_is_measured_ranked_and_typed(warehouse_file):
    from core.collected_mapped_reader import ZONE_COLLECTED
    from core.unresolved_values import read_unresolved_set

    answer = read_unresolved_set(
        project_id=PROJECT,
        relation="raw_youtube_daily",
        zone=ZONE_COLLECTED,
        dimension="video",
        start=WINDOW.isoformat(),
        end=WINDOW.isoformat(),
        classifications={"video": "none"},
        resolver=_resolver,
        reference=_reference,
    )
    assert answer["state"] == "measured"
    # Five distinct values on the day, including the blank one.
    assert answer["summary"]["observed_distinct"] == 5
    # Heaviest first; the video the reference names and the one a pair names are
    # simply not in the list -- both questions are already answered.
    assert [row["source_value"] for row in answer["values"]] == [
        "vid_heavy",
        "",
        "vid_light",
    ]
    reasons = {row["source_value"]: row["reason"] for row in answer["values"]}
    assert reasons["vid_heavy"] == "no_reference"
    assert reasons["vid_light"] == "no_reference"
    assert reasons[""] == "absent_at_source"
    assert answer["summary"]["by_reason"] == {
        "absent_at_source": 1,
        "unmapped": 0,
        "no_reference": 2,
    }
    # The weight travels, because the ranking is the whole point.
    assert answer["values"][0]["occurrences"] == 3
    # Every row says its gesture, and the blank one does NOT offer a pair.
    blank = next(row for row in answer["values"] if row["source_value"] == "")
    assert "add the pair" not in blank["repair"].lower()
    assert "collection" in blank["repair"].lower()


def test_without_a_reference_the_same_values_read_as_unmapped(warehouse_file):
    """No reference consulted is not "the reference rejected them"."""
    from core.collected_mapped_reader import ZONE_COLLECTED
    from core.unresolved_values import read_unresolved_set

    answer = read_unresolved_set(
        project_id=PROJECT,
        relation="raw_youtube_daily",
        zone=ZONE_COLLECTED,
        dimension="video",
        start=WINDOW.isoformat(),
        end=WINDOW.isoformat(),
        classifications={"video": "none"},
        resolver=_resolver,
    )
    assert answer["summary"]["by_reason"] == {
        "absent_at_source": 1,
        "unmapped": 3,
        "no_reference": 0,
    }
    # `vid_pair` is resolved by a confirmed pair, so it is absent from the list.
    assert "vid_pair" not in [row["source_value"] for row in answer["values"]]


def test_a_classified_column_is_counted_and_never_listed(warehouse_file):
    from core.collected_mapped_reader import ZONE_COLLECTED
    from core.unresolved_values import read_unresolved_set

    answer = read_unresolved_set(
        project_id=PROJECT,
        relation="raw_youtube_daily",
        zone=ZONE_COLLECTED,
        dimension="video",
        start=WINDOW.isoformat(),
        end=WINDOW.isoformat(),
        classifications={},  # absence of a classification masks
        resolver=_resolver,
    )
    assert answer["state"] == "not_listable"
    assert answer["values"] == []
    assert "classified" in (answer["message"] or "")


def test_an_unreadable_relation_is_never_an_empty_set(warehouse_file):
    from core.collected_mapped_reader import ZONE_COLLECTED
    from core.unresolved_values import read_unresolved_set

    answer = read_unresolved_set(
        project_id=PROJECT,
        relation="raw_nothing_here",
        zone=ZONE_COLLECTED,
        dimension="video",
        start=WINDOW.isoformat(),
        end=WINDOW.isoformat(),
        classifications={"video": "none"},
    )
    assert answer["state"] == "unavailable"
    assert answer["summary"]["observed_distinct"] is None
    assert answer["values"] == []


def test_a_field_that_is_not_a_column_is_refused(warehouse_file):
    from core.collected_mapped_reader import ZONE_COLLECTED
    from core.unresolved_values import read_unresolved_set

    answer = read_unresolved_set(
        project_id=PROJECT,
        relation="raw_youtube_daily",
        zone=ZONE_COLLECTED,
        dimension="playlist",
        start=WINDOW.isoformat(),
        end=WINDOW.isoformat(),
        classifications={"playlist": "none"},
    )
    assert (answer["state"], answer["reason"]) == ("unavailable", "field_absent")


def test_the_listing_says_when_it_stopped(warehouse_file):
    from core.collected_mapped_reader import ZONE_COLLECTED
    from core.unresolved_values import read_unresolved_set

    answer = read_unresolved_set(
        project_id=PROJECT,
        relation="raw_youtube_daily",
        zone=ZONE_COLLECTED,
        dimension="video",
        start=WINDOW.isoformat(),
        end=WINDOW.isoformat(),
        classifications={"video": "none"},
        limit=2,
    )
    assert answer["truncated"] is True
    assert len(answer["values"]) == 2
    # The TOTAL is still true: a bounded page never rewrites the count.
    assert answer["summary"]["observed_distinct"] == 5
    # And the counts SAY they are counting a page. On the deployment this is the
    # difference between "198 unresolved" and the true 516.
    assert answer["summary"]["complete"] is False


# ---------------------------------------------------------------------------
# The extract, and the way back in.
# ---------------------------------------------------------------------------


def test_the_pair_extract_round_trips_through_the_importer():
    from core.unresolved_values import UnresolvedValue, build_extract
    from core.value_mapping_tables import parse_pairs

    values = [
        UnresolvedValue("video", "vid_heavy", "youtube", 3, "unmapped"),
        UnresolvedValue("video", "vid_light", "youtube", 1, "unmapped"),
        # Neither of these may reach the file.
        UnresolvedValue("video", "", "youtube", 1, "absent_at_source"),
        UnresolvedValue("video", "vid_orphan", "youtube", 2, "no_reference"),
    ]
    text = build_extract(values)
    assert text.splitlines()[0] == "source_value,canonical_value"
    assert "vid_orphan" not in text and "vid_heavy" in text

    accepted, rejected = parse_pairs(text)
    # The header is recognised and skipped; the two empty right-hand cells come
    # back NAMED rather than silently dropped.
    assert accepted == []
    assert [row.reason for row in rejected] == [
        "empty_canonical_value",
        "empty_canonical_value",
    ]

    filled = text.replace("vid_heavy,", "vid_heavy,Recette").replace(
        "vid_light,", "vid_light,Chronique"
    )
    accepted, rejected = parse_pairs(filled)
    assert accepted == [("vid_heavy", "Recette"), ("vid_light", "Chronique")]
    assert rejected == []


def test_the_extract_wears_the_destination_headers():
    """A person fills THEIR file, in the columns their file has."""
    from core.unresolved_values import UnresolvedValue, build_extract

    values = [UnresolvedValue("video", "vid_heavy", "youtube", 3, "unmapped")]
    text = build_extract(
        values,
        headers=("video_id", "title", "published_at", "format"),
        key_column="video_id",
    )
    assert text.splitlines() == ["video_id,title,published_at,format", "vid_heavy,,,"]


def test_a_key_that_is_not_a_header_is_refused():
    from core.unresolved_values import build_extract

    with pytest.raises(ValueError):
        build_extract([], headers=("a", "b"), key_column="c")
