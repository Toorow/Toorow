"""Brevo seed loader -- Story 53.10.

Lands a deterministic set of ``raw_brevo_daily`` rows into a DuckDB file so
``stg_brevo_daily`` can build locally. Without it the model exists and nothing
has ever produced a row through it: the connector counts as "covered" while the
warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against. The staging model is ``SELECT *``, so the DDL below
is a copy of ``connector._RAW_DDL``: any column dropped here would vanish from
the model without a single test noticing.

Only catalog-declared metric ids land (``connector._CATALOG_METRIC_IDS``). That
gate is H-2 / AD-4: Brevo returns ``openRate`` and friends next to the
counters, and a ratio in an additive landing is a double-count waiting for a
SUM. The gate is restated here rather than assumed, so a seed cannot smuggle in
what the connector refuses.

``protected_identifier`` is empty because the fixture carries no contact: the
connector would hash whatever it found, and hashing the empty string would put
a 64-character digest in the column, suggesting a person who does not exist.
``event_type`` is empty for the same reason (no ``event`` key). ``profile`` is
our own declared profile name (``pull_email_campaign_daily``), matching the
fixture's ``channel: email`` and campaign counters -- not a provider value.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/brevo/seeds/load_brevo_seed.py \
        --duckdb-path server/modules/brevo/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from ulid import ULID

_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "golden_pull.json"

RAW_TABLE = "raw_brevo_daily"

#: A copy of connector._RAW_DDL, column for column and in the same order: the
#: staging model is `SELECT *`, so this table IS the model's column list.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    profile               VARCHAR,
    account_id            VARCHAR,
    source_id             VARCHAR,
    date                  VARCHAR,
    channel               VARCHAR,
    event_type            VARCHAR,
    protected_identifier  VARCHAR,
    metric                VARCHAR,
    value                 DOUBLE,
    non_additive          BOOLEAN,
    payload_json          VARCHAR,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""

_INSERT_SQL = f"""
INSERT INTO {RAW_TABLE}
    (profile, account_id, source_id, date, channel, event_type, protected_identifier,
     metric, value, non_additive, payload_json, pull_id, loaded_at, project_id)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

#: connector._CATALOG_METRIC_IDS (api_catalog.json exposed metrics). Anything
#: outside this set is a provider-computed rate and never lands (H-2, AD-4).
_CATALOG_METRIC_IDS = frozenset(
    {"sent", "delivered", "opens", "clicks", "hard_bounces", "unsubscribed",
     "event_count", "contact_count"}
)

#: connector._land's own rule for what must never be summed.
_NON_ADDITIVE_TOKENS = ("rate", "unique", "average")

#: Keys that describe the row rather than measure it.
_STRUCTURAL_KEYS = frozenset({"date", "account_id", "source_id", "channel", "event"})

#: Identity keys the payload must never carry (connector._land drops them).
_PII_KEYS = frozenset({"email", "contact"})

#: The connector profile whose grain the fixture matches
#: (connector.pull_email_campaign_daily). Our own vocabulary.
_PROFILE = "email_campaign_daily"


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def generate_rows(project_id: str = "default") -> list[dict]:
    """Flatten the golden pull into one row per (source, metric).

    That IS the model's grain -- the uniqueness test spans (project_id, profile,
    account_id, source_id, date, channel, event_type, protected_identifier,
    metric) -- so producing anything coarser would make the seed pass a test the
    real pull would fail.

    The fixture quotes its numbers (it mirrors a downloaded report, not the
    in-memory response), so the counters are coerced here; the catalog gate,
    not the Python type, decides what counts as a metric.
    """
    rows: list[dict] = []
    for item in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        source_id = str(item.get("source_id") or item.get("id") or "")
        # [:10] mirrors _land: an event carries a timestamp, the warehouse a day.
        row_date = str(item.get("date") or item.get("createdAt") or "")[:10]
        payload_json = json.dumps(
            {key: value for key, value in item.items() if key not in _PII_KEYS},
            sort_keys=True,
            separators=(",", ":"),
        )
        for metric, value in item.items():
            if metric in _STRUCTURAL_KEYS or metric not in _CATALOG_METRIC_IDS:
                continue
            rows.append(
                {
                    "profile": _PROFILE,
                    "account_id": str(item.get("account_id") or ""),
                    "source_id": source_id,
                    "date": row_date,
                    "channel": str(item.get("channel") or ""),
                    "event_type": str(item.get("event") or ""),
                    # No contact in the fixture -- nothing to protect, and a
                    # digest of nothing would read as a person.
                    "protected_identifier": "",
                    "metric": metric,
                    "value": float(value),
                    "non_additive": any(
                        token in metric.lower() for token in _NON_ADDITIVE_TOKENS
                    ),
                    "payload_json": payload_json,
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
                    row["profile"], row["account_id"], row["source_id"], row["date"],
                    row["channel"], row["event_type"], row["protected_identifier"],
                    row["metric"], row["value"], row["non_additive"],
                    row["payload_json"], pull_id, loaded_at, row["project_id"],
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
    whose dates the fixture pins. A campaign snapshot is a state, not a daily
    series, and stretching it over a rolling window would invent days the
    connector never returned. Taking the argument keeps every loader callable
    the same way, which is what lets the fixture discover them instead of
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
    print(f"brevo seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
