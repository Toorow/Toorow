"""DV360 standard_daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_dv360_daily`` rows into a DuckDB file so
``stg_dv360_daily`` can build locally. Without it the model exists and nothing
has ever produced a row through it: the connector counts as "covered" while the
warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- and melted the way ``connector._land()`` melts a Bid
Manager artifact row: one raw row per (dimension tuple, metric), with
``provider_value`` kept verbatim and ``value`` divided by 1e6 for the
``*_micros`` metrics, exactly once, as ``_convert_metric()`` does.

``partner_id`` is required by the model and absent from the artifact: DV360
supplies it through the *selection* (partners.list), not the report. It is
derived from the advertiser id and prefixed ``seed-`` so no reader mistakes it
for a real partner. ``timezone`` and ``currency`` are the same kind of
selection context; the connector defaults them to the empty string when the
discovery payload carries none, and so does this loader -- a fabricated ISO
code would be indistinguishable from a real one.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/dv360/seeds/load_dv360_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_dv360_daily"

#: The `standard_daily` report contract, verbatim from `manifest.json`
#: (`source_capabilities.reports[standard_daily]`). Declared rather than
#: inferred: guessing which artifact columns are metrics would let a renamed
#: dimension silently land as a metric.
REPORT_PROFILE = "standard_daily"
_DIMENSIONS = (
    "date",
    "advertiser_id",
    "campaign_id",
    "insertion_order_id",
    "line_item_id",
    "creative_id",
)
_METRICS = ("impressions", "clicks", "media_cost_micros")

#: Same set `connector._land()` uses. None of them belongs to `standard_daily`,
#: but the column exists at every grain, so the seed must fill it honestly.
_NON_ADDITIVE = frozenset({"unique_reach", "average_frequency", "viewability_rate"})

#: Exactly the columns `stg_dv360_daily.sql` selects, in its order. Declared
#: here so a model change breaks the load loudly instead of producing a view
#: with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    report_profile      VARCHAR,
    partner_id          VARCHAR,
    advertiser_id       VARCHAR,
    date                VARCHAR,
    campaign_id         VARCHAR,
    insertion_order_id  VARCHAR,
    line_item_id        VARCHAR,
    creative_id         VARCHAR,
    conversion_type     VARCHAR,
    country             VARCHAR,
    youtube_ad_group_id VARCHAR,
    timezone            VARCHAR,
    currency            VARCHAR,
    dimensions_json     VARCHAR,
    metric              VARCHAR,
    value               DOUBLE,
    provider_value      VARCHAR,
    non_additive        BOOLEAN,
    pull_id             VARCHAR,
    loaded_at           VARCHAR,
    project_id          VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _convert_metric(metric: str, provider_value) -> float | None:
    """Same conversion as `connector._convert_metric()` -- micros once, or None.

    AD-9: a value DV360 did not send is NULL. Coercing it to 0 would make an
    absent metric indistinguishable from a measured zero.
    """
    try:
        value = float(str(provider_value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return value / 1_000_000 if metric.endswith("_micros") else value


def generate_rows(project_id: str = "default") -> list[dict]:
    """Melt the golden pull into one row per (dimension tuple, metric).

    That IS the model's grain -- `stg_dv360_daily` is unique on the provider
    dimension tuple plus `dimensions_json` and `metric` -- so producing anything
    coarser would make the seed pass a test the real pull would fail.
    """
    artifact_rows = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for artifact_row in artifact_rows:
        advertiser_id = str(artifact_row.get("advertiser_id") or "")
        dimensions = {
            name: artifact_row[name] for name in _DIMENSIONS if artifact_row.get(name) is not None
        }
        # Sorted keys: `_land()` serialises the same way, and a JSON blob that
        # depended on artifact column ordering would churn the grain without a
        # schema change.
        dimensions_json = json.dumps(dimensions, sort_keys=True)
        for metric in _METRICS:
            if metric not in artifact_row:
                continue
            provider_value = artifact_row[metric]
            rows.append(
                {
                    "report_profile": REPORT_PROFILE,
                    # Selection context, not artifact content: derived from the
                    # advertiser and shaped so no reader mistakes it for a real
                    # DV360 partner id.
                    "partner_id": f"seed-partner-{advertiser_id}",
                    "advertiser_id": advertiser_id,
                    "date": artifact_row.get("date"),
                    "campaign_id": artifact_row.get("campaign_id"),
                    "insertion_order_id": artifact_row.get("insertion_order_id"),
                    "line_item_id": artifact_row.get("line_item_id"),
                    "creative_id": artifact_row.get("creative_id"),
                    # Dimensions of the other report profiles (conversion_daily,
                    # reach, youtube_compatible). NULL is the honest value here.
                    "conversion_type": artifact_row.get("conversion_type"),
                    "country": artifact_row.get("country"),
                    "youtube_ad_group_id": artifact_row.get("youtube_ad_group_id"),
                    "timezone": "",
                    "currency": "",
                    "dimensions_json": dimensions_json,
                    "metric": metric,
                    "value": _convert_metric(metric, provider_value),
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
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["report_profile"], row["partner_id"], row["advertiser_id"], row["date"],
                    row["campaign_id"], row["insertion_order_id"], row["line_item_id"],
                    row["creative_id"], row["conversion_type"], row["country"],
                    row["youtube_ad_group_id"], row["timezone"], row["currency"],
                    row["dimensions_json"], row["metric"], row["value"], row["provider_value"],
                    row["non_additive"], pull_id, loaded_at, row["project_id"],
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: the golden pull pins its own artifact dates,
    and widening the window would mean fabricating DV360 report rows. Taking the
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
    print(f"dv360 seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
