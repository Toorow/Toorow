"""Re-seed ``mirror.datastream_source_types`` in the shape Story 48.4 gave it.

WHY THIS EXISTS. Story 48.4 moved the mirror entry off the DECLARED table and
onto ``app.datastream_source_types_v`` -- the OBSERVED projection, whose columns
are ``(datastream_id, project_id, source_type, source_type_origin,
source_type_confidence, tax_posture, applicability, tax_evidence_version_id,
created_at)``. The declared shape carried ``declared_by`` / ``declared_at``; the
observed one does not, because a source type is now proven by publication rather
than asserted by a human.

``seed_fee_tax_mirror.py`` still writes the declared shape, and it is BYTE-FROZEN
by ``test_epic41_verification_overlay_additive.test_forbidden_files_are_unchanged_from_head``.
Story 41.4 met this wall and Story 48.4 met it again; both answered with a
sibling that imports what it needs and edits nothing. This is the third, for the
same reason.

WHAT BREAKS WITHOUT IT. ``mirror_sync`` replaces the relation with the view's
shape. Re-running the frozen seeder afterwards then fails on

    Binder Error: Table "datastream_source_types" does not have a column
    named "declared_by"

and, because that INSERT sits in the middle of its run, NOTHING it seeds lands --
no projects, no rules, no cost rows. Every fee/tax mart then builds empty and
every Epic-41 assertion passes by finding nothing, which is worse than failing.

WHAT IT DOES. It widens the local relation to the UNION of the two shapes and
seeds no rows. The frozen seeder can then write its declared columns, the marts
read the two they actually use (``datastream_id``, ``source_type``), and the
observed columns exist so a model that starts reading one finds it.

A union is the honest answer here and a cast is not: declared and observed are
not two spellings of one thing, they are two eras. Production carries only the
observed one; this bridge exists solely so a LOCAL build can use a fixture
written before the change, which is why it lives in a seeder.

TWO TRAPS THAT COST HALF A DAY IF YOU FIND THEM YOURSELF.

1. THE FILE MUST BE NAMED ``local.duckdb``. Several marts compile to a
   fully-qualified ``"local"."main_staging".<model>``, and DuckDB takes the
   catalog name from the FILE name. A copy called anything else fails with
   ``Binder Error: Catalog "local" does not exist!`` -- but only on the models
   that qualify, so a partial build looks like it worked.
2. A COPIED ``local.duckdb`` CARRIES STALE MATERIALISED MODELS. ``dbt run``
   without ``--full-refresh`` leaves them in place, so a mart can read a staging
   view built months ago and fail on a column the CURRENT model does emit. That
   reads exactly like a product defect and is not one. Always ``--full-refresh``
   when the fixture came from a copy.

ORDER. After ``mirror_sync``, BEFORE the frozen fee/tax seeders -- that is the
only window where both shapes can be satisfied:

    uv run python dbt/seeds/feetax/seed_datastream_source_types_mirror.py \\
        --duckdb-path <dev.duckdb>

"""

from __future__ import annotations

import argparse
import sys

import duckdb

#: The DECLARED columns, which only the frozen fixture still writes.
DECLARED_COLUMNS = (
    ("declared_by", "VARCHAR"),
    ("declared_at", "TIMESTAMP"),
)

#: The observed projection's columns, in the order `app.datastream_source_types_v`
#: declares them. This is what production carries.
OBSERVED_COLUMNS = (
    ("datastream_id", "VARCHAR"),
    ("project_id", "VARCHAR"),
    ("source_type", "VARCHAR"),
    ("source_type_origin", "VARCHAR"),
    ("source_type_confidence", "DOUBLE"),
    ("tax_posture", "VARCHAR"),
    ("applicability", "VARCHAR"),
    ("tax_evidence_version_id", "VARCHAR"),
    ("created_at", "TIMESTAMP"),
)


def run(duckdb_path: str) -> int:
    conn = duckdb.connect(duckdb_path)
    try:
        conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'mirror' AND table_name = 'datastream_source_types'"
            ).fetchall()
        }
        if not existing:
            columns = ", ".join(
                f"{name} {dtype}" for name, dtype in (*OBSERVED_COLUMNS, *DECLARED_COLUMNS)
            )
            conn.execute(f"CREATE TABLE mirror.datastream_source_types ({columns})")
            return len(OBSERVED_COLUMNS) + len(DECLARED_COLUMNS)

        # ADD, never replace: the relation may already hold rows mirror_sync
        # brought from the view, and dropping them would trade one missing shape
        # for missing data.
        added = 0
        for name, dtype in (*OBSERVED_COLUMNS, *DECLARED_COLUMNS):
            if name in existing:
                continue
            conn.execute(
                f"ALTER TABLE mirror.datastream_source_types ADD COLUMN {name} {dtype}"
            )
            added += 1
        return added
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-path", required=True)
    args = parser.parse_args()
    added = run(args.duckdb_path)
    print(
        f"mirror.datastream_source_types: {added} column(s) reconciled -> "
        f"{args.duckdb_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
