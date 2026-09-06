"""Amazon Ads LONG-landing seed loader -- Story 53.10.

Lands a deterministic set of ``raw_amazon_ads_daily`` rows into a DuckDB file
so ``stg_amazon_ads_daily`` can build locally. Without it the model exists and
nothing has ever produced a row through it: the connector counts as "covered"
while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against. The metric ids are the manifest's
``canonical_metric_mapping`` (unitsSold -> units_sold, purchases14d ->
purchases_14d, ...) because the landing stores canonical ids, never the v3
column names. ``campaignStatus`` lands in ``attributes_json`` under its
post-transform name, which is Story 26.6 Part A: a mutable entity attribute
must ride along with the superseded row instead of splitting the grain.

TWO COLUMNS COULD NOT BE MADE VISIBLY SYNTHETIC (AC5), and this is deliberate:
``ad_product`` and ``region`` both carry an ``accepted_values`` test, so the
only values the model admits are provider enums. They are derived from the
fixture's own campaign names -- "SP - Brand - FR" / "SB - Store Spotlight - DE"
give the ad product, and FR/DE place both campaigns in the EU region. The
mapping is explicit and raises on an unknown prefix, so a fixture change fails
the load instead of guessing. Everything else the fixture does not carry is
either empty (the connector's own convention for "above this grain") or an
obvious ``seed-`` marker (``profile_id``, ``data_level``).

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The fixture's own ``pull_id`` is ignored -- reusing it would make
two loads collide instead of supersede.

Usage:
    uv run python server/modules/amazon-ads/seeds/load_amazon_ads_seed.py \
        --duckdb-path server/modules/amazon-ads/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_amazon_ads_daily"

#: Exactly the columns `stg_amazon_ads_daily.sql` selects, in that order.
#: Declared here so a model change breaks the load loudly instead of producing
#: a view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date                  VARCHAR,
    data_level            VARCHAR,
    ad_product            VARCHAR,
    region                VARCHAR,
    profile_id            VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    ad_group_id           VARCHAR,
    ad_group_name         VARCHAR,
    ad_id                 VARCHAR,
    keyword_id            VARCHAR,
    search_term           VARCHAR,
    advertised_asin       VARCHAR,
    purchased_asin        VARCHAR,
    segments_json         VARCHAR,
    attributes_json       VARCHAR,
    metric                VARCHAR,
    value_num             DOUBLE,
    cost_source_currency  VARCHAR,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""

_INSERT_SQL = f"""
INSERT INTO {RAW_TABLE}
    (date, data_level, ad_product, region, profile_id, campaign_id, campaign_name,
     ad_group_id, ad_group_name, ad_id, keyword_id, search_term, advertised_asin,
     purchased_asin, segments_json, attributes_json, metric, value_num,
     cost_source_currency, pull_id, loaded_at, project_id)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

#: manifest.json canonical_metric_mapping. The landing stores canonical ids
#: (schema.yml: "catalog field_id after manifest rename"), so a seed that kept
#: the v3 spelling would put rows in the mart no metric definition can find.
_CANONICAL_METRICS = {
    "impressions": "impressions",
    "clicks": "clicks",
    "cost": "cost",
    "purchases14d": "purchases_14d",
    "purchases30d": "purchases_30d",
    "sales14d": "sales_14d",
    "sales30d": "sales_30d",
    "purchases": "purchases",
    "sales": "sales",
    "unitsSold": "units_sold",
    "viewableImpressions": "viewable_impressions",
}

#: (reportTypeId, adProduct) per campaign-name prefix, taken from
#: connector._PROFILE_SPECS and catalog_sources report_type_compatibility --
#: our own declared pairing, not a guess about what Amazon would send.
_PRODUCT_BY_PREFIX = {
    "SP": ("spCampaigns", "SPONSORED_PRODUCTS"),
    "SB": ("sbCampaigns", "SPONSORED_BRANDS"),
}

#: Fixture keys that are structure or envelope, never a metric.
_NON_METRIC_KEYS = frozenset(
    {"pull_id", "connector", "date", "campaignId", "campaignName", "campaignStatus",
     "campaignBudgetCurrencyCode"}
)

