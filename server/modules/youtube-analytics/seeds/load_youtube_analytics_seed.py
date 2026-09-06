"""YouTube Analytics daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_youtube_daily`` rows into a DuckDB file so
``stg_youtube_daily`` (and the ``fact_youtube_daily`` mart above it) can build
locally. Without it the models exist and nothing has ever produced a row
through them: the connector counts as "covered" while the warehouse half of its
path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- and renamed through the SAME manifest map
``transform()`` uses (day -> date, estimatedMinutesWatched ->
estimated_minutes_watched, ...), then unpivoted to long form exactly as
``_insert_raw_rows()`` does. The seed therefore cannot drift from what the
connector actually returns, in name or in shape.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/youtube-analytics/seeds/load_youtube_analytics_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_MODULE_DIR = Path(__file__).resolve().parents[1]
_FIXTURE = _MODULE_DIR / "tests" / "fixtures" / "golden_pull.json"
_BREAKDOWN_FIXTURE = _MODULE_DIR / "tests" / "fixtures" / "golden_breakdown_pull.json"
_MANIFEST = _MODULE_DIR / "manifest.json"

RAW_TABLE = "raw_youtube_daily"
BREAKDOWN_TABLE = "raw_youtube_breakdown"

#: Exactly the columns `stg_youtube_daily.sql` selects, in its order. Declared
#: here so a model change breaks the load loudly instead of producing a view
#: with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date         VARCHAR,
    channel_id   VARCHAR,
    video        VARCHAR,
    metric       VARCHAR,
    value        DOUBLE,
    pull_id      VARCHAR,
    loaded_at    VARCHAR,
    project_id   VARCHAR
)
"""

#: `stg_youtube_breakdown` reads this relation, and an unselected `dbt run`
#: builds every staging model -- so the loader must land the table whether or not
#: it has rows to put in it. Columns are connector.py's `_BREAKDOWN_COLUMNS`, in
#: their declared order.
#:
#: AI-342 -- IT IS NO LONGER LEFT EMPTY, and the sentence that left it empty is
#: retired rather than quietly dropped. It read: "EMPTY, never invented: the
#: daily golden pull carries no breakdown rows, and fabricating some would make
#: the seed pass a shape the real pull never produced." The first half is a fact
#: (`golden_pull.json` is a channel-grain report and carries no breakdown), the
#: second half is not: `golden_pull.json` is itself a written fixture -- channel
#: `UC_toorow_demo_channel`, `pull_youtube_001` -- not a captured response, so
#: "never invented" was never the standard this module actually held.
#:
#: The standard it DOES hold is the one the daily loader states two paragraphs
#: below: THE SHAPE COMES FROM THE CONNECTOR. `golden_breakdown_pull.json` is a
#: fixture in exactly the register of `golden_pull.json`, and every row of the
#: landing is derived from it by the manifest -- the profile's own dimensions
#: and metrics, exploded by `generate_breakdown_rows` with the same nested loop
#: `connector._insert_breakdown_rows` runs. A profile the manifest renames, or a
#: metric it drops, changes this seed without anyone editing it.
#:
#: What the fixture asserts beyond shape is ONE property, and it is the property
#: the mart's double-count discipline rests on: every breakdown of a day totals
#: that day. The six geography/device/traffic/playback/subscription rows of
#: 2026-07-01 each sum to the 1200 views and 3400 watch-minutes `golden_pull.json`
#: reports for the channel that day, and 2026-07-02 to its 1455 / 4102. A seed
#: whose marginals disagreed with its own channel roll-up would make every
#: reconciliation test pass against a fiction.
_CREATE_BREAKDOWN_DDL = """
CREATE TABLE IF NOT EXISTS raw_youtube_breakdown (
    date                 VARCHAR,
    channel_id           VARCHAR,
    breakdown_dimension  VARCHAR,
    breakdown_value      VARCHAR,
    metric               VARCHAR,
    value                DOUBLE,
    pull_id              VARCHAR,
    loaded_at            VARCHAR,
    project_id           VARCHAR
)
"""


#: Same rule as the breakdown table above, and the same reason it exists: an
#: unselected `dbt run` builds EVERY staging model, so a relation nobody seeds
#: takes the whole local loop down -- measured 2026-09-01 at HEAD, before this
#: block: `46 of 94 ERROR creating sql view model main_staging.
#: stg_youtube_video_directory -- Catalog Error: Table with name
#: raw_youtube_video_directory does not exist!`, PASS=93 ERROR=1, and every
#: seed-to-mart test erroring at setup.
#:
#: EMPTY, never invented: the directory is read from the PUBLIC Data API and the
#: golden pull of THIS module carries no directory rows. Columns are
#: `connector.py`'s `_DIRECTORY_COLUMNS`, in their declared order, with the
#: DuckDB spelling of each declared type (FLOAT -> DOUBLE, BOOLEAN kept).
_CREATE_DIRECTORY_DDL = """
CREATE TABLE IF NOT EXISTS raw_youtube_video_directory (
    date              VARCHAR,
    channel_id        VARCHAR,
    channel_title     VARCHAR,
    video             VARCHAR,
    video_title       VARCHAR,
    published_at      VARCHAR,
    duration_seconds  DOUBLE,
    lifetime_views    DOUBLE,
    is_own_channel    BOOLEAN,
    pull_id           VARCHAR,
    loaded_at         VARCHAR,
    project_id        VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _rename_map() -> dict[str, str]:
    """The connector's own AD-2 rename map, read rather than re-typed.

    Copying the pairs into this file would let the seed keep landing
    `estimatedMinutesWatched` long after the manifest renamed it -- the exact
    drift the fixture-derived rule exists to prevent.
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    renames: dict[str, str] = {}
    for source, value in (manifest.get("canonical_metric_mapping") or {}).items():
        renames[source] = value if isinstance(value, str) else value.get("canonical", source)
    renames.update(manifest.get("canonical_dimension_mapping") or {})
    return renames


