"""No CHECK constraint in schema `app` may be satisfiable by a NULL expression.

THE CLASS THIS GUARDS. A CHECK rejects only FALSE: an expression that evaluates
to NULL is ACCEPTED. Every "exactly one of these two columns" and "if this state
then that column" constraint is therefore defeated by making both sides NULL --
the row satisfies no branch and the database takes it anyway.

Migration 155 closed this on Story 50.3's four constraints. Migration 156 landed
hours later and reopened it on a fifth. That is what a class defect looks like
when it is repaired only where it was spotted, and it is why this file walks
`pg_constraint` instead of naming the constraints it knows about.

HOW IT DECIDES, and why it is not a text match. Each constraint is evaluated
against real candidate rows: NULL for every NULLABLE column, the literals the
expression itself names for the others, and NEVER a NULL in a NOT NULL column --
nulling a NOT NULL column invents a row the table cannot hold and manufactures a
false positive. If some legal row makes the expression NULL, the constraint is
reported with the exact witness that defeats it.

THE ALLOW-LIST is the ordinary nullable-enumeration idiom -- a lone
`col = ANY (ARRAY[...])` on a nullable column, where NULL means "not stated" and
is meant to be permitted. `app.target_fields.target_fields_measure_check` writes
`NULL::text` into its own array to say so. Those are not defects and tightening
them would reject legitimate existing rows. Every entry is a single-predicate
membership test; a multi-branch contract may never be added here.
"""

from __future__ import annotations

import itertools
import re

import pytest

#: THE COST IS THE POINT, AND IT IS DECLARED HERE (2026-09-01). This file
#: evaluates every CHECK of schema `app` against generated candidate rows --
#: measured 193 s on the disposable cluster. Under the 180 s per-test budget a
#: full run of `server/tests/core` was ABORTED at 18 % and reported nothing at
#: all: the whole suite was lost to the one test that legitimately takes longest.
#: `pytest.mark.timeout(600)` states the real budget on the test that owns it, so
#: measuring the suite no longer needs a global option -- the repository's own
#: idiom (`test_currency_rederivation.py`, `test_seed_to_mart_loop.py`).
pytestmark = [
    pytest.mark.usefixtures("live_postgres"),
    pytest.mark.timeout(600),
]

#: Nullable enumerated columns whose NULL means "not stated". Single-predicate
#: membership tests only -- never a constraint containing OR or AND.
INTENDED_NULLABLE_ENUMS = {
    ("app.datastream_setup_assets", "datastream_setup_assets_detected_format_check"),
    ("app.inbound_brand_match_decisions", "inbound_brand_match_decisions_alert_reason_check"),
    ("app.inbound_brand_match_decisions", "inbound_brand_match_decisions_method_check"),
    ("app.mdm_canonical_fields", "mdm_canonical_fields_aggregation_check"),
    ("app.project_grant_changes", "project_grant_changes_after_capability_check"),
    ("app.project_grant_changes", "project_grant_changes_before_capability_check"),
    ("app.target_fields", "target_fields_measure_check"),
}

_LIST_SQL = """
SELECT c.conrelid::regclass::text, c.conname, pg_get_expr(c.conbin, c.conrelid),
       (SELECT array_agg(ARRAY[a.attname, format_type(a.atttypid, a.atttypmod),
                               a.attnotnull::text] ORDER BY a.attnum)
          FROM pg_attribute a
         WHERE a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey))
FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace
WHERE c.contype = 'c' AND n.nspname = 'app'
ORDER BY 1, 2
"""

#: One or two representative values per type, enough to exercise a branch.
_GENERIC = {
    "boolean": ["true", "false"],
    "integer": ["0", "1"],
    "smallint": ["0", "1"],
    "bigint": ["0", "1"],
    "numeric": ["0", "1"],
    "double precision": ["0", "1"],
    "jsonb": ["'{}'::jsonb", "'[]'::jsonb"],
    "json": ["'{}'::json"],
    "timestamp with time zone": ["now()"],
    "timestamp without time zone": ["now()::timestamp"],
    "date": ["current_date"],
    "uuid": ["'00000000-0000-0000-0000-000000000000'::uuid"],
}


