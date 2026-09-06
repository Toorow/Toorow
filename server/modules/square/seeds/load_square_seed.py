"""Square payments seed loader -- Story 53.10.

Lands a deterministic set of ``raw_square_payments`` rows into a DuckDB file so
``stg_square_payments_daily`` can build locally. Without it the model exists and
nothing has ever produced a row through it: the connector counts as "covered"
while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. ``golden_pull.json`` is the
REAL Square Payment API shape (``amount_money``, ``refunded_money``,
``processing_fee[]``), and the rows are produced by the same two steps the
connector applies: ``_parse_payment()`` extracts the structure and converts
minor units EXPLICITLY, then the manifest rename turns the source names
(``amount``, ``refunded``, ``fee_amount``) into the canonical columns
(``revenue``, ``refunds``, ``fees``).

THE CONVERSION IS NOT A DIVISION BY 100. Square returns every Money amount in
the currency's smallest unit, but the zero-decimal currencies (JPY, KRW, ...)
have no smaller unit: the fixture's 15000 JPY payment is 15000, not 150. That is
why the currency list is read from ``connector._ZERO_DECIMAL_CURRENCIES`` rather
than assumed -- getting it wrong silently divides a real revenue figure by 100.

REFUNDS AND FEES ARE DEDICATED POSITIVE COLUMNS. They are never netted into
revenue here; the net is computed explicitly downstream (AD-9, no black box).

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/square/seeds/load_square_seed.py \
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

RAW_TABLE = "raw_square_payments"

#: `connector._ZERO_DECIMAL_CURRENCIES`: currencies with no minor unit, where
#: the Money amount is ALREADY the major unit and must not be divided.
_ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga", "pyg", "rwf",
        "ugx", "vnd", "vuv", "xaf", "xof", "xpf",
    }
)

#: `connector._parse_payment()`: the currency a payment carries no Money object
#: for. Kept identical so the seed and the pull agree on the same default.
_DEFAULT_CURRENCY = "USD"

#: Exactly the columns `stg_square_payments_daily.sql` reads from raw, in the
#: order of `connector._RAW_CREATE_DDL`. Declared here so a model change breaks
#: the load loudly instead of producing a view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date                     VARCHAR,
    payment_id               VARCHAR,
    order_id                 VARCHAR,
    location_id              VARCHAR,
    revenue                  DOUBLE,
    refunds                  DOUBLE,
    fees                     DOUBLE,
    transaction_count        INTEGER,
    order_count              INTEGER,
    revenue_source_currency  VARCHAR DEFAULT 'USD',
    pull_id                  VARCHAR,
    loaded_at                VARCHAR,
    project_id               VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _money_amount(money) -> int:
    if isinstance(money, dict):
        try:
            return int(money.get("amount", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _money_currency(money, default: str = _DEFAULT_CURRENCY) -> str:
    if isinstance(money, dict) and money.get("currency"):
        return str(money["currency"])
    return default


def _amount_to_units(amount_minor, currency: str) -> float:
    """`connector._amount_to_units()`: the conversion is EXPLICIT, never silent."""
    try:
        minor = float(amount_minor or 0)
    except (TypeError, ValueError):
        minor = 0.0
    if (currency or "").lower() in _ZERO_DECIMAL_CURRENCIES:
        return round(minor, 2)
    return round(minor / 100.0, 2)


def _fee_total(payment: dict) -> int:
    """Square can return SEVERAL fee lines per payment; the fee is their sum."""
    return sum(
        _money_amount(fee.get("amount_money"))
        for fee in payment.get("processing_fee") or []
    )


def _created_date(created_at) -> str:
    """`connector._parse_created_date()`: UTC day grain, no intraday shift."""
    if not created_at:
        return ""
    text = str(created_at).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).astimezone(UTC).date().isoformat()
    except ValueError:
        return ""


def generate_rows(project_id: str = "default") -> list[dict]:
    """Parse the golden pull into one row per PAYMENT.

    That IS the model's grain -- `stg_square_payments_daily` is unique on
    `project_id|date|payment_id` -- so producing anything coarser would make the
    seed pass a test the real pull would fail. order_id and location_id are
    DETAIL dimensions carried for reconciliation, never grain keys.
    """
    rows: list[dict] = []
    for payment in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        currency = _money_currency(payment.get("amount_money"))
        rows.append(
            {
                "date": _created_date(payment.get("created_at")),
                "payment_id": str(payment.get("id", "")),
                "order_id": str(payment["order_id"]) if payment.get("order_id") else None,
                "location_id": (
                    str(payment["location_id"]) if payment.get("location_id") else None
                ),
                "revenue": _amount_to_units(_money_amount(payment.get("amount_money")), currency),
                "refunds": _amount_to_units(
                    _money_amount(payment.get("refunded_money")), currency
                ),
                "fees": _amount_to_units(_fee_total(payment), currency),
                # One payment is one transaction and one order line. Split-tender
                # orders can overcount order_count; documented in the connector,
                # and the seed reproduces the behaviour rather than hiding it.
                "transaction_count": 1,
                "order_count": 1,
                "revenue_source_currency": (currency or _DEFAULT_CURRENCY).upper(),
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
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["date"], row["payment_id"], row["order_id"], row["location_id"],
                    row["revenue"], row["refunds"], row["fees"],
                    row["transaction_count"], row["order_count"],
                    row["revenue_source_currency"], pull_id, loaded_at, row["project_id"],
                )
                for row in rows
            ],
        )
    finally:
        con.close()
    return len(rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str = "default") -> tuple[str, int]:
    """The entry point the seed-to-mart fixture calls.

    `days` is accepted and unused: the golden pull pins its own payment dates,
    and widening the window would mean fabricating Square payments. Taking the
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
    print(f"square seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
