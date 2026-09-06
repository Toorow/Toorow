"""Taboola seed loader (KPI + campaign history) -- Story 53.10.

Lands a deterministic set of ``raw_taboola_daily`` LONG rows AND
``raw_taboola_history`` rows into a DuckDB file so BOTH of the connector's
staging models -- ``stg_taboola_daily`` and ``stg_taboola_history`` -- can build
locally. Seeding only the KPI table would leave the history model with no source
and an unselected ``dbt run`` still red, which is the same "covered on paper"
gap the seed exists to close (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. KPI rows derive from
``tests/fixtures/golden_pull.json`` and history rows from
``tests/fixtures/golden_events.json`` -- the two fixtures the conformance suite
asserts the pulls against. Metrics land under their PROVIDER ids (``spent``, not
``cost``), because that is what ``_land_kpi()`` writes: the canonical rename
happens in ``transform()``, downstream of this table.

RE-RUNNING IS SAFE ON BOTH HALVES, and this loader PROVES it on the history
half. Until 2026-08-30 ``stg_taboola_history`` was a plain ``SELECT *`` over an
append-only table with a ``unique`` test on ``record_id``, so loading this seed
twice into the same DuckDB file went red -- the model, not the loader, was the
defect (execution-substrate: "a retried unit is not idempotent"). The model now
supersedes on ``pull_id DESC`` per (project_id, account_id, record_id) like the
53 other module stagings, so ``run()`` deliberately lands every row TWICE,
under two pull ids: the fixture has to CONTAIN the retry, or
``tests/test_taboola_history_supersedes_a_repull.sql`` would be measuring
nothing and would pass again on a bare ``SELECT *``. Nothing is invented -- it
is the same golden records, landed the way a retried task lands them, and the
landing stays append-only (AD-7): no DELETE, no UPDATE.

Usage:
    uv run python server/modules/taboola/seeds/load_taboola_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
_KPI_FIXTURE = _FIXTURES / "golden_pull.json"
_HISTORY_FIXTURE = _FIXTURES / "golden_events.json"

KPI_TABLE = "raw_taboola_daily"
HISTORY_TABLE = "raw_taboola_history"

#: The report/dimension pairing the connector uses for the campaign_summary
#: profile, and the one the golden pull's columns match. Both are part of the
#: KPI supersede grain, so neither is a free label.
KPI_REPORT = "campaign_summary"
KPI_DIMENSION = "campaign_breakdown"

#: The dimension the connector uses for the campaign_history profile.
HISTORY_DIMENSION = "by_campaign"

#: Keys the connector treats as DIMENSIONS when it builds breakdown_json;
#: everything else numeric in a row is a metric to unpivot.
_DIMENSION_KEYS = frozenset(
    {
        "date", "campaign_id", "campaign_name", "item_id", "item_name",
        "site_id", "site_name", "country", "platform", "update_time",
    }
)

#: The connector's non-additivity heuristic, verbatim. A ratio column must never
#: be summed downstream, and the flag is how the warehouse knows.
_NON_ADDITIVE_TOKENS = ("rate", "ctr", "roas", "cpc", "cpm", "cpa", "reach", "frequency")

#: Exactly the columns each raw table carries, in the connector's landing order.
#: Both staging models are `SELECT *`, so this order IS their column order; a
#: mismatch would silently shift every value one column left.
_KPI_DDL = f"""
CREATE TABLE IF NOT EXISTS {KPI_TABLE} (
 account_id VARCHAR, report VARCHAR, dimension VARCHAR, date VARCHAR, campaign_id VARCHAR,
 item_id VARCHAR, site_id VARCHAR, breakdown_json VARCHAR, metric VARCHAR, value DOUBLE,
 non_additive BOOLEAN, timezone VARCHAR, currency VARCHAR, update_time VARCHAR,
 pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
)
"""

_HISTORY_DDL = f"""
CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (
 account_id VARCHAR, dimension VARCHAR, record_id VARCHAR, change_time VARCHAR,
 change_type VARCHAR, entity_id VARCHAR, payload_json VARCHAR, pull_id VARCHAR,
 loaded_at VARCHAR, project_id VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _canonical_json(payload: dict) -> str:
    """Sorted-key, separator-tight JSON -- the connector's exact encoding.

    Sorted: a breakdown key that depends on response ordering would churn the
    supersede grain without any schema change.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _numeric(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def generate_kpi_rows(project_id: str = "default") -> list[dict]:
    """Unpivot the golden pull into one row per (grain, metric).

    That IS the model's grain -- `stg_taboola_daily` supersedes on
    `project_id|account_id|report|dimension|date|campaign_id|item_id|site_id|
    breakdown_json|metric` -- so producing anything coarser would make the seed
    pass a test the real pull would fail.
    """
    rows: list[dict] = []
    for report_row in json.loads(_KPI_FIXTURE.read_text(encoding="utf-8")):
        breakdown = {
            key: value for key, value in report_row.items() if key in _DIMENSION_KEYS
        }
        for metric, provider_value in report_row.items():
            if metric in _DIMENSION_KEYS or metric == "account_id":
                continue
            value = _numeric(provider_value)
            if value is None:
                # NULL honnete (AD-9): a non-numeric column is not a metric and
                # lands NO row, rather than a coerced zero.
                continue
            rows.append(
                {
                    "account_id": str(report_row.get("account_id") or ""),
                    "report": KPI_REPORT,
                    "dimension": KPI_DIMENSION,
                    "date": str(report_row.get("date") or ""),
                    "campaign_id": str(report_row.get("campaign_id") or ""),
                    # campaign_breakdown reports above the item and site grains,
                    # so the connector lands '' on both -- part of the grain key.
                    "item_id": str(report_row.get("item_id") or ""),
                    "site_id": str(report_row.get("site_id") or ""),
                    "breakdown_json": _canonical_json(breakdown),
                    "metric": metric,
                    "value": value,
                    "non_additive": any(
                        token in metric.lower() for token in _NON_ADDITIVE_TOKENS
                    ),
                    # Both come from the ACCOUNT selection, not from the report
                    # body; the fixture carries no selection, and '' is what the
                    # connector lands in that case (context.get(..., "")).
                    "timezone": "",
                    "currency": "",
                    "update_time": str(report_row.get("update_time") or ""),
                    "project_id": project_id,
                }
            )
    return rows


def generate_history_rows(project_id: str = "default") -> list[dict]:
    """One row per Backstage history record, which is that model's grain.

    `stg_taboola_history` tests `record_id` unique: the record id IS the
    provider's immutable identity for a change, and it is what dedups a re-pull
    of an overlapping window.
    """
    rows: list[dict] = []
    for record in json.loads(_HISTORY_FIXTURE.read_text(encoding="utf-8")):
        rows.append(
            {
                "account_id": str(record.get("account_id") or ""),
                "dimension": HISTORY_DIMENSION,
                "record_id": str(record.get("record_id") or ""),
                "change_time": str(record.get("change_time") or ""),
                "change_type": str(record.get("change_type") or ""),
                "entity_id": str(record.get("entity_id") or ""),
                # The connector keeps the WHOLE record as evidence, so a field
                # it does not model yet is preserved rather than dropped.
                "payload_json": _canonical_json(record),
                "project_id": project_id,
            }
        )
    return rows


def load_duckdb(
    kpi_rows: list[dict],
    history_rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
) -> int:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_KPI_DDL)
        con.execute(_HISTORY_DDL)
        con.executemany(
            f"INSERT INTO {KPI_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["account_id"], row["report"], row["dimension"], row["date"],
                    row["campaign_id"], row["item_id"], row["site_id"],
                    row["breakdown_json"], row["metric"], row["value"],
                    row["non_additive"], row["timezone"], row["currency"],
                    row["update_time"], pull_id, loaded_at, row["project_id"],
                )
                for row in kpi_rows
            ],
        )
        con.executemany(
            f"INSERT INTO {HISTORY_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["account_id"], row["dimension"], row["record_id"],
                    row["change_time"], row["change_type"], row["entity_id"],
                    row["payload_json"], pull_id, loaded_at, row["project_id"],
                )
                for row in history_rows
            ],
        )
    finally:
        con.close()
    return len(kpi_rows) + len(history_rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: the report dates and change times come from
    the golden fixtures, which is what pins the seed to the connector's real
    responses. Widening the window would mean inventing days the fixtures never
    observed. Taking the argument keeps every loader callable the same way,
    which is what lets the fixture discover them instead of hardcoding each one.

    EVERY ROW LANDS TWICE, under two pull ids -- the retry both staging models
    must survive, and the same window a retried Cloud Task re-pulls. The golden
    fixtures carry 1 report row and 2 history records, so the second landing
    costs nothing and buys the only fixture in which the supersede is
    observable at all: with a single pull, a bare `SELECT *` and a QUALIFY are
    indistinguishable, and a test written over it would measure nothing.

    `sorted()` rather than two successive `ULID()` reads in order: two ULIDs
    minted inside the same millisecond carry independent random tails, so their
    order is NOT guaranteed, and a test asserting "the latest pull wins" has to
    know which one that is. Sorting names it.
    """
    del days
    first_pull, second_pull = sorted((_mint_pull_id(), _mint_pull_id()))
    loaded_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    kpi_rows = generate_kpi_rows(project_id=project_id)
    history_rows = generate_history_rows(project_id=project_id)
    landed = load_duckdb(kpi_rows, history_rows, first_pull, loaded_at, duckdb_path)
    landed += load_duckdb(kpi_rows, history_rows, second_pull, loaded_at, duckdb_path)
    # The LATEST pull: the one a reader finds in both staging models.
    return second_pull, landed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", required=True)
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()
    pull_id, count = run(duckdb_path=args.duckdb_path, project_id=args.project_id)
    print(f"taboola seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
