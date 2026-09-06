"""X Ads daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_x_ads_daily`` LONG rows into a DuckDB file so
``stg_x_ads_daily`` can build locally. Without it the model exists and nothing
has ever produced a row through it: the connector counts as "covered" while the
warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- and landed with the PROVIDER metric ids, because
that is what ``_land()`` writes: the canonical rename (billed_charge_local_micro
-> cost) happens in ``transform()``, downstream of this table. Landing the
canonical name here would make the seed disagree with every real pull.

MICROS: ``value`` divides a ``*_micro`` provider value by 1e6 exactly as
``_land()`` does, while ``provider_value`` keeps the untouched string. The pair
is what lets a reader check the conversion instead of trusting it.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/x-ads/seeds/load_x_ads_seed.py \
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
_COMPATIBILITY = _MODULE_DIR / "catalog_sources" / "compatibility.json"

RAW_TABLE = "raw_x_ads_daily"

#: The manifest report whose declared grain (date, account_id, campaign_id,
#: placement) the golden fixture matches. report_profile is part of the
#: supersede grain, so it is not a free label.
REPORT_PROFILE = "campaign_daily"

#: Keys of the fixture row that are DIMENSIONS, not metrics. Everything else in
#: the row is a metric to unpivot -- the same split `_land()` performs.
_DIMENSION_KEYS = frozenset({"date", "account_id", "campaign_id", "placement"})

#: The connector's non-additivity heuristic, verbatim. A ratio/level column must
#: never be summed downstream, and the flag is how the warehouse knows.
_NON_ADDITIVE_TOKENS = ("rate", "ratio", "average", "reach", "frequency")

#: Exactly the columns `raw_x_ads_daily` carries, in the connector's landing
#: order. `stg_x_ads_daily` is a `SELECT *`, so this order IS the model's
#: column order; a mismatch would silently shift every value one column left.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    report_profile VARCHAR, account_id VARCHAR, entity_type VARCHAR, entity_id VARCHAR,
    placement VARCHAR, segment_type VARCHAR, segment_value VARCHAR, interval_start VARCHAR,
    interval_end VARCHAR, timezone VARCHAR, currency VARCHAR, metric VARCHAR, value DOUBLE,
    provider_value VARCHAR, non_additive BOOLEAN, request_hash VARCHAR, pull_id VARCHAR,
    loaded_at VARCHAR, project_id VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _entity_type() -> str:
    """The entity the profile reports on, read from the compatibility contract.

    Re-typing "CAMPAIGN" here would let the seed keep claiming a campaign grain
    the day the profile is repointed at line items.
    """
    profiles = json.loads(_COMPATIBILITY.read_text(encoding="utf-8"))["profiles"]
    return str(profiles[REPORT_PROFILE]["entity"])


def _numeric(provider_value: str, metric: str) -> float | None:
    """Provider value -> stored value, with the connector's micros rule."""
    try:
        value = float(provider_value)
    except (TypeError, ValueError):
        return None
    if metric.endswith("_micro") or metric.endswith("_micros"):
        value /= 1_000_000
    return value


def generate_rows(project_id: str = "default") -> list[dict]:
    """Unpivot the golden pull into one row per (grain, metric).

    That IS the model's grain -- `stg_x_ads_daily` is unique on
    `project_id|report_profile|account_id|entity_type|entity_id|placement|
    segment_type|segment_value|interval_start|interval_end|metric` -- so
    producing anything coarser would make the seed pass a test the real pull
    would fail.
    """
    entity_type = _entity_type()
    rows: list[dict] = []
    for stat_row in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        day = str(stat_row.get("date") or "")
        for metric, provider_value in stat_row.items():
            if metric in _DIMENSION_KEYS:
                continue
            rows.append(
                {
                    "report_profile": REPORT_PROFILE,
                    "account_id": str(stat_row.get("account_id") or ""),
                    "entity_type": entity_type,
                    "entity_id": str(stat_row.get("campaign_id") or ""),
                    "placement": str(stat_row.get("placement") or "ALL_ON_TWITTER"),
                    # campaign_daily declares segmentations [null]: there is no
                    # segment on these rows, and '' is what the connector lands.
                    "segment_type": "",
                    "segment_value": "",
                    # One daily bucket: the fixture carries a single date, so
                    # start and end of the interval are that same day.
                    "interval_start": day,
                    "interval_end": day,
                    # Both come from the ACCOUNT selection, not from the report
                    # body; the fixture carries no selection, and '' is what the
                    # connector lands in that case (context.get(..., "")).
                    "timezone": "",
                    "currency": "",
                    "metric": metric,
                    "value": _numeric(provider_value, metric),
                    "provider_value": str(provider_value),
                    "non_additive": any(token in metric for token in _NON_ADDITIVE_TOKENS),
                    # Not in the fixture: the real hash digests the async stats
                    # request that produced the batch, and there is no request
                    # here. Deliberately shaped so no reader mistakes it for one.
                    "request_hash": f"seed request {REPORT_PROFILE}",
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
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["report_profile"], row["account_id"], row["entity_type"],
                    row["entity_id"], row["placement"], row["segment_type"],
                    row["segment_value"], row["interval_start"], row["interval_end"],
                    row["timezone"], row["currency"], row["metric"], row["value"],
                    row["provider_value"], row["non_additive"], row["request_hash"],
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

    `days` is accepted and unused: the interval comes from the golden fixture,
    which is what pins the seed to the connector's real response. Widening it
    would mean inventing days the fixture never observed. Taking the argument
    keeps every loader callable the same way, which is what lets the fixture
    discover them instead of hardcoding each one.
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
    print(f"x-ads seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
