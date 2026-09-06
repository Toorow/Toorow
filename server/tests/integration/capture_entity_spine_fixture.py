"""Capture the entity spine's seed lot from a REAL chain run -- AI-303.

WHY A CAPTURE AND NOT A HAND-WRITTEN FIXTURE. `server/tests/evals/corpus.yaml`
pins every question to a `reference_sql` executed against the repository's SEEDED
warehouse plus a `fixture_sha256`. Measured 2026-08-23 on
`server/modules/google-analytics/seeds/local.duckdb`:

    managed_feed_superseding             ->  0 rows
    stg_managed_feed_facts               ->  0 rows
    semantic_fact_by_entity_attribute    ->  0 rows
    mirror.entity_key_match_verdicts_dim     ABSENT
    mirror.master_data_node_attributes_asof  ABSENT
    mirror.master_data_derived_attributes_dim ABSENT

No declared entity, no reference file, no published rule. So an entity question
added to the corpus today would be RED, or would need a fixture matching nothing
-- which is what `no-demo-content-in-product` forbids. Story 69.5 named the
prerequisite and did not do it: a seed lot for the entity spine.

THE LOT IS CAPTURED, NEVER TYPED. This module drives the chain that
`test_entity_chain_end_to_end_pg.py` already proves -- declare the type, import
the catalogue, import the calendar, record the verdicts, publish the rule -- and
then reads back the five mirror views `core.mirror_sync` exports, exactly as the
nightly would. What lands in the seed is therefore what the product produced,
and the shape cannot drift from the views because it is read from them.

Two files, and the split is the point:

  * this one needs a live Postgres and regenerates the fixture;
  * `load_entity_spine_seed.py` replays the fixture into DuckDB and needs
    nothing. The local loop and CI run the second alone.

Regenerate with a disposable Postgres up (`scripts/disposable_postgres.py up`):

    TEST_POSTGRES_DSN=... uv run python \\
        server/tests/integration/capture_entity_spine_fixture.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_SERVER_DIR = Path(__file__).resolve().parents[2]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

FIXTURE = _SERVER_DIR / "tests" / "fixtures" / "entity_spine" / "captured_spine.json"

#: The five mirror relations the entity spine reads, and the Postgres view each
#: is exported from. Taken from `mirror_sync._MIRROR_SOURCES` rather than spelled
#: again: a name that drifts here would seed a relation nothing reads.
_VIEWS = {
    "managed_feed_grain": "app.managed_feed_grain_v",
    "managed_feed_execution": "app.managed_feed_execution_v",
    "entity_key_match_verdicts_dim": "app.entity_key_match_verdicts_dim_v",
    "master_data_node_attributes_asof": "app.master_data_node_attributes_asof_v",
    "master_data_derived_attributes_dim": "app.master_data_derived_attributes_dim_v",
}

#: THE TYPE MAP IS THE MIRROR'S OWN. `core.mirror_sync._PG_TO_DUCKDB` is what the
#: nightly uses to materialise these relations, and a copy here would be a second,
#: drifting definition of a shape the product already defines -- the exact defect
#: `seed_all_connectors.create_mirror` documents about the partial
#: `project_preferences` copies that preceded it.
#:
#: It matters concretely: a first draft of this file mapped `jsonb` to VARCHAR and
#: `date` to DATE by hand, which is NOT what the mirror writes (`jsonb` -> JSON),
#: so the seeded relation and the production one would have disagreed on a column
#: the cross view joins on.
from core.mirror_sync import _PG_TO_DUCKDB  # noqa: E402

#: `information_schema` spells a type in full (`timestamp with time zone`) where
#: the map is keyed by the psycopg type NAME (`timestamptz`). One translation,
#: here, rather than a second map.
_INFORMATION_SCHEMA_NAMES = {
    "boolean": "bool",
    "smallint": "int2",
    "integer": "int4",
    "bigint": "int8",
    "real": "float4",
    "double precision": "float8",
    "numeric": "numeric",
    "date": "date",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "json": "json",
    "jsonb": "jsonb",
    "uuid": "uuid",
}


def _duckdb_type(information_schema_type: str) -> str:
    return _PG_TO_DUCKDB.get(
        _INFORMATION_SCHEMA_NAMES.get(information_schema_type, ""), "VARCHAR"
    )


def _jsonable(value: Any) -> Any:
    """A value the fixture can carry, in the form the mirror column expects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, dict)):
        # A jsonb column lands as text in the mirror; serialising here keeps the
        # fixture and the landed column the same shape.
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def _columns(conn, qualified: str) -> list[tuple[str, str]]:
    schema, table = qualified.split(".", 1)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table),
        )
        rows = cur.fetchall()
    if not rows:
        raise RuntimeError(
            f"{qualified} has no columns -- the migrations that create it are not "
            "applied on this database, so a capture would seed an empty shape"
        )
    return [(str(name), _duckdb_type(str(dtype))) for name, dtype in rows]


def capture(conn, *, project_id: str) -> dict[str, Any]:
    """Read the five views for ONE project, columns and rows together."""
    captured: dict[str, Any] = {}
    for relation, view in _VIEWS.items():
        columns = _columns(conn, view)
        names = [name for name, _type in columns]
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(names)} FROM {view} WHERE project_id = %s",  # noqa: S608
                (project_id,),
            )
            rows = [
                {name: _jsonable(value) for name, value in zip(names, row, strict=True)}
                for row in cur.fetchall()
            ]
        captured[relation] = {"columns": columns, "rows": rows}
    return captured


def main() -> int:
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        print("TEST_POSTGRES_DSN is not set -- the capture drives a real chain.")
        return 2

    import psycopg  # noqa: PLC0415

    from tests.integration import entity_spine_scenario  # noqa: PLC0415

    conn = psycopg.connect(dsn, connect_timeout=10)
    try:
        run = entity_spine_scenario.run(conn)
        conn.commit()
        captured = capture(conn, project_id=run["project_id"])
    finally:
        conn.close()

    total = sum(len(entry["rows"]) for entry in captured.values())
    if not total:
        raise RuntimeError(
            "the chain ran and every mirror view came back empty -- a seed lot of "
            "zero rows would make the models build over nothing and their tests "
            "pass by emptiness, which is the state this lot exists to end"
        )

    payload = {
        "_note": (
            "CAPTURED, NEVER TYPED (AI-303). Regenerate with "
            "server/tests/integration/capture_entity_spine_fixture.py against a "
            "disposable Postgres; the rows are what the chain of epics 68-69 "
            "actually produced, read back through the same views core.mirror_sync "
            "exports to the warehouse."
        ),
        "project_id": run["project_id"],
        "org_id": run["org_id"],
        "object_kind": run["object_kind"],
        "facts": run["facts"],
        "mirror": captured,
    }
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    for relation, entry in sorted(captured.items()):
        print(f"  {relation:38s} {len(entry['rows']):3d} row(s)")
    print(f"  {'facts (warehouse landing)':38s} {len(run['facts']['rows']):3d} row(s)")
    print(f"written: {FIXTURE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
