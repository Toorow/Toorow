"""A migration must not guard on a table that a LATER migration creates.

THE DEFECT, TWICE, YEARS APART IN INTENT AND DAYS APART IN TIME.

  * `105_language_dimension_family` seeds `app.dimension_labels` under
    `IF to_regclass('app.dimension_labels') IS NOT NULL`. Its own comment attributes
    the table to migration 103; it is really created by the **106**. At 105's turn the
    table does not exist, the guard is false, the seed does nothing -- and says nothing.
    Measured on the real database on 2026-08-01, nine days after both were applied:
    `SELECT count(*) FROM app.dimension_labels` -> 0. Repaired by the **174**.
  * `154_reports_notebooks_renders` adds `fk_renders_visualization_spec_version` under
    `IF to_regclass('app.visualization_spec_versions') IS NOT NULL`; that table is
    created by the **156**. Same silence, same outcome. Repaired by the **158**.

Both were found AFTER the fact, by someone querying the database and being surprised.
That is the cost this file removes: the third one fails here, at the moment it is
written, instead of in a count(*) weeks later.

WHY THE PATTERN IS SO EASY TO GET WRONG. An existence guard is the right tool for state
the migration series does not own -- a warehouse table, a rename that may already have
happened, an index built elsewhere. It is the WRONG tool for a table the series creates
itself, because there the answer is not "unknown", it is "not yet", and the two are
indistinguishable at runtime. The guard converts an ordering bug into a no-op.

Hence the rule below, and hence its narrowness: it fires ONLY on `IS NOT NULL` guards
naming a table that some migration in this same directory creates later. Everything else
-- tables never created here, `IS NULL` rename guards, index names -- is left alone,
because for those the guard is doing the job it was designed for.

Applied migrations are immutable (their sha256 is in the manifest), so the two cases
above cannot be edited out. They are named in the allowlist with the migration that
repaired each: a known, closed defect, not an excuse to add a third.
"""

from __future__ import annotations

import re
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "infra" / "nango" / "migrations"

# ---------------------------------------------------------------------------
# THE SCANNER MUST SEE EVERY FORM, OR IT IS A GUARD THAT PASSES BY BLINDNESS.
#
# Both patterns below used to be `[a-z_]+\.[a-z_]+`, applied to the raw file:
#   * no digit was allowed in an identifier, so `app.dim_country_2` would have been
#     invisible on both sides -- created without being seen as created, guarded
#     without being seen as guarded;
#   * no quoted identifier (`"app"."x"`), which Postgres accepts everywhere;
#   * `CREATE UNLOGGED TABLE` / `CREATE TEMP TABLE` did not read as a creation;
#   * comments were scanned like code, so a commented-out statement counted.
# Measured on 2026-08-22: today's 316 CREATE TABLE statements contain none of these
# forms, so this widening changes no verdict. That is the point -- an instrument is
# widened while it agrees with itself, not on the day it is found wrong.
# ---------------------------------------------------------------------------

_IDENT = r'"?([A-Za-z_][A-Za-z0-9_$]*)"?'
_TABLE_MODIFIERS = r"(?:\s+(?:GLOBAL|LOCAL|TEMPORARY|TEMP|UNLOGGED))*"

#: `to_regclass('schema.table')` compared against NOT NULL -- "only if it already exists".
_GUARD_RE = re.compile(
    r"to_regclass\(\s*'\s*" + _IDENT + r"\s*\.\s*" + _IDENT + r"\s*'\s*\)\s*IS\s+NOT\s+NULL",
    re.IGNORECASE,
)
_CREATE_RE = re.compile(
    r"CREATE" + _TABLE_MODIFIERS + r"\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    + _IDENT + r"\s*\.\s*" + _IDENT,
    re.IGNORECASE,
)
#: Every creation the scanner must be able to NAME. A statement this finds and
#: `_CREATE_RE` does not is a table the guard cannot see at all.
#: TEMPORARY/TEMP (and the standard's GLOBAL/LOCAL prefixes, which in Postgres
#: only ever qualify temp tables) create nothing persistent -- the session drops
#: them, so the ordering rule has nothing to order. Migration 327's
#: `CREATE TEMPORARY TABLE ... ON COMMIT DROP` (2026-08-31) is the first such
#: form; counting it forced the scanner to "name" a table no schema will ever
#: hold. Only UNLOGGED remains a countable modifier: an unlogged table persists.
_ANY_CREATE_RE = re.compile(r"\bCREATE(?:\s+UNLOGGED)?\s+TABLE\b", re.IGNORECASE)

