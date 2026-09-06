"""Strava club-snapshot seed loader -- Story 53.10.

Lands a deterministic set of ``raw_strava_club_daily`` rows into a DuckDB file
so ``stg_strava_club_daily`` (and the ``fact_strava_club_snapshot`` mart above
it) can build locally. Without it the models exist and nothing has ever
produced a row through them: the connector counts as "covered" while the
warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- and renamed exactly as ``transform()`` renames them
(id -> club_id, name -> club_name, city/state/country -> club_*, private ->
is_private, verified -> is_verified, url -> club_url), so the seed cannot drift
from what the connector actually returns.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/strava/seeds/load_strava_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_strava_club_daily"

#: Exactly the columns `stg_strava_club_daily.sql` selects, in its order.
#: Declared here so a model change breaks the load loudly instead of producing
#: a view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    snapshot_date    VARCHAR,
    club_id          VARCHAR,
    club_name        VARCHAR,
    sport_type       VARCHAR,
    club_city        VARCHAR,
    club_state       VARCHAR,
    club_country     VARCHAR,
    is_private       BOOLEAN,
    is_verified      BOOLEAN,
    club_url         VARCHAR,
    is_own_club      BOOLEAN,
    member_count     BIGINT,
    following_count  BIGINT,
    pull_id          VARCHAR,
    loaded_at        VARCHAR,
    project_id       VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _optional_bool(value) -> bool | None:
    """NULL honnete (AD-9): an absent flag stays NULL, it never becomes False."""
    return None if value is None else bool(value)


def _optional_int(value) -> int | None:
    """NULL honnete (AD-9): an absent count stays NULL, it is never zero-filled."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def generate_rows(project_id: str = "default") -> list[dict]:
    """One row per club in the golden pull.

    That IS the model's grain -- `stg_strava_club_daily` is unique on
    `project_id|snapshot_date|club_id` -- and Strava returns one DetailedClub
    per club per snapshot, so a coarser or finer shape would make the seed pass
    a test the real pull would fail.
    """
    clubs = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return [
        {
            # The connector stamps the snapshot date on the row; the fixture
            # carries it as `date`.
            "snapshot_date": str(club.get("date") or ""),
            "club_id": str(club.get("id") or ""),
            "club_name": str(club.get("name") or ""),
            "sport_type": str(club.get("sport_type") or ""),
            "club_city": str(club.get("city") or ""),
            "club_state": str(club.get("state") or ""),
            "club_country": str(club.get("country") or ""),
            "is_private": _optional_bool(club.get("private")),
            "is_verified": _optional_bool(club.get("verified")),
            "club_url": str(club.get("url") or ""),
            "is_own_club": _optional_bool(club.get("is_own_club")),
            # Non-additive point-in-time LEVELS (AD-4): never summed downstream.
            "member_count": _optional_int(club.get("member_count")),
            "following_count": _optional_int(club.get("following_count")),
            "project_id": project_id,
        }
        for club in clubs
    ]


def load_duckdb(rows: list[dict], pull_id: str, loaded_at: str, duckdb_path: str) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_CREATE_DDL)
        con.executemany(
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["snapshot_date"], row["club_id"], row["club_name"],
                    row["sport_type"], row["club_city"], row["club_state"],
                    row["club_country"], row["is_private"], row["is_verified"],
                    row["club_url"], row["is_own_club"], row["member_count"],
                    row["following_count"], pull_id, loaded_at, row["project_id"],
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: Strava exposes NO history, so a club row is a
    state observed on one snapshot date, not a daily series that can be widened.
    Taking the argument keeps every loader callable the same way, which is what
    lets the fixture discover them instead of hardcoding each one.
    """
    del days
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    rows = generate_rows(project_id=project_id)
    return pull_id, load_duckdb(rows, pull_id, loaded_at, duckdb_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", required=True)
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()
    pull_id, count = run(duckdb_path=args.duckdb_path, project_id=args.project_id)
    print(f"strava seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
