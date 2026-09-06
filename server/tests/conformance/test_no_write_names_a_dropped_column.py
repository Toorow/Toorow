"""No statement in this repository may name a column a migration has DROPPED.

WHY THIS FILE EXISTS (AI-262). Migration 131 dropped `app.projects.currency` and
`app.projects.timezone` -- a Project preference belongs to its confirmed
configuration version, never to a stored default column. A test fixture kept
writing them anyway, and the way it failed is the reason a guard is needed
rather than a repair:

    without a Postgres DSN the fixture SKIPS, so nobody ever sees it red;
    with one it raises UndefinedColumn, on a machine that had already moved on.

A defect whose only symptom is a skip is invisible in exactly the runs that are
supposed to prove the schema. So the check is made STATIC: it reads the
migration files and the source, and answers on every run, DSN or not.

WHAT IT ASKS. For every column a migration drops from a table in `app.` and that
no later migration adds back, no `INSERT INTO <that table> (...)` anywhere in the
repository may name it. The column list of an INSERT is the one place where a
dropped column is a hard error rather than a stale comment.

THE GUARD READS THE MIGRATION TEXT, and that is a deliberate narrowing. Reading
the live catalog -- the choice `test_migration_erasure_claims.py` makes for
foreign keys -- would need a DSN, which is the very dependency that let this
defect hide. Drops are far simpler than `ON DELETE` clauses: eleven statements in
the whole chain, all of the form `DROP COLUMN [IF EXISTS] <name>`, so a parser
can be exact here where it could not be there.

ITS SCOPE, STATED because a ratchet that covers one package hides the rest. It
binds INSERT statements only, in `server/` -- not SELECT, not UPDATE, not the
dbt models, which read relations this file knows nothing about. A dropped column
named in a SELECT is a different defect and this guard does not claim it.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"
SOURCE_ROOT = ROOT / "server"

#: `ALTER TABLE [ONLY] app.<table>` -- the statement a DROP/ADD COLUMN belongs to.
_ALTER = re.compile(r"ALTER\s+TABLE\s+(?:ONLY\s+)?(app\.[a-z0-9_]+)", re.IGNORECASE)
_DROP_COLUMN = re.compile(r"DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?([a-z0-9_]+)", re.IGNORECASE)
_DROP_TABLE = re.compile(
    r"DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(app\.[a-z0-9_]+)", re.IGNORECASE
)
_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(app\.[a-z0-9_]+)", re.IGNORECASE
)

#: A dropped table is an offence when it is a STATEMENT, not when it is a NAME.
#:
#: `test_global_scope_api_seams.py` lists `"app.project_members"` inside a
#: `forbidden` tuple -- the line exists to keep the table out of production code,
#: and a guard that read it as a use would delete its own ally. The SQL keyword
#: in front is what tells the two apart, and it is the same distinction the
#: column half makes by matching `INSERT INTO ... (...)` rather than a bare name.
_SQL_USE = re.compile(
    r"\b(?:FROM|INTO|JOIN|UPDATE|TABLE|EXISTS)\s+(app\.[a-z0-9_]+)", re.IGNORECASE
)
_ADD_COLUMN = re.compile(r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z0-9_]+)", re.IGNORECASE)

#: `INSERT INTO app.<table> ( a, b, c )` -- the column list, possibly wrapped
#: across string concatenations, which is how every Python fixture writes it.
_INSERT = re.compile(
    r"INSERT\s+INTO\s+(app\.[a-z0-9_]+)\s*(?:\"\s*\n\s*\")?\s*\(([^)]*)\)",
    re.IGNORECASE,
)


def _statements(sql: str) -> list[tuple[str, str]]:
    """(table, body) for each ALTER TABLE statement, in file order.

    An ALTER may carry several comma-separated actions across several lines
    (migration 125 drops five columns in one statement), so the body runs to the
    terminating semicolon rather than to the end of the line.
    """
    out: list[tuple[str, str]] = []
    for match in _ALTER.finditer(sql):
        end = sql.find(";", match.end())
        body = sql[match.end() : end if end != -1 else len(sql)]
        out.append((match.group(1).lower(), body))
    return out


def dropped_columns() -> dict[str, set[str]]:
    """Columns dropped by the migration chain and never added back after."""
    dropped: dict[str, set[str]] = {}
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        for table, body in _statements(sql):
            for column in _ADD_COLUMN.findall(body):
                dropped.get(table, set()).discard(column.lower())
            for column in _DROP_COLUMN.findall(body):
                dropped.setdefault(table, set()).add(column.lower())
    return {table: columns for table, columns in dropped.items() if columns}


def dropped_tables() -> set[str]:
    """Tables the migration chain drops and no later migration creates again.

    ADDED 2026-08-16, and the reason is a measured miss: this file guarded
    COLUMNS only, and `app.project_members` -- a whole TABLE, dropped by
    migration 132 -- went on being written by a fixture for months.
    `test_mediaplan_api.py` raised `UndefinedTable` on every test as soon as a
    Postgres DSN was present, and skipped in silence without one. Same failure
    mode as the column it was written for, one granularity up.
    """
    dropped: set[str] = set()
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        for table in _CREATE_TABLE.findall(sql):
            dropped.discard(table.lower())
        for table in _DROP_TABLE.findall(sql):
            dropped.add(table.lower())
    return dropped


def required_columns() -> dict[str, set[str]]:
    """Columns a migration made NOT NULL with NO default, per table.

    THE THIRD VARIANT OF ONE DEFECT (2026-08-16). This file already refuses an
    INSERT that names a DROPPED column, and a statement that names a DROPPED
    table. The variant it could not see is the opposite: an INSERT that OMITS a
    column the schema now requires.

    `app.mdm_canonical_fields.value_type` landed NOT NULL with no default in
    migration 241, and THREE fixtures kept inserting without it -- each failing
    with `NotNullViolation` only once a DSN was present, and skipping in silence
    otherwise. Exactly the failure mode this module exists for.

    It reads `ADD COLUMN <name> ... NOT NULL` with no `DEFAULT` on the same
    statement. A column made NOT NULL by a later `ALTER COLUMN` is NOT claimed
    here: that form carries no type, its backfill may supply the value, and a
    guard that guesses is worse than one with a stated edge.
    """
    required: dict[str, set[str]] = {}
    pattern = re.compile(
        r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z0-9_]+)([^,;]*)",
        re.IGNORECASE,
    )
    for path in sorted(MIGRATIONS.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        for table, body in _statements(sql):
            for column, tail in pattern.findall(body):
                upper = tail.upper()
                if "NOT NULL" in upper and "DEFAULT" not in upper:
                    required.setdefault(table, set()).add(column.lower())
    return required


def test_the_chain_requires_a_column_so_this_half_reads_something() -> None:
    """Calibration, naming the column that opened this half."""
    required = required_columns()
    assert "value_type" in required.get("app.mdm_canonical_fields", set())


def test_no_insert_omits_a_column_the_schema_requires() -> None:
    required = required_columns()
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == pathlib.Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "INSERT INTO app." not in source:
            continue
        for table, column_list in _INSERT.findall(source):
            needed = required.get(table.lower())
            if not needed:
                continue
            named = {
                token.strip().strip(chr(34)).strip(chr(39)).lower()
                for token in column_list.replace(chr(34), " ").split(",")
            }
            for column in sorted(needed - named):
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}: INSERT INTO {table} omits "
                    f"`{column}`, which a migration made NOT NULL with no default"
                )
    assert not offenders, chr(10).join(offenders)


def test_the_chain_drops_tables_so_that_half_of_the_guard_reads_something() -> None:
    """Calibration for the table half, naming the drop that opened it."""
    assert "app.project_members" in dropped_tables(), "parser no longer sees migration 132"


def _prose_lines(source: str) -> set[int]:
    """Line numbers occupied by a DOCSTRING -- documentation, not a statement.

    `column_treatments.py:29` explains, in its module docstring, that
    `app.datastream_derived_columns` is deprecated. That sentence is why the next
    reader does not go looking for the table, and a guard that mistook it for a
    write would force the explanation out of the file -- the same mistake the
    column half already avoids for `#` comments.

    Docstrings are found with `ast`, not with a quote-counting heuristic: a
    triple-quoted SQL string assigned to a name is CODE and must stay in scope.
    """
    import ast

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    prose: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        prose.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return prose


def test_the_table_half_actually_matches_a_statement() -> None:
    """THE PROBE, KEPT -- because this guard was once green for the worst reason.

    Its first pattern carried a literal BACKSPACE where a word boundary was
    meant, so it matched nothing at all and passed on everything. No amount of
    reading found it; writing a file that SHOULD fail did, in one run.

    A guard that cannot be shown to bite is a guard nobody should trust, so the
    demonstration lives here rather than in a shell someone ran once.
    """
    statement = 'SQL = "SELECT identity FROM app.project_members WHERE id = %s"'
    assert _SQL_USE.findall(statement) == ["app.project_members"]
    # And the converse, which is the whole reason the pattern is anchored on a
    # SQL keyword: a bare NAME in a forbidden-list is not a use.
    assert _SQL_USE.findall('    "app.project_members",') == []


def test_no_statement_names_a_table_a_migration_dropped() -> None:
    """The executable half. A SENTENCE about the absence is not the defect.

    `test_hosted_entry_scope.py` asserts `"INSERT INTO app.project_members" not
    in sql` -- that line exists precisely to keep the table gone, and a guard
    that cannot tell it from a write would force the assertion out of the file.
    So a line carrying `not in`, `assert` or a comment marker is inventory, not
    an offence.
    """
    gone = dropped_tables()
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == pathlib.Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "app." not in source:
            continue
        prose = _prose_lines(source)
        for number, line in enumerate(source.splitlines(), 1):
            if number in prose:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if " not in " in stripped or stripped.startswith("assert "):
                continue
            for used in _SQL_USE.findall(stripped):
                if used.lower() in gone:
                    offenders.append(
                        f"{path.relative_to(ROOT).as_posix()}:{number}: reads or "
                        f"writes `{used}`, dropped by a migration -- {stripped[:70]}"
                    )
    assert not offenders, chr(10).join(offenders)


def test_the_chain_drops_columns_so_this_guard_has_something_to_read() -> None:
    """Calibration: a guard that finds nothing to check proves nothing.

    If a rewrite of the migration format ever makes the parser blind, this fails
    first -- with `app.projects.currency`, the drop that opened AI-262, named
    explicitly so the failure says which fact went missing.
    """
    dropped = dropped_columns()
    assert "app.projects" in dropped, "parser no longer sees migration 131"
    assert {"currency", "timezone"} <= dropped["app.projects"]


def test_no_insert_names_a_column_a_migration_dropped() -> None:
    dropped = dropped_columns()
    offenders: list[str] = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        if "INSERT INTO app." not in source:
            continue
        for table, column_list in _INSERT.findall(source):
            gone = dropped.get(table.lower())
            if not gone:
                continue
            named = {
                token.strip().strip('"').strip("'").lower()
                for token in column_list.replace('"', " ").split(",")
            }
            for column in sorted(named & gone):
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}: "
                    f"INSERT INTO {table} names `{column}`, dropped by a migration"
                )
    assert not offenders, "\n".join(offenders)
