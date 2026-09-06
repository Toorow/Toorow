"""IAS daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_ias_daily`` rows into a DuckDB file so
``stg_ias_daily`` can build locally. Without it the model exists and nothing has
ever produced a row through it: the connector counts as "covered" while the
warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- put through the two steps the connector applies
before landing: ``transform()`` renames the camelCase API tokens with the
manifest mappings and drops every rate (AD-4, ``_DROP_FIELDS``), then
``_insert_raw_rows()`` stamps the report profile. The rename map is READ from
``manifest.json`` rather than retyped here, so a contract change breaks the load
instead of drifting past it.

ONE ROW PER MERGED CELL, NOT PER PROFILE. The fixture carries the viewability,
the invalid-traffic and the brand-safety counts on a single row -- exactly as
``expected_facts.json`` records them. The model's grain admits one row per
(project_id, date, campaign_id); ``report_profile`` is provenance, NOT a grain
key. So the seed lands that merged cell once, stamped with the profile ``pull()``
defaults to. Splitting it into three profile rows would triplicate the grain and
the QUALIFY would silently keep exactly one of them.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/ias/seeds/load_ias_seed.py \
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
_MANIFEST = _MODULE_DIR / "manifest.json"

RAW_TABLE = "raw_ias_daily"

#: `connector._DROP_FIELDS`. AD-4: an IAS rate is never stored -- summing one is
#: meaningless. The semantic layer recomputes it from the additive counts that
#: ARE stored (viewable / measured, passed / measured, ...).
_DROP_FIELDS = frozenset(
    {
        "viewableRate",
        "measuredRate",
        "fraudulentPct",
        "givtPct",
        "passedPct",
        "failedPct",
        "blockedPct",
    }
)

#: Fixture provenance keys: they identify the recording, not the measurement.
_PROVENANCE_KEYS = frozenset({"pull_id", "connector"})

#: The profile `connector.pull()` defaults to. Stamped on the merged cell (see
#: the module docstring) -- our own manifest id, not a provider value.
_REPORT_PROFILE = "viewability_daily"

#: The six additive counters the model selects, in its order.
_METRIC_COLUMNS = (
    "measured_impressions",
    "viewable_impressions",
    "eligible_impressions",
    "invalid_traffic_ads",
    "brand_safety_passed_ads",
    "brand_safety_failed_ads",
)

#: Exactly the columns `stg_ias_daily.sql` selects, in its order. Declared here
#: so a model change breaks the load loudly instead of producing a view with a
#: silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date                     VARCHAR,
    campaign_id              VARCHAR,
    campaign_name            VARCHAR,
    measured_impressions     BIGINT,
    viewable_impressions     BIGINT,
    eligible_impressions     BIGINT,
    invalid_traffic_ads      BIGINT,
    brand_safety_passed_ads  BIGINT,
    brand_safety_failed_ads  BIGINT,
    report_profile           VARCHAR,
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _rename_map() -> dict[str, str]:
    """`connector._rename_map()`: AD-2 renames belong to the manifest, not to code."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    return {
        **manifest.get("canonical_metric_mapping", {}),
        **manifest.get("canonical_dimension_mapping", {}),
    }


def _to_int(value) -> int | None:
    """AD-9: an absent counter stays NULL. Zero is a measurement, absence is not."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def generate_rows(project_id: str = "default") -> list[dict]:
    """Canonicalise the golden pull into one row per (date, campaign).

    That IS the model's grain -- `stg_ias_daily` is unique on
    `project_id|date|campaign_id` -- so producing anything coarser would make
    the seed pass a test the real pull would fail.
    """
    rename = _rename_map()
    rows: list[dict] = []
    for raw_row in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        canonical = {
            rename.get(key, key): value
            for key, value in raw_row.items()
            if key not in _DROP_FIELDS and key not in _PROVENANCE_KEYS
        }
        row = {
            "date": str(canonical.get("date", "")),
            "campaign_id": str(canonical.get("campaign_id", "")),
            "campaign_name": str(canonical.get("campaign_name", "")),
            "report_profile": _REPORT_PROFILE,
            "project_id": project_id,
        }
        for column in _METRIC_COLUMNS:
            row[column] = _to_int(canonical.get(column))
        rows.append(row)
    return rows


def load_duckdb(rows: list[dict], pull_id: str, loaded_at: str, duckdb_path: str) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_CREATE_DDL)
        con.executemany(
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["date"], row["campaign_id"], row["campaign_name"],
                    *(row[column] for column in _METRIC_COLUMNS),
                    row["report_profile"], pull_id, loaded_at, row["project_id"],
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: the golden pull pins its own measurement
    dates, and widening the window would mean fabricating IAS rows. Taking the
    argument keeps every loader callable the same way, which is what lets the
    fixture discover them instead of hardcoding each one.
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
    print(f"ias seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
