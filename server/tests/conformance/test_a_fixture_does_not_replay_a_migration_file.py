"""A fixture that replays a migration file UNDOES what later migrations repaired.

WHY THIS FILE EXISTS, AND IT IS THE SECOND TIME THE SAME CLASS COST A DAY.

`tests/migration_ledger.py` named the class on 2026-08-17: twenty-eight pg-gated
test files replay a chain of raw migration files in their fixture --
`cur.execute(path.read_text())` -- a habit from before the disposable cluster,
when the test database was not migrated. The migrations are written idempotent
(`CREATE TABLE IF NOT EXISTS`, `CREATE OR REPLACE FUNCTION`,
`DROP TRIGGER IF EXISTS`), so replaying one "works" -- and silently restores
whatever a LATER migration had changed. The first symptom cost 113
`NotNullViolation` in files that had asked for nothing. One file was converted to
the helper that measurement produced. The other twenty-seven were not, and
nothing in the repository could see them.

THE SECOND SYMPTOM, MEASURED 2026-09-02, and it is the expensive one. Migrations
`030`, `032`, `042`, `077`, `078` and `081` all PREDATE `099`, and each opens
with `DROP TRIGGER IF EXISTS ... ; CREATE TRIGGER ...`. Replaying one against a
migrated base therefore discards the WHEN clause `099` installed --

    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')

-- and seven DELETE guards inside the org tree stop yielding to a flagged RGPD
erasure. `test_immutability_triggers_yield_to_erasure` then reports six offenders
and reads as a defect of the shipped schema. It is not: a clean replay 001->338
into a fresh database keeps the clause on all seven. The instrument was measuring
damage the suite itself had done to its own base, in the same session.

That is the shape of defect this repository has paid for four times: the culprit
stays green and the victim is somewhere else. So the question is asked here,
derived, instead of once per emergency.

WHAT IT ASKS, AND WHY IT IS NARROWER THAN "NEVER REPLAY A MIGRATION". Replaying
is legitimate when the replay IS the assertion -- `test_reapplying_080_is_idempotent`
and `test_migration_081_applies_on_populated_publication_log` both exist to prove
a migration survives a second application, and routing them through the ledger
helper would make them vacuous. So the rule is not "do not replay". It is:

    a test that raw-executes a migration file, in a file that names a migration
    which recreates a DELETE guard on a table inside the org tree, must re-arm
    the erasure hatch in the same function.

Everything else -- a chain in a fixture -- has the better answer already written:
`apply_migrations_absent_from_the_ledger`, which does not replay what the ledger
carries, and so has nothing to repair.

IT READS THE LIVE CATALOG for the org tree, and skips without a DSN. Same choice,
same reason, as `test_immutability_triggers_yield_to_erasure` next door: a
hand-listed org tree disagrees with the database often enough to have false
teeth, and a table that enters the tree tomorrow must start counting the same
day, with nobody having to remember.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re

import pytest

pytestmark = pytest.mark.skipif(
    not (os.getenv("TEST_POSTGRES_DSN") or os.getenv("PLATFORM_DB_URL")),
    reason="TEST_POSTGRES_DSN not set -- the org tree cannot be read",
)

_ROOT = pathlib.Path(__file__).resolve().parents[3]
_MIGRATIONS = _ROOT / "infra" / "nango" / "migrations"
_TESTS = _ROOT / "server" / "tests"

#: The function that re-arms the hatch after a deliberate replay.
_REARM = "rearm_the_erasure_hatch"

#: `030_versioned_datastream_intents.sql` and friends, as they are spelled in a
#: test file.
_MIGRATION_NAME = re.compile(r"\d{3}_[a-z0-9_]+\.sql")

#: `CREATE TRIGGER <name> <timing/events> ON app.<table> ... EXECUTE ...`.
_CREATE_TRIGGER = re.compile(
    r"CREATE\s+TRIGGER\s+(\w+)\s+(.*?)\bEXECUTE\b", re.IGNORECASE | re.DOTALL
)


def _dsn() -> str:
    return os.getenv("TEST_POSTGRES_OWNER_DSN") or os.getenv("TEST_POSTGRES_DSN") or os.getenv(
        "PLATFORM_DB_URL"
    )  # type: ignore[return-value]


@pytest.fixture(scope="module")
def org_tree() -> set[str]:
    """Tables FK-reachable from `app.organizations` -- the erasure's own scope.

    The same walk `core.org_purge` and migration 339 use, so this guard cannot
    answer differently from the thing it guards.
    """
    import psycopg

    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
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
def hatch_dropping_migrations(org_tree) -> dict[str, list[str]]:
    """Migration file -> the org-tree DELETE guards it would recreate unguarded.

    DERIVED FROM THE FILES, not listed: a migration qualifies when it contains a
    `CREATE TRIGGER` that fires on DELETE, over a table the org tree reaches, and
    carries no WHEN clause of its own. Those are exactly the statements whose
    replay drops what `099`, `264`, `278` and `339` installed.
    """
    dropping: dict[str, list[str]] = {}
    for path in sorted(_MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        for match in _CREATE_TRIGGER.finditer(sql):
            head = match.group(2)
            if not re.search(r"\bDELETE\b", head, re.IGNORECASE):
                continue
            if re.search(r"\bWHEN\b", head, re.IGNORECASE):
                continue
            table = re.search(r"\bON\s+app\.(\w+)", head, re.IGNORECASE)
            if table and table.group(1) in org_tree:
                dropping.setdefault(path.name, []).append(
                    f"app.{table.group(1)}.{match.group(1)}"
                )
    return dropping


def _raw_replay_sites(tree: ast.Module) -> list[tuple[int, ast.FunctionDef | None]]:
    """Every `<cursor>.execute(<...>.read_text(...))`, with its enclosing function.

    Also catches the two-step spelling `sql = path.read_text(); cur.execute(sql)`,
    because a rule that only sees one spelling is a rule that renames the defect.
    """
    enclosing: dict[int, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for child in ast.walk(node):
                enclosing.setdefault(id(child), node)  # type: ignore[arg-type]

    # THE NAME BINDING IS PER-FUNCTION, and that is a measured correction, not a
    # nicety. Tracking `sql = path.read_text()` across the whole MODULE reported
    # `test_datastream_publication_constraints.py:350` -- a proxy cursor whose
    # `execute(self, sql, ...)` parameter merely shares the name with an
    # assignment three hundred lines away. A guard that names an innocent line is
    # a guard that gets an exemption written for it.
    def _local_text_names(scope: ast.AST) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(scope):
            if isinstance(node, ast.Assign) and "read_text" in ast.dump(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
        return names

    sites: list[tuple[int, ast.FunctionDef | None]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
        ):
            continue
        first = node.args[0]
        function = enclosing.get(id(node))
        is_replay = "read_text" in ast.dump(first) or (
            isinstance(first, ast.Name)
            and function is not None
            and first.id in _local_text_names(function)
        )
        if is_replay:
            sites.append((node.lineno, function))
    return sites


def test_the_scanner_finds_migrations_that_would_drop_the_hatch(
    hatch_dropping_migrations,
) -> None:
    """Calibration, and it is not a formality.

    Both halves of this guard are regexes over SQL. If the `CREATE TRIGGER`
    pattern stops matching -- a reformatted migration, a `CREATE OR REPLACE
    TRIGGER`, a schema rename -- the dangerous set goes EMPTY and every assertion
    below passes on a scope of zero files. That failure mode is silent, it has
    happened to the guard next door, and this is what notices it.

    Six migrations are known to qualify (030, 032, 042, 077, 078, 097, 081); the
    bound is deliberately loose so that a new one is welcome and a collapse is
    not.
    """
    assert len(hatch_dropping_migrations) >= 5, (
        f"only {len(hatch_dropping_migrations)} migration(s) parse as recreating an "
        f"org-tree DELETE guard: {sorted(hatch_dropping_migrations)}. The scanner "
        f"has gone blind and the assertions below prove nothing."
    )


def test_no_fixture_replays_a_migration_that_drops_the_erasure_hatch(
    hatch_dropping_migrations,
) -> None:
    offenders: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        named = {n for n in _MIGRATION_NAME.findall(source) if n in hatch_dropping_migrations}
        if not named:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover -- a broken test file fails elsewhere
            continue
        for lineno, function in _raw_replay_sites(tree):
            body = ast.unparse(function) if function is not None else source
            if _REARM in body:
                continue
            rel = path.relative_to(_ROOT).as_posix()
            guards = sorted({g for n in named for g in hatch_dropping_migrations[n]})
            offenders.append(
                f"{rel}:{lineno} raw-executes a migration file while this file names "
                f"{sorted(named)}, which recreate {guards} WITHOUT the "
                f"`app.rgpd_erasure` clause 099 installed. An org erasure then fails "
                f"for the rest of the pytest session, in whatever suite runs next. "
                f"Use `tests.migration_ledger.apply_migrations_absent_from_the_ledger` "
                f"-- or, if the replay IS the assertion, call `{_REARM}(conn)` in this "
                f"same function."
            )
    assert not offenders, "\n".join(offenders)
