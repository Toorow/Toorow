"""The RGPD erasure hatch is a PRIVILEGE too, not only a trigger (AI-67.5).

WHY THIS FILE EXISTS, AND WHY THE NEIGHBOURING GUARD DID NOT COVER IT.
`test_immutability_triggers_yield_to_erasure.py` asks whether every DELETE guard
inside the org tree carries the `app.rgpd_erasure` WHEN clause. That is the
trigger half. PostgreSQL checks the TABLE PRIVILEGE first, so a table can pass
that guard perfectly and still refuse the erasure with 42501, on a statement the
trigger never sees. Migration 198 found exactly that on `app.org_plan_history`
and fixed it; `known-debt.json` (2026-08-03) then measured the fix coming
undone -- privilege true right after `apply_migrations.py`, false after a run of
`server/tests/core`, because `056_org_plan_entitlements.sql:98` revokes it and
advertises itself as replayable. Migration 277 states the final posture in one
place. This file is the half of the repair that outlives it: a future revoke
gets CAUGHT here instead of being suffered in production on a customer's
right-to-erasure request.

THE SCOPE IS THE UNION OF TWO, AND NEITHER ALONE IS ENOUGH.

  (a) a DELETE guard whose definition carries `rgpd_erasure`. Some migration
      decided the erasure must pass through that table.
  (b) an ON DELETE CASCADE child of `app.organizations` -- migration 198's own
      query.

`org_plan_history` is in (b) and is NOT among the tables `core.org_purge.plan_purge`
names, because the cascade deletes it rather than a planned statement. That is
precisely why the `plan_purge`-scoped trigger guard never covered the table whose
privilege was broken, and why this file derives its scope differently.

IT READS THE LIVE CATALOG, so it needs a DSN and skips without one -- the same
choice, for the same measured reason, as the trigger guard next to it: a parse of
the migration files disagrees with the database often enough to have false teeth,
and it is the database that refuses the erasure.

THE VACUITY TRAP IS EXPLICIT BELOW. `has_table_privilege` returns TRUE for a table
OWNER and for a SUPERUSER whatever the ACL says, so if `connector` owns the app
schema -- which the local recipe did until migration 207 measured it -- every
assertion here passes on a database where a REVOKE has no effect at all. A green
run would then prove nothing. `test_the_privilege_question_is_not_vacuous` fails
loudly rather than letting that happen quietly.

THE UPDATE PIN IS GENERALISED (migration 316). The file used to pin the absence
of UPDATE for `org_plan_history` only. But 207's `ALTER DEFAULT PRIVILEGES`
hands SELECT, INSERT, UPDATE, DELETE to `connector` on every FUTURE table, so a
later narrow `GRANT SELECT, INSERT` is declarative only -- it adds nothing and
revokes nothing. Eight tables created after 207 sit in exactly that state and
are pinned below: seven are append-only by trigger (the privilege was open but
unreachable), the eighth -- `revoked_browser_sessions` -- carries no trigger,
so the privilege was the only enforcement. DELETE is NOT pinned here: the
erasure hatch needs it on the trigger-guarded tables, and 280 grants it on the
revocation ledger.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not (os.getenv("TEST_POSTGRES_DSN") or os.getenv("PLATFORM_DB_URL")),
    reason="TEST_POSTGRES_DSN not set -- the privilege catalog cannot be read",
)

#: The role every migration since 002 writes its REVOKE statements against, and
#: the role `scripts/disposable_postgres.py` runs the pg-gated suites as.
_ROLE = "connector"

#: Tables whose guard deliberately does NOT yield to an erasure, so they owe no
#: DELETE privilege either. Kept in sync with migration 099's stated exclusions.
_DELIBERATELY_CLOSED = {"audit_log"}

#: Tables whose declaring migration granted SELECT, INSERT (280 adds DELETE) and
#: nothing else -- a posture migration 207's `ALTER DEFAULT PRIVILEGES` silently
#: widened with UPDATE until migration 316 revoked it. Pinned so the next blanket
#: GRANT is caught rather than suffered.
_UPDATE_FREE_TABLES = (
    "feedback_review_subjects",
    "feedback_eligible_observations",
    "feedback_review_retries",
    "feedback_regression_cases",
    "evaluation_assertion_results",
    "feedback_regression_resolutions",
    "mdm_common_key_versions",
    "revoked_browser_sessions",
    # 317: the context relationship versions are append-only by trigger, and the
    # migration writes its REVOKE beside its GRANT rather than declaring a
    # posture 207 had already widened. Pinned the day the table was created --
    # which is the only day the repair costs nothing.
    "context_relationship_versions",
)


def _dsn() -> str:
    return os.getenv("TEST_POSTGRES_OWNER_DSN") or os.getenv("TEST_POSTGRES_DSN") or os.getenv(
        "PLATFORM_DB_URL"
    )  # type: ignore[return-value]


#: (a) a DELETE guard that yields to the erasure, or (b) a cascade child of
#: `app.organizations` -- migration 198's query. Either one means the erasure is
#: expected to delete rows from this table.
_IN_SCOPE = """
    SELECT c.relname,
           has_table_privilege(%(role)s, c.oid, 'DELETE') AS may_delete
    FROM pg_class c
    WHERE c.relnamespace = 'app'::regnamespace
      AND c.relkind = 'r'
      AND (
            EXISTS (
                SELECT 1 FROM pg_trigger t
                WHERE t.tgrelid = c.oid
                  AND NOT t.tgisinternal
                  AND (t.tgtype & 8) <> 0
                  AND pg_get_triggerdef(t.oid) ILIKE '%%rgpd_erasure%%'
            )
         OR EXISTS (
                SELECT 1 FROM pg_constraint k
                WHERE k.conrelid = c.oid
                  AND k.contype = 'f'
                  AND k.confrelid = 'app.organizations'::regclass
                  AND k.confdeltype = 'c'
            )
      )
    ORDER BY c.relname
