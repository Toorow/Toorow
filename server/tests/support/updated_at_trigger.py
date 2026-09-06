"""`app.set_updated_at()`, ensured WITHOUT claiming to own it.

WHY THIS EXISTS. Five pg-gated suites each carried a byte-identical copy of

    CREATE OR REPLACE FUNCTION app.set_updated_at() RETURNS trigger AS $$ ... $$

to stand the trigger up on a bare schema. `CREATE OR REPLACE` is not a
conditional create: on a database where the migrations already created the
function, PostgreSQL requires the caller to OWN it before it will replace it.
The suites are meant to run as the DEPLOYED APPLICATION ROLE (`connector`),
which does not own it -- so every one of those tests died in its fixture with
`InsufficientPrivilege: doit etre le proprietaire de la fonction
set_updated_at`, before asserting anything.

Measured 2026-08-16 against a disposable PostgreSQL with all migrations
applied, connected as `connector`: 30 failures across the five files, ONE cause.
They had only ever been run as a superuser, where `CREATE OR REPLACE` succeeds
and the defect is invisible -- which is exactly how a suite passes without
proving it can run where the product runs.

So the helper asks first. The function the migrations ratified is the one the
tests must exercise; a fixture that silently replaces it would be testing its
own copy instead.
"""

from __future__ import annotations

__all__ = ["ensure_set_updated_at"]

_EXISTS_SQL = """
SELECT 1
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname = 'app' AND p.proname = 'set_updated_at'
"""

_CREATE_SQL = """
CREATE FUNCTION app.set_updated_at() RETURNS trigger AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$ LANGUAGE plpgsql
"""


def ensure_set_updated_at(conn) -> None:
    """Create `app.set_updated_at()` only if no one has already.

    On a migrated database this is a read and nothing else, so the deployed
    application role can run the suite. On a bare schema it creates the
    function, which is what the pre-migration fixtures need.
    """
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS app")
        cur.execute(_EXISTS_SQL)
        if cur.fetchone() is None:
            cur.execute(_CREATE_SQL)
    conn.commit()
