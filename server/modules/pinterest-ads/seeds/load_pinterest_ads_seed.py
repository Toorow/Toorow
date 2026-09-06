"""Pinterest Ads daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_pinterest_ads_daily`` LONG rows into a DuckDB
file so ``stg_pinterest_ads_daily`` can build locally. Without it the model
exists and nothing has ever produced a row through it: the connector counts as
"covered" while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- put through the two steps the connector applies
before landing: ``transform()`` renames the catalog enum tokens with the manifest
mapping and divides the micro-currency columns by 1e6 exactly once, then
``_insert_raw_rows()`` long-ifies each wide row into one row per metric.

THE SEGMENTS / ATTRIBUTES SPLIT IS THE POINT (story 26.6 Part A). Entity
STATE/TYPE values -- statuses, objective and budget types -- land in
``attributes_json``, which the staging keeps OUT of its partition, while
grain-bearing breakdowns land in ``segments_json``, which is IN it. Put a
mutable status in the grain and a refetch that re-reads a window with a changed
status stops superseding the earlier row: the window's metrics DOUBLE. The split
is read from ``catalog_sources/catalog_sources.json`` (the generated
``descriptive_mutable`` block), never guessed from a name.

``data_level`` is derived from the most specific structural id the fixture row
carries, which is what the connector's per-profile stamp encodes: the row with
an ad group id is AD_GROUP, the one without is CAMPAIGN. Landing both under one
marker would let a campaign-grain row and an ad-group-grain row collide and
double-count inside one series (story 3.6 lesson).

``ad_account_id`` is the ONE column the fixture cannot supply: it is the
topology selection, not part of the report payload. It is a visibly synthetic
constant -- never dressed as a Pinterest account id.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/pinterest-ads/seeds/load_pinterest_ads_seed.py \
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
_API_CATALOG = _MODULE_DIR / "api_catalog.json"
_CATALOG_SOURCES = _MODULE_DIR / "catalog_sources" / "catalog_sources.json"

RAW_TABLE = "raw_pinterest_ads_daily"

#: `connector._STRUCTURE_COLUMNS`: the dedicated grain columns. Note that
#: ad_group_name is NOT one of them -- it lands in segments_json, as it does in
#: the connector.
_STRUCTURE_COLUMNS = (
    "date", "ad_account_id", "campaign_id", "campaign_name", "ad_group_id",
    "ad_id", "pin_id", "product_group_id",
)
_NON_SEGMENT_KEYS = frozenset(_STRUCTURE_COLUMNS) | {"pull_id", "connector", "data_level"}

#: `connector._MICRO_MARKER`: the conversion set comes FROM THE CATALOG, never
#: from an id-suffix heuristic (26.2 review lesson).
_MICRO_MARKER = "value/1e6"

#: Most specific structural id first -> the report grain that produced the row.
#: Mirrors the data_level stamps of `connector._PROFILE_SPECS`.
_DATA_LEVEL_BY_ID = (
    ("product_group_id", "PRODUCT_GROUP"),
    ("ad_id", "AD"),
    ("pin_id", "AD"),
    ("ad_group_id", "AD_GROUP"),
    ("campaign_id", "CAMPAIGN"),
)

#: Not in the fixture: the reporting account is the topology SELECTION, not part
#: of the report payload. Deliberately shaped so no reader mistakes it for a
#: Pinterest ad account id.
_SEED_AD_ACCOUNT_ID = "seed-ad-account"

#: Exactly the columns `stg_pinterest_ads_daily.sql` selects, in its order.
#: Declared here so a model change breaks the load loudly instead of producing a
#: view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date              VARCHAR,
    data_level        VARCHAR,
    ad_account_id     VARCHAR,
    campaign_id       VARCHAR,
    campaign_name     VARCHAR,
    ad_group_id       VARCHAR,
    ad_id             VARCHAR,
    pin_id            VARCHAR,
    product_group_id  VARCHAR,
    segments_json     VARCHAR,
    attributes_json   VARCHAR,
    metric            VARCHAR,
    value_num         DOUBLE,
    pull_id           VARCHAR,
    loaded_at         VARCHAR,
    project_id        VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _rename_map() -> dict[str, str]:
    """`connector._rename_map()`: AD-2 renames belong to the manifest, not to code."""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    rename: dict[str, str] = {}
    for token, target in {
        **manifest.get("canonical_metric_mapping", {}),
        **manifest.get("canonical_dimension_mapping", {}),
    }.items():
        rename[token] = target if isinstance(target, str) else target["canonical"]
    return rename


def _metric_names() -> frozenset[str]:
    """The canonical names of everything the manifest declares a METRIC.

    Anything else on a row is a dimension or an entity attribute, and must not
    be melted into a (metric, value_num) pair.
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    return frozenset(
        target if isinstance(target, str) else target["canonical"]
        for target in manifest.get("canonical_metric_mapping", {}).values()
    )


