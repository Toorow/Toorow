"""LinkedIn company pages seed loader -- Story 53.10.

Lands a deterministic set of ``raw_linkedin_company_pages_daily`` rows into a
DuckDB file so ``stg_linkedin_company_pages_daily`` can build locally. Without it
the model exists and nothing has ever produced a row through it: the connector
counts as "covered" while the warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- long-ified exactly as ``connector._land()`` does: one
row per (grain, metric), with the connector's own non-additive rule applied to
the metric name.

WHICH PROFILE. The fixture carries ``entity_id`` plus ``impressions`` and
``likes``; ``organic_share_statistics`` is the only manifest report whose
dimensions include ``entity_id`` and whose metrics include both. The profile
stamp is therefore READ off the manifest by matching the fixture's own keys, not
chosen by hand.

NEGATIVE LIKES ARE NOT A BUG. ``api_catalog.json`` says of ``likes``:
"legitimate negative values are preserved". The fixture's ``-1`` lands as ``-1``;
clamping it would invent a measurement LinkedIn did not report.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python \
        server/modules/linkedin-company-pages/seeds/load_linkedin_company_pages_seed.py \
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

RAW_TABLE = "raw_linkedin_company_pages_daily"

#: Fixture provenance keys: they identify the recording, not the measurement.
_PROVENANCE_KEYS = frozenset({"pull_id", "connector"})

#: `connector._land()`'s non-additive rule, verbatim. A metric whose name carries
#: one of these tokens is visit/impression-scoped or already a ratio: it does not
#: sum across intervals or facets (AD-4).
_NON_ADDITIVE_TOKENS = ("engagement", "unique", "rate", "distribution")

#: The fixture records a time-bounded interval (interval_start / interval_end),
#: which is what a NON-lifetime pull returns. A lifetime pull carries no
#: interval, so stamping lifetime=TRUE here would contradict the row itself.
_LIFETIME = False

#: Exactly the columns `stg_linkedin_company_pages_daily.sql` selects -- it is a
#: `SELECT *`, so the raw table IS the model's column list. Mirrors
#: `connector._RAW_DDL`; declared here so a contract change breaks the load
#: loudly instead of producing a view with a silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    profile           VARCHAR,
    organization_urn  VARCHAR,
    entity_id         VARCHAR,
    interval_start    VARCHAR,
    interval_end      VARCHAR,
    facet             VARCHAR,
    facet_value       VARCHAR,
    metric            VARCHAR,
    value             DOUBLE,
    non_additive      BOOLEAN,
    lifetime          BOOLEAN,
    payload_json      VARCHAR,
    pull_id           VARCHAR,
    loaded_at         VARCHAR,
    project_id        VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _resolve_profile(fixture_keys: set[str]) -> tuple[str, frozenset[str], frozenset[str]]:
    """Pick the manifest report the fixture actually records.

    Matching on the fixture's own keys means the seed cannot be stamped with a
    profile whose metrics it does not carry -- which is precisely the drift a
    hand-written profile constant would let through.
    """
    reports = json.loads(_MANIFEST.read_text(encoding="utf-8"))["source_capabilities"]["reports"]
    for report in reports:
        metrics = frozenset(report.get("metrics") or ())
        dimensions = frozenset(report.get("dimensions") or ())
        measured = fixture_keys - dimensions - _PROVENANCE_KEYS
        if measured and measured <= metrics and (fixture_keys & dimensions):
            return report["id"], metrics, dimensions
    raise ValueError(
        f"{_FIXTURE.name}: no manifest report covers keys {sorted(fixture_keys)}"
    )


def generate_rows(project_id: str = "default") -> list[dict]:
    """Long-ify the golden pull into one row per (grain, metric).

    That IS the model's grain -- `stg_linkedin_company_pages_daily` supersedes on
    `project_id|profile|organization_urn|entity_id|interval_start|interval_end|
    facet|facet_value|metric` -- so producing anything coarser would make the
    seed pass a test the real pull would fail.
    """
    fixture = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    keys: set[str] = set()
    for fixture_row in fixture:
        keys |= set(fixture_row)
    profile, metrics, _dimensions = _resolve_profile(keys)

    rows: list[dict] = []
    for fixture_row in fixture:
        # The connector dumps the provider row it landed from; the fixture IS
        # that recorded row, so it is what provenance should carry.
        payload_json = json.dumps(fixture_row, sort_keys=True, separators=(",", ":"))
        for metric in sorted(metrics & set(fixture_row)):
            value = fixture_row[metric]
            if value is None:
                continue  # AD-9: an absent metric is NULL, not zero.
            rows.append(
                {
                    "profile": profile,
                    "organization_urn": str(fixture_row.get("organization_urn") or ""),
                    "entity_id": str(fixture_row.get("entity_id") or ""),
                    "interval_start": str(fixture_row.get("interval_start") or ""),
                    "interval_end": str(fixture_row.get("interval_end") or ""),
                    # organic_share_statistics is not faceted; the connector
                    # lands the empty string, not NULL, when no facet was asked.
                    "facet": str(fixture_row.get("facet") or ""),
                    "facet_value": str(fixture_row.get("facet_value") or ""),
                    "metric": metric,
                    "value": float(value),
                    "non_additive": _LIFETIME
                    or any(token in metric.lower() for token in _NON_ADDITIVE_TOKENS),
                    "lifetime": _LIFETIME,
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
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["profile"], row["organization_urn"], row["entity_id"],
                    row["interval_start"], row["interval_end"], row["facet"],
                    row["facet_value"], row["metric"], row["value"],
                    row["non_additive"], row["lifetime"], row["payload_json"],
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

    `days` is accepted and unused: the golden pull pins its own reporting
    interval, and widening the window would mean fabricating LinkedIn rows.
    Taking the argument keeps every loader callable the same way, which is what
    lets the fixture discover them instead of hardcoding each one.
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
    print(f"linkedin-company-pages seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
