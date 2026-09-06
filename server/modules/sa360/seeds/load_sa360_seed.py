"""SA360 daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_sa360_daily`` LONG rows into a DuckDB file so
``stg_sa360_daily`` can build locally. Without it the model exists and nothing
has ever produced a row through it: the connector counts as "covered" while the
warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- long-ified exactly as ``connector._land()`` does:
one row per (grain, metric), ``dimensions_json`` as canonical sorted-key JSON
over EVERY declared dimension of the profile, ``resource_id`` taken from the
first dimension whose field id ends in ``.id``, and ``*_micros`` divided by 1e6
once while ``provider_value`` keeps the string SA360 actually returned.

THE FIXTURE IS FIELD-ID KEYED, NOT NESTED. ``_land()`` walks dotted source paths
over the live searchAds360 response (``customer`` -> ``id``); the fixture records
the same values already resolved to field ids (``"customer.id"``). The seed reads
them directly rather than re-navigating a nesting the fixture does not have.

MAILLE. ``stg_sa360_full_grain_unique`` keys on
``project_id|report_profile|customer_id|resource_id|date|dimensions_json|metric``
-- SEVEN parts, of which ``dimensions_json`` alone carries the device split. The
seed emits one row per metric of the fixture's single reported grain; collapsing
any of those parts would make it pass a test the real pull would fail.

A METRIC THE FIXTURE DOES NOT CARRY LANDS NO ROW. ``campaign_daily`` declares
``metrics.conversions``, which this pull did not return. AD-9: absence stays
distinguishable from a recorded zero -- and inventing a value here would be
inventing SA360 data.

Two columns are pull CONTEXT, not payload: ``query_hash`` is visibly synthetic
(no SAQL was ever issued for a seed) and ``request_id`` is NULL (no HTTP request
happened). ``manager_customer_id`` and ``currency_code`` land as the empty
string, which is what ``_land()`` writes when the pull carries neither.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/sa360/seeds/load_sa360_seed.py \
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

RAW_TABLE = "raw_sa360_daily"

#: The manifest report whose declared dimensions the fixture matches: it carries
#: campaign.id and no adGroup.id. Our own profile id, not a provider value.
_REPORT_PROFILE = "campaign_daily"

#: `connector._land()`'s non-additive rule, verbatim: a share, a rate, an
#: average or a ratio does not sum across a grain (AD-4).
_NON_ADDITIVE_TOKENS = ("share", "rate", "average", "ratio")

#: No SAQL was issued to produce a seed, so the provenance columns say exactly
#: that instead of imitating a hash and a request id.
_SEED_QUERY_HASH = f"seed {_REPORT_PROFILE} no-query"

#: Exactly the columns `stg_sa360_daily.sql` selects -- it is a `SELECT *`, so
#: the raw table IS the model's column list. Mirrors `connector._RAW_DDL`;
#: declared here so a contract change breaks the load loudly instead of
#: producing a view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    report_profile       VARCHAR,
    manager_customer_id  VARCHAR,
    customer_id          VARCHAR,
    resource_id          VARCHAR,
    date                 VARCHAR,
    campaign_id          VARCHAR,
    ad_group_id          VARCHAR,
    criterion_id         VARCHAR,
    device               VARCHAR,
    currency_code        VARCHAR,
    dimensions_json      VARCHAR,
    metric               VARCHAR,
    value                DOUBLE,
    provider_value       VARCHAR,
    non_additive         BOOLEAN,
    query_hash           VARCHAR,
    request_id           VARCHAR,
    pull_id              VARCHAR,
    loaded_at            VARCHAR,
    project_id           VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _profile_fields() -> tuple[list[str], list[str]]:
    """Return (declared dimensions, declared metrics) of the seeded profile.

    Read from the manifest so the dimension ORDER is the connector's -- it
    decides which id becomes `resource_id`.
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    report = next(
        item
        for item in manifest["source_capabilities"]["reports"]
        if item["id"] == _REPORT_PROFILE
    )
    return list(report["dimensions"]), list(report["metrics"])


def generate_rows(project_id: str = "default") -> list[dict]:
    """Long-ify the golden pull into one row per (grain, metric)."""
    dimension_fields, metric_fields = _profile_fields()
    rows: list[dict] = []
    for provider_row in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        # Every declared dimension is keyed, present or not: a dimension the
        # pull returned empty is part of the grain as a NULL, exactly as
        # `_extract_path` leaves it.
        dims = {field: provider_row.get(field) for field in dimension_fields}
        resource_id = next(
            (
                str(value)
                for key, value in dims.items()
                if key.endswith(".id") and value is not None
            ),
            "",
        )
        dimensions_json = json.dumps(dims, sort_keys=True)
        for metric in metric_fields:
            provider_value = provider_row.get(metric)
            if provider_value is None:
                continue  # AD-9: a metric the pull did not return lands no row.
            value = float(provider_value)
            if metric.endswith("_micros"):
                value /= 1_000_000
            rows.append(
                {
                    "report_profile": _REPORT_PROFILE,
                    # The manager (login) customer and the currency are pull
                    # context; this pull carried neither, and `_land()` writes
                    # the empty string for both in that case.
                    "manager_customer_id": "",
                    "customer_id": str(dims.get("customer.id") or ""),
                    "resource_id": resource_id,
                    "date": dims.get("segments.date"),
                    "campaign_id": dims.get("campaign.id"),
                    "ad_group_id": dims.get("adGroup.id"),
                    "criterion_id": dims.get("adGroupCriterion.criterionId"),
                    "device": dims.get("segments.device"),
                    "currency_code": "",
                    "dimensions_json": dimensions_json,
                    "metric": metric,
                    "value": value,
                    # The string SA360 returned, kept beside the converted
                    # number so the micros division stays auditable (AD-9).
                    "provider_value": str(provider_value),
                    "non_additive": any(token in metric for token in _NON_ADDITIVE_TOKENS),
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
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["report_profile"], row["manager_customer_id"], row["customer_id"],
                    row["resource_id"], row["date"], row["campaign_id"], row["ad_group_id"],
                    row["criterion_id"], row["device"], row["currency_code"],
                    row["dimensions_json"], row["metric"], row["value"],
                    row["provider_value"], row["non_additive"], _SEED_QUERY_HASH,
                    None, pull_id, loaded_at, row["project_id"],
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
    and widening the window would mean fabricating SA360 rows. Taking the
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
    print(f"sa360 seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
