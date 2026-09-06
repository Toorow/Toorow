"""Land the captured entity spine into the seeded warehouse -- AI-303.

WHAT THIS EXISTS TO END. Measured 2026-08-23 on
`server/modules/google-analytics/seeds/local.duckdb`, the warehouse every eval
question is executed against: `managed_feed_superseding`, `stg_managed_feed_facts`
and `semantic_fact_by_entity_attribute` all built over ZERO rows, and the three
mirror relations the cross reads were absent entirely. Four models green on
nothing. Story 69.5 could not add its corpus entry for exactly that reason, and
said so rather than fabricating one.

IT REPLAYS, IT DOES NOT INVENT. Every row comes from
`tests/fixtures/entity_spine/captured_spine.json`, which
`capture_entity_spine_fixture.py` writes by driving the real chain of epics 68-69
against a live Postgres and reading back the same views `core.mirror_sync`
exports. This module needs no Postgres, which is the whole point of the split:
the local loop and CI run this half alone.

THE SHAPES ARE THE FIXTURE'S, NOT THIS FILE'S. Column names and types travel with
the rows. A relation whose shape this file spelled out would be a second, drifting
definition of a mirror the product already defines -- the exact defect
`seed_all_connectors.create_mirror` documents about the partial
`project_preferences` copies that preceded it.

Idempotent by relation: each is dropped and recreated from the fixture, so
re-running the loop cannot double the rows the way an append would.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SERVER_DIR = Path(__file__).resolve().parents[2]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

FIXTURE = _SERVER_DIR / "tests" / "fixtures" / "entity_spine" / "captured_spine.json"


def _quote(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"entity spine seed: refusing an identifier {name!r}")
    return f'"{name}"'


def _create_and_fill(con, *, schema: str | None, table: str, entry: dict[str, Any]) -> int:
    columns = [(str(name), str(dtype)) for name, dtype in entry["columns"]]
    rows = entry["rows"]
    qualified = f"{_quote(schema)}.{_quote(table)}" if schema else _quote(table)
    ddl = ", ".join(f"{_quote(name)} {dtype}" for name, dtype in columns)
    con.execute(f"DROP TABLE IF EXISTS {qualified}")
    con.execute(f"CREATE TABLE {qualified} ({ddl})")
    if rows:
        names = [name for name, _dtype in columns]
        placeholders = ", ".join("?" for _ in names)
        con.executemany(
            f"INSERT INTO {qualified} VALUES ({placeholders})",  # noqa: S608
            [tuple(row.get(name) for name in names) for row in rows],
        )
    return len(rows)


def run(*, duckdb_path: str, days: int = 30, project_id: str | None = None) -> dict[str, int]:
    """Land the lot. `days` and `project_id` are accepted and unused.

    Both are part of the signature every seed loader shares so the discovery in
    `seed_all_connectors` can call them all the same way. Honouring them here
    would mean inventing days the capture never observed, or filing the rows
    under a Project that did not produce them -- and the mirror rows carry their
    own `project_id`, which is what joins them to the facts.
    """
    del days, project_id

    import duckdb  # noqa: PLC0415

    if not FIXTURE.is_file():
        raise FileNotFoundError(
            f"{FIXTURE} is missing -- regenerate it with "
            "server/tests/integration/capture_entity_spine_fixture.py against a "
            "live Postgres. This seed replays a capture; it has nothing to invent."
        )
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))

    landed: dict[str, int] = {}
    con = duckdb.connect(duckdb_path)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
        for relation, entry in sorted(payload["mirror"].items()):
            landed[f"mirror.{relation}"] = _create_and_fill(
                con, schema="mirror", table=relation, entry=entry
            )
        facts = payload["facts"]
        # The facts relation lives in the warehouse's own schema, unqualified --
        # `stg_managed_feed_facts` resolves it by the bare name `managed_feed_grain`
        # gives it, and a schema-qualified copy would be invisible to that lookup.
        landed[facts["relation"]] = _create_and_fill(
            con, schema=None, table=facts["relation"], entry=facts
        )
    finally:
        con.close()
    return landed


def main() -> int:
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", required=True)
    args = parser.parse_args()
    for relation, count in sorted(run(duckdb_path=args.duckdb_path).items()):
        print(f"  {relation:44s} {count:3d} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
