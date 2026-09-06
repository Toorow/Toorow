"""Amazon DSP seed loader -- Story 53.10.

Lands a deterministic set of ``raw_amazon_dsp_daily`` rows into a DuckDB file
so ``stg_amazon_dsp_daily`` can build locally. Without it the model exists and
nothing has ever produced a row through it: the connector counts as "covered"
while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against. The staging model is ``SELECT *``, so the DDL below
is a copy of ``connector._RAW_DDL``: any column dropped here would vanish from
the model without a single test noticing. ``provider_value`` keeps the
fixture's own string, which is the point of that column: what the API sent,
before we coerced it.

``dimensions_json`` carries ``date`` alongside ``advertiserId`` because that is
what ``connector._land`` produces -- both are in the default ``columns``
selection and neither is numeric, so both land as dimensions. It looks
redundant next to the ``date`` column; it is what the connector does, and a
prettier seed would be a seed that proves nothing.

Values the fixture does not carry are NOT dressed as Amazon data (AC5):
``region``, ``ads_account_id`` and ``request_hash`` are derived from the ids
and are unmistakably synthetic. ``report_type`` and ``group_by`` are the
connector's own declared defaults (``dspCampaign`` / ``campaign``), not
provider values. ``non_additive`` is recomputed with the connector's own token
rule.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/amazon-dsp/seeds/load_amazon_dsp_seed.py \
        --duckdb-path server/modules/amazon-dsp/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_amazon_dsp_daily"

#: A copy of connector._RAW_DDL, column for column and in the same order: the
#: staging model is `SELECT *`, so this table IS the model's column list.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    region           VARCHAR,
    ads_account_id   VARCHAR,
    advertiser_id    VARCHAR,
    report_type      VARCHAR,
    group_by         VARCHAR,
    date             VARCHAR,
    dimensions_json  VARCHAR,
    metric           VARCHAR,
    value            DOUBLE,
    provider_value   VARCHAR,
    non_additive     BOOLEAN,
    request_hash     VARCHAR,
    pull_id          VARCHAR,
    loaded_at        VARCHAR,
    project_id       VARCHAR
)
"""

_INSERT_SQL = f"""
INSERT INTO {RAW_TABLE}
    (region, ads_account_id, advertiser_id, report_type, group_by, date,
     dimensions_json, metric, value, provider_value, non_additive, request_hash,
     pull_id, loaded_at, project_id)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

#: connector._land's own rule for what must never be summed (AD-4).
_NON_ADDITIVE_TOKENS = ("rate", "ratio", "average", "reach", "frequency", "roas")

#: The connector's default `columns` selection (connector._pull_profile). The
#: fixture is that selection, which is why every key below appears in it.
_DIMENSION_KEYS = ("date", "advertiserId")

#: connector._pull_profile defaults. Our own request vocabulary, not something
#: the API returned.
_REPORT_TYPE = "dspCampaign"
_GROUP_BY = "campaign"


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def generate_rows(project_id: str = "default") -> list[dict]:
    """Long-ify the golden pull into one row per (advertiser, day, metric).

    That IS the model's grain -- the uniqueness test spans (project_id, region,
    ads_account_id, advertiser_id, report_type, group_by, date, dimensions_json,
    metric) -- so producing anything coarser would make the seed pass a test the
    real pull would fail.

    The fixture quotes its numbers (it mirrors a downloaded report, not the
    in-memory response), so the metric columns are coerced here. `_land` splits
    metric from dimension on the Python type, which means the split has to be
    restated rather than replayed; `_DIMENSION_KEYS` is that restatement.
    """
    rows: list[dict] = []
    for item in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        advertiser_id = str(item.get("advertiserId") or "")
        dimensions = {
            key: str(item[key]) for key in _DIMENSION_KEYS if item.get(key) is not None
        }
        dimensions_json = json.dumps(dimensions, sort_keys=True, separators=(",", ":"))
        for metric, provider_value in item.items():
            if metric in _DIMENSION_KEYS:
                continue
            rows.append(
                {
                    # The 3 API hosts are isolated silos; the fixture names none
                    # of them, so this one says it is a seed rather than picking.
                    "region": "seed-region",
                    "ads_account_id": f"seed-account-{advertiser_id}",
                    "advertiser_id": advertiser_id,
                    "report_type": _REPORT_TYPE,
                    "group_by": _GROUP_BY,
                    "date": str(item.get("date") or ""),
                    "dimensions_json": dimensions_json,
                    "metric": metric,
                    "value": float(provider_value),
                    # What the provider sent, before coercion -- the fixture's
                    # own string, untouched.
                    "provider_value": str(provider_value),
                    "non_additive": any(
                        token in metric.lower() for token in _NON_ADDITIVE_TOKENS
                    ),
                    # A sha256 here would be indistinguishable from a real
                    # request hash; this one announces that no request was made.
                    "request_hash": f"seed-request-{advertiser_id}",
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
                    row["region"], row["ads_account_id"], row["advertiser_id"],
                    row["report_type"], row["group_by"], row["date"],
                    row["dimensions_json"], row["metric"], row["value"],
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
    print(f"amazon-dsp seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
