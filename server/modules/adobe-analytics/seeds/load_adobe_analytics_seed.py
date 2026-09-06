"""Adobe Analytics seed loader -- Story 53.10.

Lands a deterministic set of ``raw_adobe_analytics_daily`` rows into a DuckDB
file so ``stg_adobe_analytics_daily`` can build locally. Without it the model
exists and nothing has ever produced a row through it: the connector counts as
"covered" while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against. The staging model is ``SELECT *``, so the DDL below
is a copy of ``connector._RAW_DDL``: any column dropped here would vanish from
the model without a single test noticing.

Values the fixture does not carry are NOT dressed as Adobe data (AC5):
``global_company_id``, ``dimension`` and ``request_hash`` are derived from the
ids and are unmistakably synthetic; ``report_suite_timezone``, ``unit`` and
``parent_item_id`` are empty because that is exactly what ``connector._land``
writes when the caller supplies none, and ``segment_ids`` is ``"[]"`` for the
same reason. ``non_additive`` is recomputed with the connector's own token
rule, so the seed states the same thing about ``visits`` that a real pull does.
``report_profile`` is our own declared profile name (``pull_daily_kpi``), not a
provider value.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/adobe-analytics/seeds/load_adobe_analytics_seed.py \
        --duckdb-path server/modules/adobe-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_adobe_analytics_daily"

#: A copy of connector._RAW_DDL, column for column and in the same order: the
#: staging model is `SELECT *`, so this table IS the model's column list.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    report_profile        VARCHAR,
    global_company_id     VARCHAR,
    rsid                  VARCHAR,
    report_suite_timezone VARCHAR,
    date                  VARCHAR,
    dimension             VARCHAR,
    item_id               VARCHAR,
    item_value            VARCHAR,
    parent_item_id        VARCHAR,
    segment_ids           VARCHAR,
    metric                VARCHAR,
    value                 DOUBLE,
    unit                  VARCHAR,
    non_additive          BOOLEAN,
    partial               BOOLEAN,
    partial_errors        VARCHAR,
    request_hash          VARCHAR,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""

_INSERT_SQL = f"""
INSERT INTO {RAW_TABLE}
    (report_profile, global_company_id, rsid, report_suite_timezone, date, dimension,
     item_id, item_value, parent_item_id, segment_ids, metric, value, unit, non_additive,
     partial, partial_errors, request_hash, pull_id, loaded_at, project_id)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

#: connector._land's own rule. A metric whose id carries one of these tokens is
#: a de-duplicated count or a ratio: additive over rows it is not.
_NON_ADDITIVE_TOKENS = ("visit", "visitor", "unique", "rate", "average", "ratio")

#: Keys of the fixture row that describe the ROW, not a measured value.
#: Everything else is a metric -- so a fixture gaining a metric gains a seed row
#: instead of being silently ignored.
_STRUCTURAL_KEYS = frozenset({"date", "rsid", "item_id", "item_value"})

#: The connector's default reporting profile (connector.pull_daily_kpi). Our own
#: vocabulary, not something Adobe returned.
_REPORT_PROFILE = "daily_kpi"


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def generate_rows(project_id: str = "default") -> list[dict]:
    """Flatten the golden pull into one row per (item, metric).

    That IS the model's grain -- the uniqueness test spans
    (project_id, report_profile, global_company_id, rsid, date, dimension,
    item_id, parent_item_id, segment_ids, metric) -- so producing anything
    coarser would make the seed pass a test the real pull would fail.
    """
    rows: list[dict] = []
    for item in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        rsid = str(item.get("rsid") or "")
        for metric, provider_value in item.items():
            if metric in _STRUCTURAL_KEYS:
                continue
            rows.append(
                {
                    "report_profile": _REPORT_PROFILE,
                    # Adobe issues this id; the fixture does not carry it. Derived
                    # from the rsid and shaped so no reader mistakes it for one.
                    "global_company_id": f"seed-company-{rsid}",
                    "rsid": rsid,
                    # Empty is what _land writes when the caller passes no
                    # timezone -- an invented one would claim knowledge we lack.
                    "report_suite_timezone": "",
                    "date": str(item.get("date") or ""),
                    # The fixture does not say which dimension was requested, and
                    # a plausible "variables/..." id would be a fabrication.
                    "dimension": "seed-dimension",
                    "item_id": str(item.get("item_id") or ""),
                    "item_value": str(item.get("item_value") or ""),
                    # Top-level item: no rowContainer, so no parent (as _land).
                    "parent_item_id": "",
                    # json.dumps([]) -- what _land writes with no segment filter.
                    "segment_ids": "[]",
                    "metric": metric,
                    "value": float(provider_value),
                    # No monetary metric here, so no currency unit (as _land).
                    "unit": "",
                    "non_additive": any(
                        token in metric.lower() for token in _NON_ADDITIVE_TOKENS
                    ),
                    "partial": False,
                    "partial_errors": "[]",
                    # A sha256 here would be indistinguishable from a real
                    # request hash; this one announces that no request was made.
                    "request_hash": f"seed-request-{rsid}",
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
                    row["report_profile"], row["global_company_id"], row["rsid"],
                    row["report_suite_timezone"], row["date"], row["dimension"],
                    row["item_id"], row["item_value"], row["parent_item_id"],
                    row["segment_ids"], row["metric"], row["value"], row["unit"],
                    row["non_additive"], row["partial"], row["partial_errors"],
                    row["request_hash"], pull_id, loaded_at, row["project_id"],
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
    print(f"adobe-analytics seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
