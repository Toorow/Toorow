"""A DELETE guard inside the org tree must yield to a flagged erasure (AI-258).

WHY THIS FILE EXISTS. Migration 099 solved this once, by hand, for the fifteen
tables the first real erasure ran into: their DELETE guard enforces a genuine
invariant -- history is not rewritable by the application -- but a tenant
erasure is a different, human-gated, audited operation, and it was simply
impossible. The repair leaves each guard byte-for-byte intact and gives the
TRIGGER a WHEN clause:

    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')

Nothing has asked the question since. A migration that adds an immutability
trigger to a table inside the org tree and forgets the clause blocks the
erasure of an organization, and the first thing to notice would be a customer's
right-to-erasure request failing in production. 145 DELETE triggers live in
`app.` today; re-reading them by hand at every migration is not a check.

THE RULE IS 099'S OWN, AND IT IS NARROWER THAN "EVERY GUARD". A table owes the
clause exactly when `core.org_purge` reaches it, because that is what the
erasure actually deletes. 099 states both exclusions and they are deliberate:

    `app.audit_log` is the durable trace OF the erasure and must survive it;
    reference data belonging to no tenant (import_templates,
    target_field_approvals, context_topics_versions, procedures_versions)
    is outside the plan, so no org erasure has any business deleting it.

099 also listed `file_source_templates` among the global data, and it is not:
it references `app.projects`, so it is org-owned and `plan_purge` reaches it.
Migration 264 gives it the clause, and this guard is what noticed.

Deriving the scope from `plan_purge` rather than re-listing it here is the
point: a table that ENTERS the org tree tomorrow starts owing the clause the
same day, with nobody having to remember.

IT READS THE LIVE CATALOG, so it needs a DSN and skips without one. That is the
choice `test_migration_erasure_claims.py` already made for the neighbouring
question, and for the same measured reason: a hand-rolled parse of the migration
files disagrees with the catalog often enough to have false teeth -- `WHEN`
clauses added by a later `ALTER`, dollar-quoted `DO` bodies, definitions this
very migration rewrites in place. What is guarded here is what the database
actually holds.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not (os.getenv("TEST_POSTGRES_DSN") or os.getenv("PLATFORM_DB_URL")),
    reason="TEST_POSTGRES_DSN not set -- the trigger catalog cannot be read",
)

#: The exact clause migration 099 installs. Compared as a substring on the
#: trigger DEFINITION, which is how Postgres hands it back.
_ESCAPE_HATCH = "rgpd_erasure"

#: An org id that exists nowhere. `plan_purge` builds its statements from the
#: foreign-key graph, not from rows, so the plan is complete for any id.
_NO_SUCH_ORG = "org_EXAMPLE"


#: `plan_purge` returns SCHEMA-QUALIFIED table names (`app.datastreams`), while
#: `pg_trigger` hands back bare ones. Comparing them raw makes the intersection
#: EMPTY -- and an empty intersection is a guard that passes on everything. It
#: happened here, on the first run, and was caught only by trying to break the
#: guard on purpose; the size calibration below did not notice, because both sets
#: were large. That is why `test_the_guard_has_real_coverage` counts the
#: INTERSECTION and not the operands.
def _bare(table: str) -> str:
    return table.split(".", 1)[-1]


def _dsn() -> str:
    return os.getenv("TEST_POSTGRES_OWNER_DSN") or os.getenv("TEST_POSTGRES_DSN") or os.getenv(
        "PLATFORM_DB_URL"
    )  # type: ignore[return-value]


def _delete_blocking_triggers(conn) -> list[tuple[str, str, str, str]]:
    """(table, trigger, trigger definition, guard function definition).

    The FUNCTION definition is fetched too, and that is not decoration: the hatch
    has TWO idioms in this repository and reading only one gives the check false
    teeth. See `_carries_the_hatch`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid),
                   pg_get_functiondef(t.tgfoid)
            FROM pg_trigger t
            JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'app'
              AND NOT t.tgisinternal
              AND (t.tgtype & 8) <> 0        -- the DELETE bit
            ORDER BY c.relname, t.tgname
            """
        )
        return [(row[0], row[1], row[2] or "", row[3] or "") for row in cur.fetchall()]


def _carries_the_hatch(definition: str, function_definition: str) -> bool:
    """The hatch is written TWO ways, and both open the erasure path.

    Migration 099 puts a WHEN clause on the TRIGGER. Migration 098 puts the same
    test inside the guard FUNCTION, which returns early when the flag is on --
    `app.org_plan_history_block_mutation` is the original of that shape. Grepping
    only `pg_get_triggerdef` reported `org_plan_history`, `first_value_events` and
    `support_access_ledger` as blocking the erasure when their path is open. A
    check that cries wolf on three tables is a check nobody reads.
    """
    return _ESCAPE_HATCH in definition or _ESCAPE_HATCH in function_definition


def _org_tree(conn) -> set[str]:
    """Tables FK-reachable from `app.organizations` -- a SUPERSET of the purge plan.

    `plan_purge` names the statements the purge ISSUES. A table reached by an
    ON DELETE CASCADE is deleted by the cascade and never appears in that plan --
    `app.org_plan_history` is exactly that shape, which is why this guard, scoped
    to the plan alone, never covered the one table whose erasure path was measured
    broken (`known-debt.json`, 2026-08-03). Measured 2026-08-17: the walk reaches
    273 tables, the plan names 192.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH RECURSIVE tree AS (
                SELECT 'app.organizations'::regclass AS oid
                UNION
                SELECT k.conrelid
                FROM pg_constraint k
                JOIN tree ON k.confrelid = tree.oid
                WHERE k.contype = 'f'
                  AND k.conrelid <> k.confrelid
            )
            SELECT c.relname
            FROM tree
            JOIN pg_class c ON c.oid = tree.oid
            WHERE c.relnamespace = 'app'::regnamespace
            """
        )
        return {row[0] for row in cur.fetchall()}


@pytest.fixture(scope="module")
def catalog():
    import psycopg
    from core.org_purge import plan_purge

    with psycopg.connect(_dsn()) as conn:
        # The UNION of both authorities. `plan_purge` is what the purge issues;
        # the FK walk is what a cascade silently carries with it. Neither contains
        # the other by construction, and a table missed by both is a table no
        # erasure touches.
        planned = {_bare(op.table) for op in plan_purge(conn, _NO_SUCH_ORG)}
        planned |= _org_tree(conn)
        triggers = _delete_blocking_triggers(conn)
    return planned, triggers


def test_the_catalog_is_readable_and_not_empty(catalog) -> None:
    """Calibration, first half: both operands are non-empty."""
    planned, triggers = catalog
    assert len(planned) > 100, len(planned)
    assert len(triggers) > 50, len(triggers)


def test_the_guard_has_real_coverage(catalog) -> None:
    """Calibration, second half, and it is the one that matters.

    Two large sets can intersect in NOTHING -- which is exactly what happened
    while this file was written, because `plan_purge` qualifies its table names
    with the schema and `pg_trigger` does not. Every assertion below passed, on a
    scope of zero tables. Counting the operands did not notice; counting the
    INTERSECTION does.

    103 of the 112 in-scope guards already carried the clause when this was first
    measured, and migration 264 gave it to the other nine. Re-measured 2026-08-17
    on a base rebuilt FROM ZERO, with the scope widened to the FK closure and the
    hatch read in the function body as well: 145 DELETE guards in `app`, 117 of
    them inside the org tree, and exactly one carrying no hatch at all --
    `connector_installations`, whose trigger migration 267 recreated. Migration
    278 restores it.
    """
    planned, triggers = catalog
    in_scope = [t for t in triggers if t[0] in planned]
    assert len(in_scope) > 50, (
        f"only {len(in_scope)} DELETE guards fall inside the purge plan -- the "
        f"scope has collapsed and the check below proves nothing"
    )


def test_every_guard_inside_the_org_tree_yields_to_a_flagged_erasure(catalog) -> None:
    planned, triggers = catalog
    offenders = [
        f"app.{table}.{trigger} blocks DELETE and the org tree reaches the table, but "
        f"the guard carries the `{_ESCAPE_HATCH}` hatch in NEITHER the trigger nor its "
        f"function -- an org erasure will fail on it. If a migration recreated this "
        f"trigger, `DROP TRIGGER` discarded the WHEN clause a previous one installed; "
        f"restore it in a NEW migration (278 is the precedent)."
        for table, trigger, definition, function_definition in triggers
        if table in planned and not _carries_the_hatch(definition, function_definition)
    ]
    assert not offenders, "\n".join(offenders)


def test_the_audit_log_deliberately_does_not_yield(catalog) -> None:
    """The one exclusion worth pinning, because it looks like an omission.

    `app.audit_log` is the durable trace OF the erasure. A migration that
    "fixed" it by adding the clause would let the erasure delete its own record,
    so this asserts the absence rather than leaving it to a comment in 099.
    """
    _, triggers = catalog
    audit = [
        (trigger, definition, function_definition)
        for table, trigger, definition, function_definition in triggers
        if table == "audit_log"
    ]
    assert audit, "app.audit_log no longer carries a DELETE guard at all"
    for trigger, definition, function_definition in audit:
        assert not _carries_the_hatch(definition, function_definition), (
            f"audit_log.{trigger} now yields to the erasure; the erasure would "
            f"delete its own trace"
        )