def generate_rows(project_id: str = "default") -> list[dict]:
    """Unpivot the golden pull into one row per (date, channel, video, metric).

    That IS the model's grain -- `stg_youtube_daily` is unique on
    `project_id|date|channel_id|video|metric` -- so producing anything coarser
    would make the seed pass a test the real pull would fail.
    """
    renames = _rename_map()
    metric_ids = [
        renames.get(source, source)
        for source in (
            "views", "estimatedMinutesWatched", "likes", "comments", "shares",
            "subscribersGained", "subscribersLost",
        )
    ]
    rows: list[dict] = []
    for report_row in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        canonical = {renames.get(key, key): value for key, value in report_row.items()}
        for metric in metric_ids:
            if metric not in canonical:
                # NULL honnete (AD-9): an absent metric lands NO row, so absence
                # stays distinguishable from a recorded zero.
                continue
            rows.append(
                {
                    "date": str(canonical.get("date") or ""),
                    "channel_id": str(canonical.get("channel_id") or ""),
                    # channel_daily carries no video breakdown; the connector
                    # lands '' there, and '' is part of the grain key.
                    "video": str(canonical.get("video") or ""),
                    "metric": metric,
                    "value": float(canonical[metric]),
                    "project_id": project_id,
                }
            )
    return rows


def _breakdown_profiles() -> dict[str, tuple[list[str], list[str]]]:
    """profile id -> (its breakdown dimensions, its metrics), read from the manifest.

    `channel_id` and `date` are not breakdowns -- they are the grain every report
    shares -- which is the same exclusion `connector._insert_breakdown_rows`
    makes. Reading it here rather than listing six profile names is what lets a
    seventh breakdown profile seed itself.
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    profiles: dict[str, tuple[list[str], list[str]]] = {}
    for report in manifest.get("source_capabilities", {}).get("reports") or []:
        dimensions = [
            d for d in (report.get("dimensions") or []) if d not in ("channel_id", "date")
        ]
        profiles[str(report.get("id") or "")] = (dimensions, list(report.get("metrics") or []))
    return profiles


def generate_breakdown_rows(project_id: str = "default") -> list[dict]:
    """Explode the golden breakdown fixture into the canonical long landing.

    ONE ROW PER (date, channel, breakdown_dimension, breakdown_value, metric),
    which is what `connector._insert_breakdown_rows` writes: for each API row,
    every declared dimension of its profile crossed with every declared metric
    that row carries. A profile carrying TWO dimensions (`audience_device` --
    device type and operating system) therefore lands the same measurement twice,
    once under each dimension, and the two are separate marginals of the same day.
    """
    renames = _rename_map()
    profiles = _breakdown_profiles()
    rows: list[dict] = []
    for report_row in json.loads(_BREAKDOWN_FIXTURE.read_text(encoding="utf-8")):
        profile = str(report_row.get("report_profile") or "")
        dimensions, metrics = profiles.get(profile, ([], []))
        if not dimensions:
            # A profile the manifest does not declare as a breakdown lands
            # nothing rather than a guessed dimension.
            continue
        canonical = {renames.get(key, key): value for key, value in report_row.items()}
        for dimension in dimensions:
            if dimension not in canonical:
                continue
            for metric in metrics:
                if metric not in canonical:
                    # NULL honnete (AD-9): an absent metric lands NO row.
                    continue
                rows.append(
                    {
                        "date": str(canonical.get("date") or ""),
                        "channel_id": str(canonical.get("channel_id") or ""),
                        "breakdown_dimension": dimension,
                        "breakdown_value": str(canonical.get(dimension) or ""),
                        "metric": metric,
                        "value": float(canonical[metric]),
                        "project_id": project_id,
                    }
                )
    return rows


def load_duckdb(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
    breakdown_rows: list[dict] | None = None,
) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_CREATE_DDL)
        con.execute(_CREATE_BREAKDOWN_DDL)
        con.execute(_CREATE_DIRECTORY_DDL)
        con.executemany(
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    row["date"], row["channel_id"], row["video"], row["metric"],
                    row["value"], pull_id, loaded_at, row["project_id"],
                )
                for row in rows
            ],
        )
        if breakdown_rows:
            con.executemany(
                f"INSERT INTO {BREAKDOWN_TABLE} VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        row["date"], row["channel_id"], row["breakdown_dimension"],
                        row["breakdown_value"], row["metric"], row["value"],
                        pull_id, loaded_at, row["project_id"],
                    )
                    for row in breakdown_rows
                ],
            )
    finally:
        con.close()
    return len(rows) + len(breakdown_rows or [])


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: the dates come from the golden fixture, which
    is what pins the seed to the connector's real response. Widening the window
    here would mean inventing days the fixture never observed. Taking the
    argument keeps every loader callable the same way, which is what lets the
    fixture discover them instead of hardcoding each one.
    """
    del days
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    rows = generate_rows(project_id=project_id)
    breakdown_rows = generate_breakdown_rows(project_id=project_id)
    return pull_id, load_duckdb(rows, pull_id, loaded_at, duckdb_path, breakdown_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", required=True)
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()
    pull_id, count = run(duckdb_path=args.duckdb_path, project_id=args.project_id)
    print(
        f"youtube-analytics seed loaded: pull_id={pull_id} rows={count} "
        f"(daily + breakdown)"
    )


if __name__ == "__main__":
    main()
