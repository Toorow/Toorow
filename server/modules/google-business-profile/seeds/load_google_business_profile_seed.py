"""Google Business Profile location_daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_gbp_location_daily`` rows into a DuckDB file
so ``stg_gbp_location_daily`` can build locally. Without it the model exists and
nothing has ever produced a row through it: the connector counts as "covered"
while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- renamed through ``manifest.json``'s
``canonical_metric_mapping`` (read at load time, so the seed cannot drift from
the mapping the connector uses) and coerced with the same ``_to_int()``.

AD-9, and it matters here more than elsewhere: GBP OMITS zero-days. A metric the
API did not return stays NULL, never 0 -- gap-filling is a mart concern. So this
loader coerces absent values to NULL and keeps the fixture's real zeros as 0.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage (from the repo root, path shortened to fit):
    cd server/modules/google-business-profile/seeds
    uv run python load_google_business_profile_seed.py \
        --duckdb-path ../../google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_MODULE_DIR = Path(__file__).resolve().parents[1]
_FIXTURE = _MODULE_DIR / "tests" / "fixtures" / "golden_pull.json"
_MANIFEST = _MODULE_DIR / "manifest.json"

RAW_TABLE = "raw_gbp_location_daily"

#: `connector._METRIC_COLUMNS`, in the connector's order -- which is also the
#: order `stg_gbp_location_daily.sql` selects them in. The order is part of the
#: contract because the INSERT is positional.
_METRIC_COLUMNS = (
    "business_impressions_desktop_maps",
    "business_impressions_desktop_search",
    "business_impressions_mobile_maps",
    "business_impressions_mobile_search",
    "business_conversations",
    "business_direction_requests",
    "call_clicks",
    "website_clicks",
    "business_bookings",
    "business_food_orders",
    "business_food_menu_clicks",
)

#: Exactly the columns `stg_gbp_location_daily.sql` selects, in its order.
#: Declared here so a model change breaks the load loudly instead of producing a
#: view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date                                 VARCHAR,
    location_id                          VARCHAR,
    business_impressions_desktop_maps     BIGINT,
    business_impressions_desktop_search   BIGINT,
    business_impressions_mobile_maps      BIGINT,
    business_impressions_mobile_search    BIGINT,
    business_conversations                BIGINT,
    business_direction_requests           BIGINT,
    call_clicks                           BIGINT,
    website_clicks                        BIGINT,
    business_bookings                     BIGINT,
    business_food_orders                  BIGINT,
    business_food_menu_clicks             BIGINT,
    pull_id                              VARCHAR,
    loaded_at                            VARCHAR,
    project_id                           VARCHAR
)
"""