"""


@pytest.fixture(scope="module")
def catalog():
    import psycopg

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(_IN_SCOPE, {"role": _ROLE})
            in_scope = [(row[0], row[1]) for row in cur.fetchall()]

            cur.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s",
                (_ROLE,),
            )
            role_row = cur.fetchone()

            cur.execute(
                """
                SELECT count(*) FROM pg_class c
                WHERE c.relnamespace = 'app'::regnamespace
                  AND c.relkind = 'r'
                  AND pg_get_userbyid(c.relowner) = %s
                """,
                (_ROLE,),
            )
            owned_by_role = cur.fetchone()[0]

            cur.execute(
                """
                SELECT has_table_privilege(%s, 'app.org_plan_history', 'DELETE'),
                       has_table_privilege(%s, 'app.org_plan_history', 'UPDATE')
                """,
                (_ROLE, _ROLE),
            )
            org_plan_history = cur.fetchone()

            cur.execute(
                """
                SELECT c.relname,
                       has_table_privilege(%(role)s, c.oid, 'UPDATE') AS may_update
                FROM pg_class c
                WHERE c.relnamespace = 'app'::regnamespace
                  AND c.relkind = 'r'
                  AND c.relname = ANY(%(tables)s)
                ORDER BY c.relname
                """,
                {"role": _ROLE, "tables": list(_UPDATE_FREE_TABLES)},
            )
            update_free = dict(cur.fetchall())
    return {
        "in_scope": in_scope,
        "role": role_row,
        "owned_by_role": owned_by_role,
        "org_plan_history": org_plan_history,
        "update_free": update_free,
    }


def test_the_privilege_question_is_not_vacuous(catalog) -> None:
    """Calibration, and it is the one that matters here.

    `has_table_privilege` is TRUE unconditionally for a table owner and for a
    superuser. On a database where `connector` owns the `app` tables -- what the
    local recipe did until 207 measured it -- every assertion below passes while a
    REVOKE materialises an EMPTY acl and changes nothing. The suite would be green
    on exactly the broken state it exists to catch.
    """
    assert catalog["role"] is not None, (
        f"role {_ROLE!r} does not exist on this cluster; every assertion below "
        f"would raise instead of measuring. Use scripts/disposable_postgres.py."
    )
    rolsuper, rolbypassrls = catalog["role"]
    assert not rolsuper, f"{_ROLE} is SUPERUSER: has_table_privilege is always true"
    assert not rolbypassrls, f"{_ROLE} has BYPASSRLS: this database does not model production"
    assert catalog["owned_by_role"] == 0, (
        f"{_ROLE} owns {catalog['owned_by_role']} tables in schema app. An owner keeps "
        f"every privilege whatever the ACL says, so the assertions below prove nothing. "
        f"Create the database `OWNER postgres` (scripts/disposable_postgres.py does)."
    )


def test_the_scope_has_real_coverage(catalog) -> None:
    """Two large sets can intersect in nothing; an empty scope passes everything.

    Measured 2026-08-17 on a base at 275 migrations: 126 DELETE guards carry the
    `rgpd_erasure` clause, and the union with the cascade children of
    `app.organizations` is larger still. A collapse below 50 means the derivation
    stopped matching the catalog, not that the schema got smaller.
    """
    assert len(catalog["in_scope"]) > 50, (
        f"only {len(catalog['in_scope'])} tables fall in scope -- the derivation has "
        f"collapsed and the check below proves nothing"
    )


def test_every_table_the_erasure_must_reach_carries_the_delete_privilege(catalog) -> None:
    """The finding of migration 198, generalised so it cannot recur unseen."""
    offenders = [
        f"app.{table}: `core.org_purge` must delete rows here -- its DELETE guard "
        f"yields to `app.rgpd_erasure`, or it cascades from app.organizations -- but "
        f"{_ROLE} holds no DELETE privilege, so the statement is refused with 42501 "
        f"BEFORE the trigger runs. Something revoked it after migration 277; state "
        f"the final posture there rather than in a new revoke."
        for table, may_delete in catalog["in_scope"]
        if not may_delete and table not in _DELIBERATELY_CLOSED
    ]
    assert not offenders, "\n".join(offenders)


def test_org_plan_history_keeps_both_halves_of_its_posture(catalog) -> None:
    """The exact table `known-debt.json` measured flipping, pinned in both directions.

    DELETE granted is the RGPD hatch (098/198). UPDATE revoked is append-only
    (056): history is not REWRITABLE, and erasing a whole tenant is a different,
    audited operation. Migration 207's blanket
    `GRANT ... UPDATE ... ON ALL TABLES IN SCHEMA app` handed UPDATE back and
    restated only the DELETE half; migration 277 states both.
    """
    may_delete, may_update = catalog["org_plan_history"]
    assert may_delete, (
        "app.org_plan_history: the RGPD erasure hatch is closed at the privilege "
        "level -- an org erasure fails with 42501 before the trigger is reached. "
        "This is exactly the state known-debt.json measured on 2026-08-03, and "
        "migration 277 exists to hold it open."
    )
    assert not may_update, (
        f"app.org_plan_history: {_ROLE} holds UPDATE. The table is append-only "
        f"(migration 056); the trigger still refuses the write, but the declared "
        f"privilege posture has been widened by a blanket GRANT."
    )


def test_the_eight_narrow_grant_tables_hold_no_update(catalog) -> None:
    """Migration 316's finding, pinned so a blanket GRANT cannot quietly undo it.

    Every table in `_UPDATE_FREE_TABLES` was created under 207's
    `ALTER DEFAULT PRIVILEGES`, which grants UPDATE to `connector` on future
    tables whatever the creating migration's narrow GRANT says. A table MISSING
    from the catalog is a failure too: it means the scope no longer matches the
    schema, and the pin has gone vacuous for that name.
    """
    missing = [t for t in _UPDATE_FREE_TABLES if t not in catalog["update_free"]]
    assert not missing, (
        f"tables not found in schema app: {', '.join(missing)} -- the pin no "
        f"longer matches the schema and proves nothing for these names."
    )
    offenders = [
        f"app.{table}: {_ROLE} holds UPDATE though the declaring migration "
        f"granted SELECT, INSERT only -- 207's default privileges widened the "
        f"posture again (or 316 was never applied). State the REVOKE in a "
        f"migration rather than suffering the drift."
        for table, may_update in catalog["update_free"].items()
        if may_update
    ]
    assert not offenders, "\n".join(offenders)


def test_the_audit_log_deliberately_holds_no_delete(catalog) -> None:
    """The exclusion worth pinning, because it looks like the same omission.

    `app.audit_log` is the durable trace OF the erasure and must survive it (099).
    A migration that "fixed" it by granting DELETE would let the erasure delete its
    own record, so this asserts the absence rather than leaving it to a comment.
    """
    import psycopg

    with psycopg.connect(_dsn()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT has_table_privilege(%s, 'app.audit_log', 'DELETE')", (_ROLE,)
            )
            may_delete = cur.fetchone()[0]
    assert not may_delete, (
        f"{_ROLE} can now DELETE from app.audit_log. The audit log is the durable "
        f"trace of the erasure itself and must survive it (migration 099's stated "
        f"exclusion)."
    )
