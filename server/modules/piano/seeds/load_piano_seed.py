"""Piano Analytics daily seed loader -- Story 53.10.

Lands a deterministic set of ``raw_piano_daily`` LONG rows into a DuckDB file so
``stg_piano_daily`` can build locally. Without it the model exists and nothing
has ever produced a row through it: the connector counts as "covered" while the
warehouse half of its path is unverified (CAV-21).

SHAPE COMES FROM THE CONNECTOR, NOT FROM THIS FILE. The rows are derived from
``tests/fixtures/golden_pull.json`` -- the same fixture the conformance suite
asserts the pull against -- put through the two steps the connector applies
before landing: ``transform()`` renames the ``m_*`` catalog tokens with the
manifest mapping, then ``_insert_raw_rows()`` long-ifies each wide row into one
row per metric and folds every remaining property into ``segments_json``.

Both the rename map and the AD-4 non-additive set are READ from
``manifest.json`` (``canonical_metric_mapping`` and the ``non_additive`` flag on
each field), never retyped here: visits and unique_visitors are visit-scoped and
do NOT sum across property breakdowns, and that fact belongs to the contract.

The fixture's ``_fixture_note`` / ``_provenance`` keys are annotations about the
recording, not properties Piano returned. They are skipped rather than folded
into ``segments_json``, where they would silently become part of the grain.

Append-only (AD-7): each invocation mints a fresh ``pull_id`` and never
overwrites. The staging model supersedes on ``pull_id DESC``, so re-running is
safe and idempotent in effect.

Usage:
    uv run python server/modules/piano/seeds/load_piano_seed.py \
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

RAW_TABLE = "raw_piano_daily"

#: `connector._NON_SEGMENT_KEYS`: the day grain plus the site id. Everything else
#: a selection carries lands in segments_json, so ANY catalog or per-site custom
#: property participates in the supersede grain without schema churn.
_NON_SEGMENT_KEYS = frozenset({"date", "site_id", "pull_id", "connector"})

#: Exactly the columns `stg_piano_daily.sql` selects, in its order. Declared here
#: so a model change breaks the load loudly instead of producing a view with a
#: silently missing column.
_CREATE_DDL = f"""
CREATE TABLE IF NOT EXISTS {RAW_TABLE} (
    date          VARCHAR,
    site_id       VARCHAR,
    segments_json VARCHAR,
    metric        VARCHAR,
    value_num     DOUBLE,
    non_additive  BOOLEAN,
    pull_id       VARCHAR,
    loaded_at     VARCHAR,
    project_id    VARCHAR
)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def _metric_contract() -> tuple[dict[str, str], frozenset[str]]:
    """Return (catalog token -> canonical name, canonical non-additive names).

    Both come from the manifest, which is where AD-2 renames and the AD-4
    non-additive marking are decided. Reading them means a contract change
    reaches the seed instead of drifting past it.
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    rename: dict[str, str] = {}
    for token, target in manifest.get("canonical_metric_mapping", {}).items():
        rename[token] = target if isinstance(target, str) else target["canonical"]
    non_additive = frozenset(
        rename.get(field["field_id"], field["field_id"])
        for field in manifest["source_capabilities"]["fields"]
        if field.get("kind") == "metric" and field.get("non_additive")
    )
    return rename, non_additive


def generate_rows(project_id: str = "default") -> list[dict]:
    """Long-ify the golden pull into one row per (date, site, segments, metric).

    That IS the model's grain -- `stg_piano_daily` is unique on
    `project_id|date|site_id|COALESCE(segments_json,'')|metric` -- so producing
    anything coarser would make the seed pass a test the real pull would fail.
    """
    rename, non_additive_names = _metric_contract()
    metric_names = set(rename.values())
    rows: list[dict] = []
    for wide_row in json.loads(_FIXTURE.read_text(encoding="utf-8")):
        canonical = {
            rename.get(key, key): value
            for key, value in wide_row.items()
            if not key.startswith("_")  # fixture annotations, not Piano properties
        }
        segment_items = {
            key: value
            for key, value in canonical.items()
            if key not in _NON_SEGMENT_KEYS and key not in metric_names and value is not None
        }
        segments_json = json.dumps(segment_items, sort_keys=True) if segment_items else None
        for metric in sorted(metric_names & set(canonical)):
            value = canonical[metric]
            if value is None:
                continue  # AD-9: an absent metric is NULL, not zero.
            rows.append(
                {
                    "date": str(canonical.get("date", "")),
                    "site_id": str(canonical.get("site_id") or ""),
                    "segments_json": segments_json,
                    "metric": metric,
                    # Piano metrics are plain counts / decimals: there is NO
                    # micro-currency conversion here, unlike the ads platforms.
                    "value_num": float(value),
                    "non_additive": metric in non_additive_names,
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
            f"INSERT INTO {RAW_TABLE} VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    row["date"], row["site_id"], row["segments_json"], row["metric"],
                    row["value_num"], row["non_additive"],
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

    `days` is accepted and unused: the golden pull pins its own reporting days,
    and widening the window would mean fabricating Piano rows. Taking the
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
    print(f"piano seed loaded: pull_id={pull_id} rows={count}")


if __name__ == "__main__":
    main()
