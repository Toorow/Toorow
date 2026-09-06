"""Google Ad Manager historical_daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_google_ad_manager_daily`` rows into a DuckDB
file so ``stg_google_ad_manager_daily`` can build locally. Without it the model
exists and nothing has ever produced a row through it: the connector counts as
"covered" while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- renamed through ``manifest.json``'s canonical
mappings (read at load time, so the seed cannot drift from the mapping the
connector uses) and expanded to marginals the way ``_expand_to_marginals()``
does: one row per (metric, breakdown dimension).

MONEY stays in MICROS, as it does in the connector, in staging AND in the mart.
Dividing here would put the /1e6 in two places.

``currency`` and ``report_timezone`` land as NULL. They are network context
(``networks.get``), not report content, and the golden pull does not carry them;
an ISO 4217 code written here would be indistinguishable from a real one, which
is exactly what this seed must never produce. Neither column is NOT NULL and
neither participates in the grain.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/google-ad-manager/seeds/load_google_ad_manager_seed.py \
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

RAW_TABLE = "raw_google_ad_manager_daily"

#: Exactly the columns `stg_google_ad_manager_daily.sql` selects, in its order.
#: Declared here so a model change breaks the load loudly instead of producing a
#: view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date                VARCHAR,
    metric              VARCHAR,
    value               DOUBLE,
    currency            VARCHAR,
    report_timezone     VARCHAR,
    breakdown_dimension VARCHAR,
    breakdown_value     VARCHAR,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _canonical_maps() -> tuple[dict[str, str], dict[str, str]]:
    """(metric, dimension) source->canonical renames, straight from the manifest.

    Duplicating them in this file would let the seed keep landing a name the
    connector has stopped producing.
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    metrics = {
        source: (target if isinstance(target, str) else target.get("canonical", source))
        for source, target in manifest.get("canonical_metric_mapping", {}).items()
    }
    dimensions = dict(manifest.get("canonical_dimension_mapping", {}))
    return metrics, dimensions


def generate_rows(project_id: str = "default") -> list[dict]:
    """Expand the golden pull into one row per (date, metric, breakdown).

    That IS the model's grain -- `stg_google_ad_manager_daily` is unique on
    `project_id|date|metric|breakdown_dimension|breakdown_value` -- so producing
    anything coarser would make the seed pass a test the real pull would fail.

    Only the breakdown dimensions the fixture actually carries are expanded. The
    profile also declares `line_item_name`; emitting it with an empty value
    would invent a marginal this pull never measured.
    """
    metric_renames, dimension_renames = _canonical_maps()
    wide_rows = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for wide_row in wide_rows:
        canonical = {
            metric_renames.get(key, dimension_renames.get(key, key)): value
            for key, value in wide_row.items()
        }
        date = str(canonical.get("date", ""))
        breakdowns = [
            (name, str(canonical[name]))
            for name in dimension_renames.values()
            if name != "date" and canonical.get(name) is not None
        ]
        for metric in metric_renames.values():
            if metric not in canonical:
                continue
            for breakdown_dimension, breakdown_value in breakdowns:
                rows.append(
                    {
                        "date": date,
                        "metric": metric,
                        # MICROS for the money metric: the /1e6 happens once, at
                        # read, together with the currency.
                        "value": float(canonical[metric]),
                        "breakdown_dimension": breakdown_dimension,
                        "breakdown_value": breakdown_value,
                        "project_id": project_id,
                    }
                )
    return rows


def load_duckdb(rows: list[dict], pull_id: str, loaded_at: str, duckdb_path: str) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_CREATE_DDL)
        con.executemany(
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["date"], row["metric"], row["value"],
                    None,  # currency: network context, absent from the fixture.
                    None,  # report_timezone: idem.
                    row["breakdown_dimension"], row["breakdown_value"],
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

    `days` is accepted and unused: the golden pull pins its own report dates,
    and widening the window would mean fabricating GAM report rows. Taking the
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
    print(f"google-ad-manager seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