_INSERT_SQL = f"INSERT INTO {RAW_TABLE} VALUES ({','.join(['?'] * (2 + len(_METRIC_COLUMNS) + 3))})"


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _to_int(value) -> int | None:
    """`connector._to_int()`, verbatim: int64-as-string in, int or NULL out.

    AD-9: an absent DailyMetric is NULL. GBP omits zero-days, so a 0 written
    here for a missing value would be an assertion the API never made.
    """
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _canonical_metric_map() -> dict[str, str]:
    """Source->canonical metric renames, straight from the manifest.

    Duplicating them in this file would let the seed keep landing a name the
    connector has stopped producing.
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    return {
        source: (target if isinstance(target, str) else target.get("canonical", source))
        for source, target in manifest.get("canonical_metric_mapping", {}).items()
    }


def generate_rows(project_id: str = "default") -> list[dict]:
    """Rename the golden pull into one wide row per (date, location).

    That IS the model's grain -- `stg_gbp_location_daily` is unique on
    `project_id|date|location_id` -- and the landing is wide, one column per
    DailyMetric, so there is nothing to melt.
    """
    renames = _canonical_metric_map()
    pulled_rows = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for pulled_row in pulled_rows:
        canonical = {renames.get(key, key): value for key, value in pulled_row.items()}
        row = {
            "date": str(canonical.get("date") or ""),
            "location_id": str(canonical.get("location_id") or ""),
            "project_id": project_id,
        }
        for name in _METRIC_COLUMNS:
            row[name] = _to_int(canonical.get(name))
        rows.append(row)
    return rows


def load_duckdb(rows: list[dict], pull_id: str, loaded_at: str, duckdb_path: str) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_CREATE_DDL)
        con.executemany(
            _INSERT_SQL,
            [
                (
                    row["date"],
                    row["location_id"],
                    *(row[name] for name in _METRIC_COLUMNS),
                    pull_id,
                    loaded_at,
                    row["project_id"],
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


# ---------------------------------------------------------------------------
# Story 30.1 -- the two SEPARATE facts: reviews and monthly search keywords.
#
# These two do NOT get a hand-copied column list. The daily table above has one
# for historical reasons and it costs a conformance test to keep honest; there is
# no reason to pay that twice. The connector's own _RAW_REVIEW_COLUMNS /
# _RAW_KEYWORD_COLUMNS and its own transform functions are loaded BY PATH (the
# module directory carries a hyphen, so it is not importable as a package) and
# reused verbatim -- the seed cannot land a shape the connector stopped writing.
# ---------------------------------------------------------------------------

_CONNECTOR_PATH = _MODULE_DIR / "connector.py"
_REVIEWS_FIXTURE = _MODULE_DIR / "tests" / "fixtures" / "golden_reviews.json"
_KEYWORDS_FIXTURE = _MODULE_DIR / "tests" / "fixtures" / "golden_search_keywords.json"

#: The location the seeded reviews and keywords belong to -- the same one the
#: golden daily pull uses, so the three facts describe one place.
_SEED_LOCATION_ID = "locations/12345678901234567890"
_SEED_ACCOUNT_ID = "accounts/10000000000000000000"
#: The months the keyword rows cover, matching the golden pull's July window.
_SEED_MONTHS = ("2026-06", "2026-07")


def _load_connector():
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location("gbp_connector_for_seed", _CONNECTOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _create_and_insert(con, table: str, columns, rows: list[dict]) -> int:
    ddl = "CREATE TABLE IF NOT EXISTS {} (\n    {}\n)".format(
        table, ",\n    ".join(f"{name} {sql_type}" for name, sql_type in columns)
    )
    insert = "INSERT INTO {} ({}) VALUES ({})".format(
        table,
        ", ".join(name for name, _ in columns),
        ", ".join(["?"] * len(columns)),
    )
    con.execute(ddl)
    if rows:
        con.executemany(insert, [tuple(row.get(name) for name, _ in columns) for row in rows])
    return len(rows)


def load_separate_facts_duckdb(
    pull_id: str, loaded_at: str, duckdb_path: str, project_id: str = "default"
) -> dict[str, int]:
    """Land raw_gbp_review and raw_gbp_search_keyword_monthly from the fixtures.

    Returns {table: row_count}. Append-only like the daily loader: a re-run mints
    a new pull_id and the staging models supersede, so re-seeding is idempotent
    in effect without ever deleting a landed row.
    """
    import duckdb  # noqa: PLC0415

    connector = _load_connector()

    review_rows = connector.transform_reviews(
        json.loads(_REVIEWS_FIXTURE.read_text(encoding="utf-8")),
        account_id=_SEED_ACCOUNT_ID,
        location_id=_SEED_LOCATION_ID,
    )
    keyword_rows: list[dict] = []
    counts = json.loads(_KEYWORDS_FIXTURE.read_text(encoding="utf-8"))["searchKeywordsCounts"]
    for month in _SEED_MONTHS:
        keyword_rows.extend(
            connector.transform_search_keywords(counts, month, location_id=_SEED_LOCATION_ID)
        )
    for row in (*review_rows, *keyword_rows):
        row["pull_id"] = pull_id
        row["loaded_at"] = loaded_at
        row["project_id"] = project_id

    con = duckdb.connect(duckdb_path)
    try:
        return {
            connector._RAW_REVIEW_TABLE: _create_and_insert(
                con, connector._RAW_REVIEW_TABLE, connector._RAW_REVIEW_COLUMNS, review_rows
            ),
            connector._RAW_KEYWORD_TABLE: _create_and_insert(
                con, connector._RAW_KEYWORD_TABLE, connector._RAW_KEYWORD_COLUMNS, keyword_rows
            ),
        }
    finally:
        con.close()


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: the golden pull pins its own dates, and
    widening the window would mean fabricating days GBP never reported. Taking
    the argument keeps every loader callable the same way, which is what lets
    the fixture discover them instead of hardcoding each one.
    """
    del days
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    rows = generate_rows(project_id=project_id)
    count = load_duckdb(rows, pull_id, loaded_at, duckdb_path)
    # The three facts of this connector are seeded together: a mart that never
    # had a row through it is a model, not a covered path (CAV-21).
    separate = load_separate_facts_duckdb(pull_id, loaded_at, duckdb_path, project_id)
    return pull_id, count + sum(separate.values())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", required=True)
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()
    pull_id, count = run(duckdb_path=args.duckdb_path, project_id=args.project_id)
    print(f"google-business-profile seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