_LINE_COMMENT_RE = re.compile("--[^" + chr(10) + "]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _statements(path: Path) -> str:
    """The file's SQL with its comments removed.

    A commented-out `CREATE TABLE` used to register a creator, and a commented-out
    guard used to count as an offence. Prose is not a statement.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    return _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", text))


def _qualified(match: tuple[str, str]) -> str:
    return f"{match[0].lower()}.{match[1].lower()}"

#: (guarding migration, table) -> the migration that repaired it. Historical and CLOSED:
#: both are applied, therefore immutable, therefore uneditable. A new entry here is a
#: bug being waved through -- write the follow-up migration instead.
_REPAIRED: dict[tuple[int, str], int] = {
    (105, "app.dimension_labels"): 174,
    (154, "app.visualization_spec_versions"): 158,
}


def _identifier(path: Path) -> int:
    return int(path.name[:3])


def _sql_files() -> list[Path]:
    return sorted(_MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


def _created_by() -> dict[str, int]:
    """table -> the LOWEST migration that creates it (the one that makes it exist)."""
    created: dict[str, int] = {}
    for path in _sql_files():
        text = _statements(path)
        for match in _CREATE_RE.findall(text):
            created.setdefault(_qualified(match), _identifier(path))
    return created


def test_no_migration_guards_on_a_table_that_a_later_migration_creates():
    created = _created_by()
    offences: list[str] = []
    for path in _sql_files():
        identifier = _identifier(path)
        text = _statements(path)
        for table in sorted({_qualified(m) for m in _GUARD_RE.findall(text)}):
            creator = created.get(table)
            if creator is None or creator <= identifier:
                continue  # not ours: external state, or created before -- guard is fine
            if _REPAIRED.get((identifier, table)) is not None:
                continue
            offences.append(
                f"{path.name}: guards on `{table}` IS NOT NULL, but migration "
                f"{creator:03d} creates it -- the guard is FALSE when this runs, so the "
                "block is a silent no-op. Order the migrations, or split the work."
            )
    assert not offences, "\n".join(offences)


def test_the_allowlist_names_only_real_closed_defects():
    """An allowlist nobody re-reads becomes a hiding place. This is that re-read."""
    files = {_identifier(path): path for path in _sql_files()}
    created = _created_by()
    for (identifier, table), repair in _REPAIRED.items():
        assert identifier in files, f"migration {identifier:03d} no longer exists"
        assert repair in files, (
            f"the repair announced for {identifier:03d}/{table} is migration "
            f"{repair:03d}, which is not in the tree"
        )
        text = _statements(files[identifier])
        assert table in {_qualified(m) for m in _GUARD_RE.findall(text)}, (
            f"{files[identifier].name} no longer guards on `{table}` -- if the defect is "
            "gone, delete the entry rather than leaving a permission behind"
        )
        assert created.get(table, 0) > identifier, (
            f"`{table}` is no longer created after {identifier:03d}: entry is stale"
        )


def test_the_scanner_can_name_every_table_the_migrations_create():
    """A creation the scanner cannot NAME is a table the rule above cannot protect.

    `_CREATE_RE` decides which tables exist and when. Every form it fails to parse --
    a quoted identifier, an unqualified name, a modifier nobody thought of -- silently
    removes a table from `_created_by`, and a guard on that table then reads as
    "external state, guard is fine". The rule would pass by blindness rather than by
    correctness, which is the failure mode the two historical defects had.

    So the loose count and the parsed count must agree, file by file: the scanner
    proves it sees what it claims to judge, instead of being trusted to.
    """
    blind: list[str] = []
    for path in _sql_files():
        text = _statements(path)
        seen = len(_ANY_CREATE_RE.findall(text))
        named = len(_CREATE_RE.findall(text))
        if seen != named:
            blind.append(
                f"{path.name}: {seen} CREATE TABLE statements, {named} the scanner can "
                "name. An unparsed form makes the table invisible to the ordering rule."
            )
    assert not blind, "\n".join(blind)


def test_the_scanner_reads_statements_and_not_prose():
    """A commented-out CREATE TABLE is not a creation, and a commented-out guard is
    not an offence. Both used to count, because the raw file was scanned."""
    sample = (
        "-- CREATE TABLE app.never_created (id TEXT);\n"
        "/* IF to_regclass('app.never_created') IS NOT NULL THEN */\n"
        "CREATE TABLE app.actually_created (id TEXT);\n"
    )
    stripped = _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", sample))
    assert [_qualified(m) for m in _CREATE_RE.findall(stripped)] == ["app.actually_created"]
    assert _GUARD_RE.findall(stripped) == []


def test_the_scanner_reads_the_forms_postgres_accepts_even_where_the_tree_uses_none():
    """Widened on 2026-08-22. The tree contains none of these today, and a pattern
    only proven on what exists is a pattern proven on nothing."""
    unlogged = "CREATE UNLOGGED TABLE app.staging_2 (id TEXT);"
    quoted = 'CREATE TABLE IF NOT EXISTS "app"."dim_country_2" (id TEXT);'
    temp = "CREATE TEMPORARY TABLE app.scratch (id TEXT);"
    assert [_qualified(m) for m in _CREATE_RE.findall(unlogged)] == ["app.staging_2"]
    assert [_qualified(m) for m in _CREATE_RE.findall(quoted)] == ["app.dim_country_2"]
    assert [_qualified(m) for m in _CREATE_RE.findall(temp)] == ["app.scratch"]
    guard = "IF to_regclass('\"app\".\"dim_country_2\"') IS NOT NULL THEN"
    assert [_qualified(m) for m in _GUARD_RE.findall(guard)] == ["app.dim_country_2"]