def _micro_field_ids() -> frozenset[str]:
    """`connector._micro_field_ids()`: the catalog stamps which fields are micros."""
    catalog = json.loads(_API_CATALOG.read_text(encoding="utf-8"))
    return frozenset(
        field["field_id"]
        for field in catalog["fields"]
        if _MICRO_MARKER in (field.get("description") or "")
    )


def _descriptive_mutable_names(rename: dict[str, str]) -> frozenset[str]:
    """`connector._descriptive_mutable_landed_names()`: story 26.6 Part A.

    Each enum id expands to BOTH itself AND its canonical landed name, so the
    split works whether a mutable attribute lands under one or the other.
    """
    block = json.loads(_CATALOG_SOURCES.read_text(encoding="utf-8")).get("descriptive_mutable")
    names: set[str] = set()
    for field_id in (block or {}).get("field_ids") or ():
        names.add(field_id)
        names.add(rename.get(field_id, field_id))
    return frozenset(names)


def _data_level(canonical: dict) -> str:
    for column, level in _DATA_LEVEL_BY_ID:
        if canonical.get(column):
            return level
    raise ValueError(f"{_FIXTURE.name}: row carries no structural id, grain undecidable")


def generate_rows(project_id: str = "default") -> list[dict]:
    """Long-ify the golden pull into one row per (grain, metric).

    That IS the model's grain -- `stg_pinterest_ads_daily` is unique on
    `project_id|date|data_level|ad_account_id|campaign_id|ad_group_id|ad_id|
    pin_id|product_group_id|COALESCE(segments_json,'')|metric` -- so producing
    anything coarser would make the seed pass a test the real pull would fail.
    """
    rename = _rename_map()
    metric_names = _metric_names()
    micro_fields = _micro_field_ids()
    mutable_names = _descriptive_mutable_names(rename)

    rows: list[dict] = []
    for wide_row in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        canonical: dict = {}
        for key, value in wide_row.items():
            if key in micro_fields and value is not None:
                value = float(value) / 1e6
            canonical[rename.get(key, key)] = value

        segment_items: dict = {}
        attribute_items: dict = {}
        for key, value in canonical.items():
            if key in _NON_SEGMENT_KEYS or key in metric_names or value is None:
                continue
            if key in mutable_names:
                attribute_items[key] = value
            else:
                segment_items[key] = value
        segments_json = json.dumps(segment_items, sort_keys=True) if segment_items else None
        attributes_json = json.dumps(attribute_items, sort_keys=True) if attribute_items else None

        for metric in sorted(metric_names & set(canonical)):
            value = canonical[metric]
            if value is None:
                continue  # AD-9: an absent metric is NULL, not zero.
            rows.append(
                {
                    "date": str(canonical.get("date", "")),
                    "data_level": _data_level(canonical),
                    "ad_account_id": _SEED_AD_ACCOUNT_ID,
                    "campaign_id": str(canonical.get("campaign_id") or ""),
                    "campaign_name": str(canonical.get("campaign_name") or ""),
                    "ad_group_id": str(canonical.get("ad_group_id") or ""),
                    "ad_id": str(canonical.get("ad_id") or ""),
                    "pin_id": str(canonical.get("pin_id") or ""),
                    "product_group_id": str(canonical.get("product_group_id") or ""),
                    "segments_json": segments_json,
                    "attributes_json": attributes_json,
                    "metric": metric,
                    # Monetary metrics are ALWAYS in currency units here: the
                    # micros division happened once, above.
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
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["date"], row["data_level"], row["ad_account_id"],
                    row["campaign_id"], row["campaign_name"], row["ad_group_id"],
                    row["ad_id"], row["pin_id"], row["product_group_id"],
                    row["segments_json"], row["attributes_json"], row["metric"],
                    row["value_num"], pull_id, loaded_at, row["project_id"],
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: the golden pull pins its own reporting days,
    and widening the window would mean fabricating Pinterest rows. Taking the
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
    print(f"pinterest-ads seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