#: The 3 API regions are isolated silos and `region` carries an accepted_values
#: test: NA|EU|FE and nothing else. Both fixture campaigns name European
#: markets (FR, DE), so EU is the region the fixture itself points at.
_REGION = "EU"

#: An advertiser x marketplace id Amazon issues. Absent from the fixture, and
#: unconstrained by any test -- so it says what it is.
_PROFILE_ID = f"seed-profile-{_REGION}"


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def generate_rows(project_id: str = "default") -> list[dict]:
    """Long-ify the golden pull into one row per (campaign, metric).

    That IS the model's grain -- the uniqueness test spans the full key down to
    (segments_json, metric) -- so producing anything coarser would make the seed
    pass a test the real pull would fail. Two campaigns x 7 metrics each.
    """
    rows: list[dict] = []
    for item in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        campaign_id = str(item.get("campaignId") or "")
        campaign_name = str(item.get("campaignName") or "")
        prefix = campaign_name.split(" - ")[0].strip()
        if prefix not in _PRODUCT_BY_PREFIX:
            raise ValueError(
                f"amazon-ads seed: campaign {campaign_id!r} has no ad-product prefix "
                f"({campaign_name!r}); ad_product carries an accepted_values test and "
                "must not be guessed"
            )
        data_level, ad_product = _PRODUCT_BY_PREFIX[prefix]
        status = item.get("campaignStatus")
        # Story 26.6 Part A: mutable entity state lands OUT of the grain, under
        # its post-transform name (canonical_dimension_mapping campaignStatus).
        attributes_json = (
            json.dumps({"campaign_status": str(status)}, sort_keys=True)
            if status is not None
            else None
        )
        for provider_metric, value in item.items():
            if provider_metric in _NON_METRIC_KEYS:
                continue
            if provider_metric not in _CANONICAL_METRICS:
                raise ValueError(
                    f"amazon-ads seed: metric {provider_metric!r} is absent from the "
                    "manifest canonical_metric_mapping; add the rename there rather "
                    "than inventing a canonical id here"
                )
            rows.append(
                {
                    "date": str(item.get("date") or ""),
                    # The v3 reportTypeId the row would have come from. No test
                    # constrains it, so it stays derived from the fixture prefix.
                    "data_level": data_level,
                    "ad_product": ad_product,
                    "region": _REGION,
                    "profile_id": _PROFILE_ID,
                    "campaign_id": campaign_id,
                    "campaign_name": campaign_name,
                    # Empty above their own grain -- the connector's convention
                    # (schema.yml: "'' on rows whose grain does not carry it").
                    "ad_group_id": "",
                    "ad_group_name": "",
                    "ad_id": "",
                    "keyword_id": "",
                    "search_term": "",
                    "advertised_asin": "",
                    "purchased_asin": "",
                    # NULL: the fixture selected no non-key dimension. The
                    # staging QUALIFY COALESCEs it, so NULL keys cleanly.
                    "segments_json": None,
                    "attributes_json": attributes_json,
                    "metric": _CANONICAL_METRICS[provider_metric],
                    "value_num": float(value),
                    "cost_source_currency": str(
                        item.get("campaignBudgetCurrencyCode") or ""
                    ),
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
            _INSERT_SQL,
            [
                (
                    row["date"], row["data_level"], row["ad_product"], row["region"],
                    row["profile_id"], row["campaign_id"], row["campaign_name"],
                    row["ad_group_id"], row["ad_group_name"], row["ad_id"],
                    row["keyword_id"], row["search_term"], row["advertised_asin"],
                    row["purchased_asin"], row["segments_json"], row["attributes_json"],
                    row["metric"], row["value_num"], row["cost_source_currency"],
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
    invent days the connector never returned -- and Amazon restates conversions
    for up to 42 days, so a fabricated window would also fabricate a
    restatement history. Taking the argument keeps every loader callable the
    same way, which is what lets the fixture discover them instead of
    hardcoding each one.
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
    print(f"amazon-ads seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
