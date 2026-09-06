"""Adjust network_daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_adjust_daily`` rows into a DuckDB file so
``stg_adjust_daily`` can build locally. Without it the model exists and nothing
has ever produced a row through it: the connector counts as "covered" while the
warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- so the seed cannot drift from what the connector
actually returns. The renames below are copied from the manifest's
``canonical_dimension_mapping`` (day -> date, campaign_id_network ->
campaign_id, ...) and the dropped ratio fields from ``connector._DROP_FIELDS``:
storing a ratio would violate AD-4, and re-deriving them here would let the
seed drift from the connector's own rule.

NULL HONNETE (AD-9): a metric the fixture does not carry lands NULL, never 0 --
"the provider did not report it" and "the provider reported zero" must stay
distinguishable all the way to the mart.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The fixture's own ``pull_id`` values are deliberately ignored --
reusing them would make two loads collide instead of supersede. The staging
model supersedes on ``pull_id DESC``, so re-running is safe.

Usage:
    uv run python server/modules/adjust/seeds/load_adjust_seed.py \
        --duckdb-path server/modules/adjust/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_adjust_daily"

#: Exactly the columns `stg_adjust_daily.sql` selects, in that order. Declared
#: here so a model change breaks the load loudly instead of producing a view
#: with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date                  VARCHAR,
    app_token             VARCHAR,
    app                   VARCHAR,
    network               VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    cost                  DOUBLE,
    installs              INTEGER,
    clicks                INTEGER,
    impressions           INTEGER,
    sessions              INTEGER,
    revenue               DOUBLE,
    ad_revenue            DOUBLE,
    all_revenue           DOUBLE,
    cost_source_currency  VARCHAR,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""

#: Named columns rather than positional VALUES: the connector's own DDL lists
#: `cost_source_currency` last, so a positional insert into a table the
#: connector created first would silently shift every value by one.
_INSERT_SQL = f"""
INSERT INTO {RAW_TABLE}
    (date, app_token, app, network, campaign_id, campaign_name, cost, installs,
     clicks, impressions, sessions, revenue, ad_revenue, all_revenue,
     cost_source_currency, pull_id, loaded_at, project_id)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

#: manifest.json canonical_dimension_mapping (AD-2). The provider names live in
#: the fixture; the warehouse only ever sees the canonical ones.
_RENAMES = {
    "day": "date",
    "campaign_id_network": "campaign_id",
    "campaign_network": "campaign_name",
    "currency_code": "cost_source_currency",
}

#: connector._DROP_FIELDS plus the envelope keys. Ratios are NEVER stored
#: (AD-4); `pull_id` is re-minted per load (AD-7), so the fixture's is noise.
_DROPPED = frozenset(
    {"attr_dependency", "ctr", "click_conversion_rate", "impression_conversion_rate",
     "connector", "pull_id"}
)

_TEXT_COLUMNS = ("date", "app_token", "app", "network", "campaign_id", "campaign_name")
_INT_COLUMNS = ("installs", "clicks", "impressions", "sessions")
_FLOAT_COLUMNS = ("cost", "revenue", "ad_revenue", "all_revenue")


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def generate_rows(project_id: str = "default") -> list[dict]:
    """Rename the golden pull into the canonical wide row the model reads.

    One row per (date, app_token, network, campaign_id) -- that IS the model's
    grain, so producing anything coarser would make the seed pass a test the
    real pull would fail. The fixture's three rows are already distinct on it
    (two networks on 2026-07-01, one on 2026-07-02).
    """
    rows: list[dict] = []
    for item in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        canonical = {
            _RENAMES.get(key, key): value
            for key, value in item.items()
            if key not in _DROPPED
        }
        row = {column: str(canonical.get(column) or "") for column in _TEXT_COLUMNS}
        # AD-9: absent stays NULL. The fixture's numbers are strings because it
        # mirrors the Report Service CSV, which quotes every cell.
        for column in _INT_COLUMNS:
            raw = canonical.get(column)
            row[column] = None if raw is None else int(float(raw))
        for column in _FLOAT_COLUMNS:
            raw = canonical.get(column)
            row[column] = None if raw is None else float(raw)
        row["cost_source_currency"] = str(canonical.get("cost_source_currency") or "")
        row["project_id"] = project_id
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
                    row["date"], row["app_token"], row["app"], row["network"],
                    row["campaign_id"], row["campaign_name"], row["cost"], row["installs"],
                    row["clicks"], row["impressions"], row["sessions"], row["revenue"],
                    row["ad_revenue"], row["all_revenue"], row["cost_source_currency"],
                    pull_id, loaded_at, row["project_id"],
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: the rows are a replay of the golden pull,
    whose dates the fixture pins. Stretching them over a rolling window would
    invent days the connector never returned. Taking the argument keeps every
    loader callable the same way, which is what lets the fixture discover them
    instead of hardcoding each one.
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
    print(f"adjust seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