def _candidates(expr: str, typ: str, notnull: bool) -> list[str] | None:
    values: list[str] = []
    if not notnull:
        values.append("NULL")
    literals = ["'" + lit + "'" for lit in re.findall(r"'((?:[^']|'')*)'::text", expr)]
    # `numeric(38,18)` must reduce to `numeric`, or the lookup misses and a
    # NOT NULL column would be probed with NULL.
    base = re.sub(r"\(.*\)", "", typ.split("[")[0]).strip()

    if typ.endswith("[]"):
        # AN ARRAY COLUMN NEEDS ARRAY-SHAPED CANDIDATES, and this is the hole the
        # sweep had. Without this branch the grid for a `text[]` column collapsed
        # to the scalar literals -- `'console'::text[]`, `'__no_such_value__'::text[]`
        # -- every one of which raises `malformed array literal`, is caught, and is
        # skipped. Every candidate failing to cast looks exactly like no candidate
        # making the expression NULL, so the constraint was reported clean.
        #
        # THE VALUE THAT MATTERS IS THE EMPTY ARRAY. `array_length(col, 1)` returns
        # NULL, not 0, for `'{}'` -- so `array_length(col,1) >= 1 AND ...` is
        # `NULL AND true` = NULL, and the row is ACCEPTED while carrying none of
        # the elements the constraint exists to require. Migration 160 shipped
        # exactly that on `app.renderer_runtime_builds.responsive_profiles`,
        # verified accepted on this database, and this sweep stayed green through
        # it. `cardinality()` returns 0 and is the fix; this branch is what makes
        # the next `array_length` a red test instead of a silent one.
        values.append("'{}'")
        values.extend(f"ARRAY[{lit}]" for lit in literals)
        values.append("ARRAY[NULL]")
        if not (base.startswith("character") or base == "text"):
            values.extend(f"ARRAY[{lit}]" for lit in _GENERIC.get(base, []))
    else:
        values.extend(literals)
        if base.startswith("character") or base == "text":
            values.append("'__no_such_value__'")
        else:
            values.extend(_GENERIC.get(base, []))

    unique: list[str] = []
    for value in values:
        if value not in unique and (value != "NULL" or not notnull):
            unique.append(value)
    return unique[:8] or None


def _null_witness(conn, table: str, expr: str, columns) -> dict | None:
    """A legal row making `expr` NULL, or None if no probed row does."""
    grids = [_candidates(expr, c[1], c[2] == "true") for c in columns]
    if not columns or any(g is None for g in grids):
        return None
    total = 1
    for grid in grids:
        total *= len(grid)
    if total > 400_000:  # nothing in this schema reaches it; guards a future one
        return None
    for combo in itertools.product(*grids):
        projection = ", ".join(f"{v}::{c[1]} AS {c[0]}" for v, c in zip(combo, columns))
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT probe")
            try:
                cur.execute(f"SELECT ({expr}) IS NULL FROM (SELECT {projection}) AS t")
                is_null = cur.fetchone()[0]
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT probe")
                continue
            cur.execute("RELEASE SAVEPOINT probe")
        if is_null:
            return dict(zip((c[0] for c in columns), combo))
    return None


def test_no_check_constraint_in_app_is_satisfiable_by_null(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute(_LIST_SQL)
        constraints = cur.fetchall()

    assert len(constraints) > 1000, (
        f"only {len(constraints)} CHECK constraints found in schema app -- the "
        "migrations do not look applied, and a sweep over an empty schema is a "
        "green that means nothing"
    )

    offenders = []
    for table, name, expr, columns in constraints:
        if (table, name) in INTENDED_NULLABLE_ENUMS:
            continue
        witness = _null_witness(live_postgres, table, expr, columns or [])
        if witness is not None:
            offenders.append(
                f"{table} :: {name}\n"
                f"      accepted with {witness}\n"
                f"      {' '.join(expr.split())[:220]}"
            )

    assert not offenders, (
        f"{len(offenders)} CHECK constraint(s) evaluate to NULL on a legal row, so "
        "the database ACCEPTS a row that satisfies no branch of them. Wrap the "
        "expression in COALESCE(..., FALSE) in the next migration, as 155 and 159 "
        "did:\n    " + "\n    ".join(offenders)
    )


def test_the_allow_list_only_contains_single_predicate_membership_tests(live_postgres):
    """Stops the allow-list from becoming the place real defects are hidden."""
    with live_postgres.cursor() as cur:
        cur.execute(_LIST_SQL)
        by_name = {(t, n): e for t, n, e, _ in cur.fetchall()}

    wrong = []
    for key in sorted(INTENDED_NULLABLE_ENUMS):
        expr = by_name.get(key)
        if expr is None:
            wrong.append(f"{key[0]} :: {key[1]} -- allow-listed but no longer exists")
            continue
        if " OR " in expr or " AND " in expr:
            wrong.append(
                f"{key[0]} :: {key[1]} -- allow-listed but is a multi-branch "
                f"contract, not a nullable enumeration: {' '.join(expr.split())[:160]}"
            )
    assert not wrong, "\n    ".join(["allow-list is unsound:"] + wrong)
