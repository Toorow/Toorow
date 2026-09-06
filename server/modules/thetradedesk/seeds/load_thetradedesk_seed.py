"""The Trade Desk daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_thetradedesk_daily`` LONG rows into a DuckDB
file so ``stg_thetradedesk_daily`` can build locally. Without it the model
exists and nothing has ever produced a row through it: the connector counts as
"covered" while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- renamed through the SAME manifest map
``transform()`` uses (Impressions -> impressions, AdvertiserCostUSD ->
advertiser_cost_usd, ...) and then long-ified one row per (grain, metric)
exactly as ``_insert_raw_rows()`` does.

WHY report_template = daily_performance: the fixture's columns ARE that
template's declared shape (dimensions date/AdvertiserId/CampaignId/AdGroupId,
its nine metrics -- catalog_sources.json report_template_compatibility). The
template is not a free label; it is part of the supersede grain, so naming the
wrong one would let two report grains double-count inside one series.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect -- the same mechanism that absorbs TTD's
attribution restatements.

Usage:
    uv run python server/modules/thetradedesk/seeds/load_thetradedesk_seed.py \
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

RAW_TABLE = "raw_thetradedesk_daily"

#: The managed ReportTemplate whose declared shape the golden fixture matches.
#: `stg_thetradedesk_daily` tests this column against a closed accepted_values
#: list, so an invented label fails loudly rather than quietly.
REPORT_TEMPLATE = "daily_performance"

#: Exactly the columns `stg_thetradedesk_daily.sql` selects, in its order.
#: Declared here so a model change breaks the load loudly instead of producing
#: a view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date                  VARCHAR,
    report_template       VARCHAR,
    partner_id            VARCHAR,
    advertiser_id         VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    ad_group_id           VARCHAR,
    ad_group_name         VARCHAR,
    creative_id           VARCHAR,
    segments_json         VARCHAR,
    metric                VARCHAR,
    value_num             DOUBLE,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _rename_map() -> dict[str, str]:
    """The connector's own AD-2 rename map, read rather than re-typed.

    Copying the pairs into this file would let the seed keep landing
    `AdvertiserCostUSD` long after the manifest renamed it -- the exact drift
    the fixture-derived rule exists to prevent.
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    renames: dict[str, str] = {}
    for source, value in (manifest.get("canonical_metric_mapping") or {}).items():
        renames[source] = value if isinstance(value, str) else value.get("canonical", source)
    renames.update(manifest.get("canonical_dimension_mapping") or {})
    return renames


def _metric_names() -> list[str]:
    """The canonical landed names of the daily_performance metric columns."""
    renames = _rename_map()
    manifest_metrics = (
        "Impressions", "Clicks", "Bids", "AdvertiserCostAdvCurrency", "AdvertiserCostUSD",
        "TtdCostUsd", "PartnerCostUsd", "TotalClickConversions", "TotalViewThroughConversions",
    )
    return [renames.get(column, column) for column in manifest_metrics]


def generate_rows(project_id: str = "default") -> list[dict]:
    """Long-ify the golden pull into one row per (grain, metric).

    That IS the model's grain -- `stg_thetradedesk_daily` is unique on
    `project_id|date|report_template|partner_id|advertiser_id|campaign_id|
    ad_group_id|creative_id|segments_json|metric` -- and the landing is LONG
    because catalog_daily can select ANY column of the 263-column surface, which
    no wide table could hold.
    """
    renames = _rename_map()
    metric_names = _metric_names()
    rows: list[dict] = []
    for report_row in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        canonical = {renames.get(key, key): value for key, value in report_row.items()}
        advertiser_id = str(canonical.get("advertiser_id") or "")
        for metric in metric_names:
            value = canonical.get(metric)
            if value is None or value == "":
                # NULL honnete (AD-9): an absent metric lands NO row, so absence
                # stays distinguishable from a recorded zero.
                continue
            rows.append(
                {
                    "date": str(canonical.get("date") or ""),
                    "report_template": REPORT_TEMPLATE,
                    # Not in the fixture (MyReports returns the PartnerId from
                    # the seat context, not the report body): derived from the
                    # advertiser and deliberately shaped so no reader mistakes
                    # it for a real TTD PartnerId.
                    "partner_id": f"seed partner {advertiser_id}",
                    "advertiser_id": advertiser_id,
                    "campaign_id": str(canonical.get("campaign_id") or ""),
                    # daily_performance does NOT select CampaignName /
                    # AdGroupName / CreativeId, so the connector itself lands ''
                    # on these rows. Empty is the faithful value here, not a
                    # placeholder for something we failed to derive.
                    "campaign_name": "",
                    "ad_group_id": str(canonical.get("ad_group_id") or ""),
                    "ad_group_name": "",
                    "creative_id": "",
                    # No non-key dimension was selected, so there is no segment
                    # to carry. NULL, which the staging COALESCEs into the grain.
                    "segments_json": None,
                    "metric": metric,
                    "value_num": float(value),
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
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["date"], row["report_template"], row["partner_id"],
                    row["advertiser_id"], row["campaign_id"], row["campaign_name"],
                    row["ad_group_id"], row["ad_group_name"], row["creative_id"],
                    row["segments_json"], row["metric"], row["value_num"],
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

    `days` is accepted and unused: the report dates come from the golden
    fixture, which is what pins the seed to the connector's real response.
    Sliding them onto a rolling window would mean inventing days the fixture
    never observed. Taking the argument keeps every loader callable the same
    way, which is what lets the fixture discover them instead of hardcoding
    each one.
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
    print(f"thetradedesk seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
