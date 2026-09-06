"""CM360 standard_daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_cm360_daily`` rows into a DuckDB file so
``stg_cm360_daily`` can build locally. Without it the model exists and nothing
has ever produced a row through it: the connector counts as "covered" while the
warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- and melted the way ``connector._land()`` melts a
report row: one raw row per (dimension tuple, metric), with ``provider_value``
kept as the provider string and ``value`` its float reading.

Fields the model requires and the fixture does not carry are the ones CM360
supplies through the *selection*, not through the report: ``profile_id``,
``account_id``, ``subaccount_id``. They are derived from ``advertiser_id`` and
prefixed ``seed-`` so no reader mistakes them for real CM360 ids.
``report_profile`` is not derived -- it is the connector's own profile constant.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/cm360/seeds/load_cm360_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_cm360_daily"

#: The `standard_daily` report contract, verbatim from `manifest.json`
#: (`source_capabilities.reports[standard_daily]`). Declared rather than
#: inferred: guessing which fixture keys are metrics would let a renamed
#: dimension silently land as a metric.
REPORT_PROFILE = "standard_daily"
_DIMENSIONS = ("date", "advertiser_id", "campaign_id", "placement_id", "creative_id")
_METRICS = ("impressions", "clicks", "cost")

#: Same set `connector._land()` uses. Neither appears in `standard_daily`, but
#: the column exists at every grain, so the seed must fill it honestly.
_NON_ADDITIVE = frozenset({"unique_reach", "average_frequency"})

#: Exactly the columns `stg_cm360_daily.sql` selects, in its order. Declared
#: here so a model change breaks the load loudly instead of producing a view
#: with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    report_profile          VARCHAR,
    profile_id              VARCHAR,
    account_id              VARCHAR,
    subaccount_id           VARCHAR,
    advertiser_id           VARCHAR,
    date                    VARCHAR,
    campaign_id             VARCHAR,
    placement_id            VARCHAR,
    creative_id             VARCHAR,
    floodlight_activity_id  VARCHAR,
    dimensions_json         VARCHAR,
    metric                  VARCHAR,
    value                   DOUBLE,
    provider_value          VARCHAR,
    non_additive            BOOLEAN,
    pull_id                 VARCHAR,
    loaded_at               VARCHAR,
    project_id              VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _numeric(provider_value) -> float | None:
    """Read the provider string the way `_land()` does -- None, never 0.

    AD-9: a value CM360 did not send is NULL. Coercing it to 0 would make an
    absent metric indistinguishable from a measured zero.
    """
    try:
        return float(str(provider_value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def generate_rows(project_id: str = "default") -> list[dict]:
    """Melt the golden pull into one row per (dimension tuple, metric).

    That IS the model's grain -- `stg_cm360_daily` is unique on the full
    dimension tuple plus `dimensions_json` and `metric` -- so producing anything
    coarser would make the seed pass a test the real pull would fail.
    """
    report_rows = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for report_row in report_rows:
        advertiser_id = str(report_row.get("advertiser_id") or "")
        dimensions = {
            name: report_row[name] for name in _DIMENSIONS if report_row.get(name) is not None
        }
        # Sorted keys: `_land()` serialises the same way, and a JSON blob that
        # depended on response ordering would churn the grain without a schema
        # change.
        dimensions_json = json.dumps(dimensions, sort_keys=True)
        for metric in _METRICS:
            if metric not in report_row:
                continue
            provider_value = report_row[metric]
            rows.append(
                {
                    "report_profile": REPORT_PROFILE,
                    # Selection context, not report content: derived from the
                    # advertiser and deliberately shaped so no reader mistakes
                    # it for a real CM360 profile/account id.
                    "profile_id": f"seed-profile-{advertiser_id}",
                    "account_id": f"seed-account-{advertiser_id}",
                    "subaccount_id": f"seed-subaccount-{advertiser_id}",
                    "advertiser_id": advertiser_id,
                    "date": report_row.get("date"),
                    "campaign_id": report_row.get("campaign_id"),
                    "placement_id": report_row.get("placement_id"),
                    "creative_id": report_row.get("creative_id"),
                    # Absent from `standard_daily`: a FLOODLIGHT-only dimension.
                    # NULL is the honest value (AD-9).
                    "floodlight_activity_id": report_row.get("floodlight_activity_id"),
                    "dimensions_json": dimensions_json,
                    "metric": metric,
                    "value": _numeric(provider_value),
                    "provider_value": str(provider_value),
                    "non_additive": metric in _NON_ADDITIVE,
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
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["report_profile"], row["profile_id"], row["account_id"],
                    row["subaccount_id"], row["advertiser_id"], row["date"],
                    row["campaign_id"], row["placement_id"], row["creative_id"],
                    row["floodlight_activity_id"], row["dimensions_json"], row["metric"],
                    row["value"], row["provider_value"], row["non_additive"],
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
    and widening the window would mean fabricating CM360 report rows. Taking the
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
    print(f"cm360 seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
