"""The dbt project must compile on BOTH engines -- `execution-substrate.md` 23.

WHY THIS FILE EXISTS. toorow builds the same dbt project twice: DuckDB in the
local fixture loop, BigQuery in production. A relation or a test that spells a
type in only one of the two dialects builds green locally and CANNOT RUN in
production. Measured 2026-08-30, before the sweep this file closes: 231
occurrences of `DOUBLE`, `VARCHAR` and parameterised `DECIMAL(p, s)` outside
comments, over 37 files -- BigQuery has none of the three, and answers
*Parameterized types are not allowed in CAST expressions* to the last one. They
were unreachable in production only because the `TOOROW_SOURCE_ABSENT` guards
(criterion 21) emptied the branches carrying them, which is a reprieve and not a
repair.

THE GATE HAS TWO HALVES, and only together do they say anything.

  * The OFFLINE half, below, always runs and needs no network. It refuses a new
    engine-specific TYPE, and a `SELECT ... WHERE` with no `FROM` -- the two
    defects the dry run actually found here, so both are held where they are
    cheapest to hold. Both allowlists are EMPTY, and both are ratchets: the
    sweep reached zero, so zero is the frozen number. A file added tomorrow with
    `CAST(x AS DOUBLE)` fails here, in a second, at the moment it is written.
  * The BIGQUERY half runs `QueryJobConfig(dry_run=True)` over the COMPILED SQL
    -- BigQuery parses, resolves and plans it, then bills 0 bytes. It SKIPS when
    no credentials are reachable, because a test that cannot measure must say so
    rather than pass. The work itself lives in
    `scripts/dbt_bigquery_dry_run.py`, which is also how the deploy runs it.

Why both. The offline half is lexical: it knows the spellings we have already
been bitten by, and nothing else. The dry run knows the whole dialect -- `::`,
`QUALIFY`, `COUNT(*) FILTER (WHERE ...)`, `SELECT * EXCLUDE (...)`, a function
that exists on one engine only -- but it needs credentials and a compile. Neither
alone is the criterion, and the order in which they were built is the proof: the
type sweep left the project lexically clean, and the dry run then found 16 more
refusals of four other kinds. This is the instrument-must-not-measure-its-own-copy
rule applied to a dialect -- the lexical half reads the SOURCE, the dry run reads
what the ENGINE was actually handed.

WHAT THE PERIMETER MISSED, and why it is now wider (2026-08-31, second pass).
The sweep above swept `dbt/models` and `dbt/tests` and nothing else. The FIRST
real per-project dbt run on BigQuery -- revision mcp-server-00225, production --
then failed on two classes it had never looked at:

  * `server/modules/*/dbt/**`. Sixty staging and mart relations, shipped INSIDE
    the connector modules, compiled into the same project by the module loader
    and never scanned. `stg_youtube_breakdown` died on *Function not found:
    list_contains*; widening the scan found one more, a bare `DOUBLE` in
    `fact_gbp_review_rollup`, that nothing else would have caught.
  * `accepted_values` YAML. dbt QUOTES every value of that test unless told
    `quote: false`, so an INT64 or BOOL column gets `x IN ('1','2','3')` and
    BigQuery answers *No matching signature for operator IN for argument types
    INT64 and {STRING}*. Nothing in the SQL is wrong; the defect is in a .yml,
    which a scanner that only reads .sql cannot see.

THE REPAIR PATTERN, for whoever this test stops: `dbt/macros/engine_types.sql`.
`toorow_float_type()`, `toorow_string_type()`, `toorow_decimal_type(p, s)`, and
`toorow_exact_money_type()` for money; `toorow_star_except(alias, [cols])` for
`SELECT * EXCLUDE/EXCEPT (...)`. Never a second convention, never a bare type
name. For a `WHERE` with no `FROM`: `FROM (SELECT 1) AS one_row`, which both
engines accept and which needs no branch at all. For a DuckDB-only FUNCTION,
prefer a rewrite both engines already accept over a fourth macro --
`normalize_dimension` went from `list_contains(string_split(a, '|'), v)` to
`STRPOS('|' || a || '|', '|' || v || '|') > 0`, which needs no branch. For
`accepted_values` on a non-string column: `quote: false` beside `values:`,
under `arguments:`. For a `LIMIT` inside a subquery: an AGGREGATE, `(SELECT
MIN(col) FROM t WHERE ...)`.

AND WHAT THE PERIMETER STILL MISSES, one run later (2026-08-31, third pass).
A compiling relation is not a running one. `stg_youtube_breakdown` came back
green from the dry run and green from the local build, and its `not_null` tests
still died in production on *Correlated subqueries that reference other tables
are not supported* -- because BigQuery resolves a view's NAMES without planning
its scalar subqueries, and the dry run of a module staging never gets past name
resolution at all: its raw source does not exist in the warehouse, so the node
is reported `absent` and its SQL is never judged. That blind spot is now NAMED
by `scripts/dbt_bigquery_dry_run.py` (a `NEVER MEASURED` section, counted per
run) instead of being folded into a serene `source-absent` total, and the class
itself is held below, offline, where no warehouse is needed to see it.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
DBT_DIR = REPO_ROOT / "dbt"
MODULES_DIR = REPO_ROOT / "server" / "modules"
SCRIPT = REPO_ROOT / "scripts" / "dbt_bigquery_dry_run.py"

#: `dbt/macros/**` is where the two spellings are ALLOWED to appear -- that is
#: what a dialect-aware macro is. `target/` is a compile artifact of one engine
#: or the other and is never a source. Both are excluded by PATH PART rather
#: than by a per-file judgement, so a new macro or a new module is covered the
#: day it is written.
EXCLUDED_PARTS = frozenset({"macros", "target"})


def _sources(root: pathlib.Path, suffix: str) -> list[pathlib.Path]:
    return [
        p
        for p in sorted(root.rglob(f"*{suffix}"))
        if p.is_file() and not (EXCLUDED_PARTS & set(p.relative_to(root).parts))
    ]


def swept_sql_files() -> list[pathlib.Path]:
    """Every .sql dbt compiles, in BOTH places it is written.

    `dbt/models` and `dbt/tests` are the project's own. `server/modules/*/dbt`
    is the OTHER half -- a connector ships its staging and its marts inside its
    module directory, `dbt_project.yml` names those paths, and they compile into
    exactly the same manifest. A perimeter that reads only the first half says
    nothing about a production run, which is how `list_contains` reached it.
    """
    files = [p for root in (DBT_DIR / "models", DBT_DIR / "tests") for p in _sources(root, ".sql")]
    files += [
        p
        for module in sorted(MODULES_DIR.glob("*/dbt"))
        if module.is_dir()
        for p in _sources(module, ".sql")
    ]
    return sorted(set(files))


def swept_yaml_files() -> list[pathlib.Path]:
    """Every .yml that can declare a generic test, same two halves."""
    files = _sources(DBT_DIR, ".yml")
    files += [
        p
        for module in sorted(MODULES_DIR.glob("*/dbt"))
        if module.is_dir()
        for p in _sources(module, ".yml")
    ]
    return sorted(set(files))

#: The three spellings production refuses. `DOUBLE` and `VARCHAR` do not exist
#: in BigQuery at all (`FLOAT64`, `STRING`); a parameterised `DECIMAL(p, s)` is
#: refused in a CAST outright.
ENGINE_SPECIFIC = re.compile(r"\bDOUBLE\b|\bVARCHAR\b|DECIMAL\s*\(")

#: FROZEN ALLOWLIST -- deliberately empty. The sweep of 2026-08-30 reached zero,
#: so the ratchet is at zero. This list may only ever SHRINK: a new entry means
#: a relation production cannot run, and the repair is a macro, not a line here.
ALLOWLIST: dict[str, str] = {}

#: The tokens the FROM-less scan needs to see, with string literals taken WHOLE
#: so a `where` written in prose inside a quoted failure message never counts.
SQL_TOKEN = re.compile(r"'(?:[^']|'')*'|\bSELECT\b|\bFROM\b|\bWHERE\b|\bUNION\b|[()]", re.I)

#: Second frozen allowlist, same rule, also empty: 98 `SELECT ... WHERE` blocks
#: with no `FROM` were repaired on 2026-08-31 (`FROM (SELECT 1) AS one_row`).
FROMLESS_ALLOWLIST: dict[str, str] = {}

#: THIRD CLASS -- a FUNCTION only DuckDB has. BigQuery answers *Function not
#: found: x*, which is a compile-time refusal and not a runtime one, so it takes
#: the whole node down. Every entry below was checked against BigQuery's
#: function list: none of them exists there under this name.
#:
#: `list_*` is matched by PREFIX because DuckDB's list library is open-ended --
#: `list_contains` is the one that reached production, and `list_transform`,
#: `list_filter`, `list_sort` would each have done the same. `len(` and
#: `printf(` are DuckDB's too, and are spelled with a word boundary so a column
#: named `problem_len` never counts.
DUCKDB_ONLY = re.compile(
    r"\blist_[a-z_]+\s*\("
    r"|\bstring_split(_regex)?\s*\("
    r"|\bstr_split\s*\("
    r"|\bstruct_(pack|extract|insert)\s*\("
    r"|\bregexp_matches\s*\("
    r"|\bstr(f|p)time\s*\("
    r"|\bepoch(_ms|_us|_ns)?\s*\("
    r"|\bprintf\s*\("
    r"|\btry_cast\s*\("
    r"|\blen\s*\("
    r"|\bnextval\s*\("
    r"|\barray_slice\s*\("
    r"|\bbool_(or|and)\s*\("
    r"|\bILIKE\b",
    re.I,
)

#: `bool_or` / `bool_and` joined the list on 2026-08-31, and how they got there
#: is the argument for this whole file. They are Postgres's and DuckDB's;
#: BigQuery spells the pair `LOGICAL_OR` / `LOGICAL_AND` and answers *Function
#: not found: BOOL_OR*. Six call sites over the three `plan_pacing_*` marts
#: carried them, under a comment asserting the opposite ("BOOL_OR is portable
#: (DuckDB + BigQuery)"), and BOTH halves of the gate were blind: the scan did
#: not know the name, and the dry run never planned those models because they
#: read `mirror.*`, absent from that warehouse. Only a real per-project build
#: found them. The repair is `toorow_bool_or()` / `toorow_bool_and()`
#: (dbt/macros/engine_types.sql), which emit `MAX/MIN(CASE ...) = 1` -- one
#: spelling, no dialect branch.

#: Third frozen allowlist, also empty. `normalize_dimension` carried the only
#: two occurrences (`list_contains`, `string_split`) and was rewritten portably
#: on 2026-08-31, so zero is the measured number.
DUCKDB_ONLY_ALLOWLIST: dict[str, str] = {}

#: FOURTH CLASS -- the star modifier. DuckDB writes `SELECT t.* EXCLUDE (c)`,
#: BigQuery writes `SELECT t.* EXCEPT (c)`, and each refuses the other's word.
#: Both spellings are refused HERE because the repo has one way to say it,
#: `toorow_star_except()`, and a file that hardcodes either word is a file that
#: builds on one engine only. Matching `.*` before the keyword is what keeps
#: this off `SELECT ... EXCEPT SELECT ...`, the set operator, which is fine.
STAR_MODIFIER = re.compile(r"\*\s*(EXCLUDE|EXCEPT)\s*\(", re.I)

#: Fourth frozen allowlist, also empty: five connector stagings carried
#: `EXCLUDE (metric)` and now call the macro.
STAR_MODIFIER_ALLOWLIST: dict[str, str] = {}

#: FIFTH CLASS, and the only one that lives in YAML rather than SQL. dbt's
#: `accepted_values` renders every value inside single quotes unless it is
#: passed `quote: false`, so the test compiles to `x IN ('1','2','3')` over an
#: INT64 column and BigQuery answers *No matching signature for operator IN for
#: argument types INT64 and {STRING}*. DuckDB coerces and says nothing.
#:
#: Fifth frozen allowlist, also empty: three sites carried the defect
#: (`fee_tax_rules_effective.scope_precedence`, INT64; the two epic41 fixture
#: `expected_is_complete` columns, BOOL) and all three now pass `quote: false`.
ACCEPTED_VALUES_ALLOWLIST: dict[str, str] = {}

#: SIXTH CLASS -- a `LIMIT` inside a subquery. BigQuery: *Correlated subqueries
#: that reference other tables are not supported unless they can be
#: de-correlated, such as by transforming them into an efficient JOIN*.
#:
#: THIS ONE IS NOT A SPELLING, and it is the reason it took a second production
#: run to find. `stg_youtube_breakdown` COMPILED -- BigQuery resolves the names
#: of a view without planning its scalar subqueries -- and then its `not_null`
#: tests died on this, because a test wraps the view in a query BigQuery must
#: actually plan (revision mcp-server-00226, project
#: proj_01KZGCRSV2XACWRP3RSVNWWGBK). DuckDB plans the same SQL without complaint.
#:
#: WHAT DECIDES IT IS THE `LIMIT`, not the correlation predicate. Six constructs
#: were put to BigQuery directly on 2026-08-31, one dry run each, 0 bytes billed:
#:
#:     scalar subquery + LIMIT 1 + STRPOS(...) > 0      REFUSED
#:     scalar subquery + LIMIT 1 + equality             REFUSED
#:     scalar subquery + ARRAY_AGG(... LIMIT 1)         REFUSED
#:     scalar subquery + MIN(...) + STRPOS(...) > 0     ACCEPTED
#:     scalar subquery + MIN(...) + equality            ACCEPTED
#:     LEFT JOIN ... ON STRPOS(...) > 0                 ACCEPTED
#:
#: So the repair is an AGGREGATE (`MIN(col)` instead of `col ... LIMIT 1`), or a
#: JOIN, and never a dialect branch: both engines accept both forms.
#:
#: WHAT THIS SCANNER CANNOT SEE, said plainly. It is lexical, so it cannot tell a
#: CORRELATED subquery from an uncorrelated one -- deciding that needs the scope
#: of every alias, which is a parser this file deliberately does not carry. It
#: therefore refuses `LIMIT` inside ANY parenthesised sub-select, which is
#: STRICTER than BigQuery: an uncorrelated `FROM (SELECT ... LIMIT 10) x` is
#: legal there and is refused here. That over-reach is cheap on purpose -- the
#: repo has never had one, and the rewrite costs a line -- while the under-reach
#: it replaces cost a production outage. It also cannot see a `LIMIT` that only
#: EXISTS after jinja renders (a macro that composes the word from variables);
#: `dbt/macros/**` is scanned here for exactly that reason, which is the one
#: place this scan looks and the other five do not.
SUBQUERY_LIMIT_TOKEN = re.compile(r"'(?:[^']|'')*'|\bLIMIT\b|[()]", re.I)

#: Sixth frozen allowlist, also empty. `normalize_dimension` carried the only
#: occurrence in the whole project (measured 2026-08-31: one `LIMIT 1`, in
#: `dbt/macros/normalize_dimension.sql`, reaching six connector stagings) and it
#: is now `SELECT MIN(canonical_col)`.
SUBQUERY_LIMIT_ALLOWLIST: dict[str, str] = {}

#: SEVENTH CLASS -- a UNION arm that fills a temporal column with a literal of
#: another type. BigQuery: *Column N in UNION ALL has incompatible types: DATE,
#: DATE, STRING, STRING*, and it refuses to PLAN the node -- on any warehouse,
#: empty or not, before a single row is read. DuckDB coerces the two and says
#: nothing, which is why the local loop was green.
#:
#: WHERE IT COMES FROM, every time: an anti-vacuity branch. A singular test says
#: "this relation was empty, I compared nothing" and has to emit the same column
#: list as the real branches, so its payload columns get placeholders -- and `''`
#: is the reflex for all of them. It is right for a STRING column and wrong for a
#: DATE one. Found 2026-09-01 in `test_cross_source_revenue_states_its_money_gap`
#: by the dry run, once the gate started planning tests against this checkout's
#: own model SQL rather than against the relations they name.
#:
#: The repair is `CAST(NULL AS DATE)` (or the column's own type), which both
#: engines accept and which says the true thing: a branch that names no day
#: should return NO day rather than the empty string.
#:
#: Lexical, therefore narrow ON PURPOSE: it refuses a bare literal aliased to a
#: column whose NAME is temporal (`date`, `*_date`, `*_at`), which is the shape
#: every occurrence has had, and it says nothing about a numeric column filled
#: with `''` -- BigQuery would refuse that too, and only the dry run can see it.
#: A literal that already carries its type is not a hit -- `CAST('...' AS DATE)`
#: and the prefixed form `DATE '2026-07-15'` are both the repair, not the defect.
TEMPORAL_SENTINEL = re.compile(
    r"(?<![\w.])(?P<literal>'(?:[^']|'')*'|[0-9]+(?:\.[0-9]+)?)"
    r"\s+AS\s+(?P<column>date|\w+_date|\w+_at)\b",
    re.I,
)

#: What comes IMMEDIATELY before a literal that is already typed. Both engines
#: accept both spellings, and seven lines of `dbt/tests` use them.
TYPED_LITERAL_PREFIX = re.compile(r"(?:CAST\(|\b(?:DATE|DATETIME|TIMESTAMP|TIME))\s*$", re.I)

#: Seventh frozen allowlist, empty like the other six. One occurrence in the whole
#: project (two lines of one singular test), repaired 2026-09-01.
TEMPORAL_SENTINEL_ALLOWLIST: dict[str, str] = {}


def strip_comments(text: str) -> str:
    """Blank `{# #}`, `/* */` and `--` comments, keeping line and column geometry.

    Comments are removed and STRING LITERALS ARE NOT, on purpose. A jinja
    `{%- do parts.append("... AS VARCHAR ...") -%}` is SQL this project emits;
    it is exactly the site the raw grep of the criterion could not tell from a
    comment, and it was a real defect. Prose that happens to contain
    `DOUBLE-COUNT SAFETY` is not.

    IT IS A STATE MACHINE AND NOT THREE REGEXES, because an apostrophe inside a
    string is enough to fool the regexes: `'... alias rows, ' || '... -- the
    seed did not run'` carries a `--` INSIDE a literal, and blanking from there
    to the end of line eats the closing quote and desynchronises everything
    after it. Measured 2026-08-31: that single line hid 36 further defects from
    the first version of this scanner. An instrument a quote can fool is not an
    instrument.
    """
    out = list(text)
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < n:
                if text[i] == "\\" and quote != "`":
                    i += 2
                    continue
                if text[i] == quote:
                    if quote == "'" and i + 1 < n and text[i + 1] == "'":
                        i += 2  # SQL escapes a quote by doubling it
                        continue
                    i += 1
                    break
                i += 1
            continue
        for opener, closer in (("{#", "#}"), ("/*", "*/"), ("--", "\n")):
            if text.startswith(opener, i):
                end = text.find(closer, i)
                end = n if end == -1 else (end if closer == "\n" else end + len(closer))
                for j in range(i, end):
                    if out[j] != "\n":
                        out[j] = " "
                i = end
                break
        else:
            i += 1
    return "".join(out)


def scan_engine_specific_types() -> dict[str, list[tuple[int, str]]]:
    """{repo-relative path: [(line number, the line as written), ...]}."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in swept_sql_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        raw = path.read_text(encoding="utf-8")
        if not ENGINE_SPECIFIC.search(raw):
            continue
        source = raw.split("\n")
        hits = [
            (i + 1, source[i].strip())
            for i, line in enumerate(strip_comments(raw).split("\n"))
            if ENGINE_SPECIFIC.search(line)
        ]
        if hits:
            found[rel] = hits
    return found


def scan_fromless_where() -> dict[str, list[tuple[int, str]]]:
    """Every `SELECT ... WHERE` whose own level carries no `FROM`.

    BigQuery: *Query without FROM clause cannot have a WHERE clause*. DuckDB
    accepts it, and the repo's `cardinality_guard` idiom -- a row of literals
    emitted only when a fixture is empty -- was written that way 98 times.

    Levels are counted in parentheses from the `SELECT`, so a `WHERE` inside a
    scalar subquery (`WHERE (SELECT COUNT(*) FROM x) = 0`) belongs to the inner
    query and is not the one being judged.
    """
    found: dict[str, list[tuple[int, str]]] = {}
    for path in swept_sql_files():
        raw = path.read_text(encoding="utf-8")
        text = strip_comments(raw)
        tokens = list(SQL_TOKEN.finditer(text))
        hits: list[tuple[int, str]] = []
        for i, token in enumerate(tokens):
            if token.group(0).upper() != "SELECT":
                continue
            depth = 0
            for j in range(i + 1, len(tokens)):
                value = tokens[j].group(0)
                upper = value.upper()
                if value == "(":
                    depth += 1
                elif value == ")":
                    if depth == 0:
                        break
                    depth -= 1
                elif depth == 0 and upper in ("FROM", "UNION", "SELECT"):
                    break
                elif depth == 0 and upper == "WHERE":
                    line = text.count("\n", 0, tokens[j].start()) + 1
                    hits.append((line, raw.split("\n")[line - 1].strip()))
                    break
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def test_no_where_without_a_from() -> None:
    """Offline half, second defect. BigQuery refuses a WHERE with no FROM."""
    found = scan_fromless_where()
    unexpected = {p: hits for p, hits in found.items() if p not in FROMLESS_ALLOWLIST}
    assert not unexpected, (
        "BigQuery answers *Query without FROM clause cannot have a WHERE clause* to "
        "these, so production cannot run them (execution-substrate.md 23).\n"
        "Add `FROM (SELECT 1) AS one_row` above the WHERE -- both engines accept it, "
        "and it needs no dialect branch.\n"
        + "\n".join(
            f"  {path}:{line}  {text[:120]}"
            for path, hits in sorted(unexpected.items())
            for line, text in hits
        )
    )


def test_no_engine_specific_type_outside_the_macros() -> None:
    """Offline half. A bare DuckDB type in a model or a test is a production outage."""
    found = scan_engine_specific_types()
    unexpected = {path: hits for path, hits in found.items() if path not in ALLOWLIST}
    assert not unexpected, (
        "These dbt nodes carry SQL only DuckDB accepts, so production cannot run them "
        "(execution-substrate.md 'Incomplete if' 23).\n"
        "Replace the type with a macro from dbt/macros/engine_types.sql: "
        "toorow_float_type(), toorow_string_type(), toorow_decimal_type(p, s), "
        "or toorow_exact_money_type() for money.\n"
        + "\n".join(
            f"  {path}:{line}  {text[:120]}"
            for path, hits in sorted(unexpected.items())
            for line, text in hits
        )
    )



def scan_duckdb_only_functions() -> dict[str, list[tuple[int, str]]]:
    """Every call to a function BigQuery does not have."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in swept_sql_files():
        raw = path.read_text(encoding="utf-8")
        if not DUCKDB_ONLY.search(raw):
            continue
        source = raw.split("\n")
        hits = [
            (i + 1, source[i].strip())
            for i, line in enumerate(strip_comments(raw).split("\n"))
            if DUCKDB_ONLY.search(line)
        ]
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def scan_bare_star_modifier() -> dict[str, list[tuple[int, str]]]:
    """Every hardcoded `* EXCLUDE (...)` or `* EXCEPT (...)` outside the macros."""
    found: dict[str, list[tuple[int, str]]] = {}
    for path in swept_sql_files():
        raw = path.read_text(encoding="utf-8")
        if not STAR_MODIFIER.search(raw):
            continue
        source = raw.split("\n")
        hits = [
            (i + 1, source[i].strip())
            for i, line in enumerate(strip_comments(raw).split("\n"))
            if STAR_MODIFIER.search(line)
        ]
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def subquery_limit_files() -> list[pathlib.Path]:
    """The swept files PLUS `dbt/macros/**` -- the sixth class lived in a macro.

    Every other scan excludes `macros/` because that is where the two dialects
    are ALLOWED to appear side by side. This one does not: a `LIMIT` is not a
    dialect spelling, it is refused on BigQuery wherever it is written, and the
    single occurrence the project ever had was inside a macro reaching six
    connector stagings. Excluding macros here would have measured every call
    site and missed the definition.
    """
    files = list(swept_sql_files())
    files += [p for p in sorted((DBT_DIR / "macros").rglob("*.sql")) if p.is_file()]
    return sorted(set(files))


def scan_subquery_limit() -> dict[str, list[tuple[int, str]]]:
    """Every `LIMIT` written inside parentheses, i.e. inside a sub-select.

    Depth is counted over the file with string literals taken WHOLE, the same
    tokenising discipline `scan_fromless_where` uses: a parenthesis inside a
    quoted failure message must not move the depth, or the instrument reports
    the wrong lines for the rest of the file.
    """
    found: dict[str, list[tuple[int, str]]] = {}
    for path in subquery_limit_files():
        raw = path.read_text(encoding="utf-8")
        text = strip_comments(raw)
        if not re.search(r"\bLIMIT\b", text, re.I):
            continue
        depth = 0
        hits: list[tuple[int, str]] = []
        for token in SUBQUERY_LIMIT_TOKEN.finditer(text):
            value = token.group(0)
            if value == "(":
                depth += 1
            elif value == ")":
                depth = max(0, depth - 1)
            elif value.upper() == "LIMIT" and depth > 0:
                line = text.count("\n", 0, token.start()) + 1
                hits.append((line, raw.split("\n")[line - 1].strip()))
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def test_no_limit_inside_a_subquery() -> None:
    """Offline half, sixth defect. BigQuery cannot de-correlate a LIMIT subquery."""
    found = scan_subquery_limit()
    unexpected = {p: hits for p, hits in found.items() if p not in SUBQUERY_LIMIT_ALLOWLIST}
    assert not unexpected, (
        "A `LIMIT` inside a subquery is what BigQuery cannot de-correlate: it answers "
        "*Correlated subqueries that reference other tables are not supported unless "
        "they can be de-correlated* and takes the node down, while DuckDB plans it "
        "silently (execution-substrate.md 23). This is the class that killed the "
        "`stg_youtube_breakdown` not_null tests on the second real per-project run, "
        "AFTER the view itself had started compiling.\n"
        "Write an AGGREGATE instead -- `(SELECT MIN(col) FROM t WHERE ...)` for the "
        "one-value lookup, which both engines accept and which is deterministic where "
        "`LIMIT 1` was not -- or lift the subquery into a CTE and JOIN it.\n"
        + "\n".join(
            f"  {path}:{line}  {text[:120]}"
            for path, hits in sorted(unexpected.items())
            for line, text in hits
        )
    )


def scan_temporal_sentinel() -> dict[str, list[tuple[int, str]]]:
    """Every bare literal aliased to a column whose name is a date or a timestamp.

    `CAST('2026-05-01' AS DATE)` and `DATE '2026-07-15'` are excluded because the
    literal there is already typed -- seven such lines exist in `dbt/tests` and
    none of them is the defect.
    """
    found: dict[str, list[tuple[int, str]]] = {}
    for path in swept_sql_files():
        raw = path.read_text(encoding="utf-8")
        text = strip_comments(raw)
        hits: list[tuple[int, str]] = []
        for match in TEMPORAL_SENTINEL.finditer(text):
            before = text[: match.start("literal")]
            if TYPED_LITERAL_PREFIX.search(before):
                continue
            line = text.count("\n", 0, match.start()) + 1
            hits.append((line, raw.split("\n")[line - 1].strip()))
        if hits:
            found[path.relative_to(REPO_ROOT).as_posix()] = hits
    return found


def test_no_untyped_literal_in_a_temporal_column() -> None:
    """Offline half, seventh defect. A UNION arm must carry the column's own type."""
    found = scan_temporal_sentinel()
    unexpected = {p: hits for p, hits in found.items() if p not in TEMPORAL_SENTINEL_ALLOWLIST}
    assert not unexpected, (
        "A bare literal aliased to a temporal column is what BigQuery refuses in a "
        "UNION: *Column N in UNION ALL has incompatible types: DATE, DATE, STRING, "
        "STRING*, and it refuses to PLAN the node -- so the relation being empty "
        "saves nothing. DuckDB coerces the two and the local loop stays green "
        "(execution-substrate.md 23).\n"
        "This is the anti-vacuity branch's reflex: `'' AS date` beside `'' AS "
        "project_id`. Write `CAST(NULL AS DATE)` -- both engines accept it, and a "
        "branch that names no day should return NO day.\n"
        + "\n".join(
            f"  {path}:{line}  {text[:120]}"
            for path, hits in sorted(unexpected.items())
            for line, text in hits
        )
    )


def _accepted_values_sites(document, path: str):
    """Yield (test name, values, quote, has_arguments) for every accepted_values.

    The walk is generic rather than schema-aware on purpose: `accepted_values`
    is declared under `models:`, `seeds:`, `sources:` and `snapshots:`, at the
    column level and at the model level, and a walk that enumerated those places
    would miss the next one somebody uses.
    """
    if isinstance(document, dict):
        for key, value in document.items():
            if key == "accepted_values" and isinstance(value, dict):
                arguments = value.get("arguments")
                nested = isinstance(arguments, dict)
                holder = arguments if nested else value
                yield (
                    value.get("name") or holder.get("column_name") or "<unnamed>",
                    holder.get("values"),
                    holder.get("quote"),
                    nested,
                )
            else:
                yield from _accepted_values_sites(value, path)
    elif isinstance(document, list):
        for item in document:
            yield from _accepted_values_sites(item, path)


def scan_accepted_values() -> dict[str, list[tuple[str, str]]]:
    """{path: [(test name, what is wrong), ...]} over every dbt .yml.

    TWO RULES, and both were measured on 2026-08-31.

      * A value that is not a YAML string needs `quote: false`. `values: [1, 2,
        3]` on an INT64 column renders `IN ('1','2','3')` and BigQuery refuses
        the comparison outright.
      * Arguments belong under `arguments:`. dbt 1.11 raises
        `MissingArgumentsPropertyInGenericTestDeprecation` for the flat shape,
        and it is the shape the next major stops accepting -- one convention,
        so the answer to "where does `quote` go" is never a guess.

    What this CANNOT see is a BOOL column whose values are the strings "true"
    and "false": the type lives in the warehouse, not in the yaml. That half is
    the dry run's, which is the division of labour this whole file is built on.
    """
    found: dict[str, list[tuple[str, str]]] = {}
    for path in swept_yaml_files():
        text = path.read_text(encoding="utf-8")
        if "accepted_values" not in text:
            continue
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            found[path.relative_to(REPO_ROOT).as_posix()] = [("<parse>", str(exc)[:200])]
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        problems: list[tuple[str, str]] = []
        for name, values, quote, nested in _accepted_values_sites(document, rel):
            if not nested:
                problems.append(
                    (name, "its arguments are not under `arguments:` (dbt deprecation)")
                )
            if isinstance(values, list):
                literal = [v for v in values if not isinstance(v, str)]
                if literal and quote is not False:
                    problems.append(
                        (
                            name,
                            "values %r are not strings, so the column is not a string "
                            "either -- add `quote: false` beside `values:`" % (literal,),
                        )
                    )
        if problems:
            found[rel] = problems
    return found


def test_no_duckdb_only_function() -> None:
    """Offline half, third defect. BigQuery answers *Function not found*."""
    found = scan_duckdb_only_functions()
    unexpected = {p: hits for p, hits in found.items() if p not in DUCKDB_ONLY_ALLOWLIST}
    assert not unexpected, (
        "These call a function DuckDB has and BigQuery does not, so production cannot "
        "run them (execution-substrate.md 23). This is the class that took "
        "`stg_youtube_breakdown` down on the first real per-project run.\n"
        "Prefer a rewrite both engines already accept -- `list_contains(string_split("
        "a, '|'), v)` became `STRPOS('|' || a || '|', '|' || v || '|') > 0` -- and a "
        "macro in dbt/macros/engine_types.sql only when no such rewrite is readable.\n"
        + "\n".join(
            f"  {path}:{line}  {text[:120]}"
            for path, hits in sorted(unexpected.items())
            for line, text in hits
        )
    )


def test_no_hardcoded_star_modifier() -> None:
    """Offline half, fourth defect. EXCLUDE is DuckDB's word, EXCEPT is BigQuery's."""
    found = scan_bare_star_modifier()
    unexpected = {p: hits for p, hits in found.items() if p not in STAR_MODIFIER_ALLOWLIST}
    assert not unexpected, (
        "`SELECT t.* EXCLUDE (...)` is DuckDB's spelling and `SELECT t.* EXCEPT (...)` "
        "is BigQuery's; each engine refuses the other's, so either word written by "
        "hand builds on one engine only (execution-substrate.md 23).\n"
        "Write `{{ toorow_star_except('t', ['col']) }}` instead.\n"
        + "\n".join(
            f"  {path}:{line}  {text[:120]}"
            for path, hits in sorted(unexpected.items())
            for line, text in hits
        )
    )


def test_accepted_values_declares_its_types() -> None:
    """Offline half, fifth defect -- and the only one that lives in a .yml."""
    found = scan_accepted_values()
    unexpected = {p: hits for p, hits in found.items() if p not in ACCEPTED_VALUES_ALLOWLIST}
    assert not unexpected, (
        "dbt quotes every `accepted_values` value unless told otherwise, so a numeric "
        "or boolean column is compared against strings and BigQuery answers *No "
        "matching signature for operator IN* (execution-substrate.md 23).\n"
        "One convention: arguments under `arguments:`, and `quote: false` beside "
        "`values:` whenever the column is not a string.\n"
        + "\n".join(
            f"  {path}  {name}: {why}"
            for path, hits in sorted(unexpected.items())
            for name, why in hits
        )
    )


def test_the_allowlists_only_shrink() -> None:
    """A frozen exception that no longer applies must be DELETED, not kept warm."""
    for name, allowlist, found in (
        ("ALLOWLIST", ALLOWLIST, scan_engine_specific_types()),
        ("FROMLESS_ALLOWLIST", FROMLESS_ALLOWLIST, scan_fromless_where()),
        ("DUCKDB_ONLY_ALLOWLIST", DUCKDB_ONLY_ALLOWLIST, scan_duckdb_only_functions()),
        ("STAR_MODIFIER_ALLOWLIST", STAR_MODIFIER_ALLOWLIST, scan_bare_star_modifier()),
        ("ACCEPTED_VALUES_ALLOWLIST", ACCEPTED_VALUES_ALLOWLIST, scan_accepted_values()),
        ("SUBQUERY_LIMIT_ALLOWLIST", SUBQUERY_LIMIT_ALLOWLIST, scan_subquery_limit()),
        ("TEMPORAL_SENTINEL_ALLOWLIST", TEMPORAL_SENTINEL_ALLOWLIST, scan_temporal_sentinel()),
    ):
        stale = sorted(set(allowlist) - set(found))
        assert not stale, (
            f"These entries no longer carry the defect; remove them from {name} so the "
            f"ratchet stays at its measured number: {stale}"
        )
        unreasoned = sorted(path for path, reason in allowlist.items() if not reason.strip())
        assert not unreasoned, (
            f"Every {name} entry states its reason; these do not: {unreasoned}"
        )


def _gate_module():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import dbt_bigquery_dry_run as gate  # noqa: PLC0415 - the script is not a package

    return gate


def _bigquery_credentials() -> tuple[bool, str]:
    try:
        gate = _gate_module()
    except ImportError as exc:  # pragma: no cover - the script is committed beside this test
        return False, f"cannot import scripts/dbt_bigquery_dry_run.py ({exc})"
    return gate.credentials_available()


# ---------------------------------------------------------------------------
# THE GATE MUST COVER THE TESTS, and these three hold that offline.
#
# A `materialized='view'` model is CREATED without being planned: BigQuery
# resolves the names and the types of a view and stops there. The only node that
# ever plans the body is a query that READS it -- which, in a dbt project, is a
# schema test. 912 of this project's 1006 compiled nodes are tests, and each one
# names a RELATION, so dry-running it as written judges the warehouse's last
# deployment rather than this checkout. That is how the correlated `LIMIT 1` of
# `stg_youtube_breakdown` reached production twice.
# ---------------------------------------------------------------------------


def test_the_gate_knows_a_test_node_from_a_model_node() -> None:
    """Both spellings dbt uses, or the split in the report is a guess."""
    gate = _gate_module()
    generic = pathlib.Path("target/compiled/x/models/staging/schema.yml/not_null_stg_a_date.sql")
    singular = pathlib.Path("target/compiled/x/dbt/tests/test_money_gap.sql")
    model = pathlib.Path("target/compiled/x/models/staging/stg_a.sql")
    assert gate.is_test_node(generic)
    assert gate.is_test_node(singular)
    assert not gate.is_test_node(model)


def test_the_gate_plans_a_test_against_this_checkout(tmp_path) -> None:
    """The test must carry the MODEL'S SQL, not the name of a deployed relation."""
    gate = _gate_module()
    body = tmp_path / "stg_a.sql"
    body.write_text("SELECT 1 AS date FROM `p`.`raw_x`.`t`\n", encoding="utf-8")
    inlined = gate.inline_model_bodies(
        "select date\nfrom `p`.`staging_x`.`stg_a`\nwhere date is null\n",
        {"stg_a": body},
    )
    assert "`p`.`staging_x`.`stg_a`" not in inlined, (
        "The test still names the relation, so a dry run of it would judge the view "
        "the warehouse holds -- the last deployment -- and say nothing about this tree."
    )
    assert "SELECT 1 AS date" in inlined
    # The leaf source is NOT a compiled model and must survive untouched.
    assert "`p`.`raw_x`.`t`" in inlined


def test_the_gate_does_not_inline_inside_a_comment() -> None:
    """dbt writes `-- depends_on: <relation>`; substituting there breaks the SQL."""
    gate = _gate_module()
    inlined = gate.inline_model_bodies(
        "-- depends_on: `p`.`marts_x`.`m`\nselect 1\n", {"m": pathlib.Path("never-read.sql")}
    )
    assert inlined.startswith("-- depends_on: `p`.`marts_x`.`m`"), (
        "A relation named in a comment is not a reference. Inlining there buries the "
        "opening parenthesis in the comment and leaves the model body dangling -- "
        "measured 2026-09-01: 76 nodes broke exactly this way."
    )


@pytest.mark.slow
def test_bigquery_accepts_the_compiled_project() -> None:
    """BigQuery half. Skips without credentials -- never passes without measuring.

    Opt-in because it compiles the whole project and issues one dry run per node
    (about 1000 of them): `TOOROW_BIGQUERY_DRY_RUN=1 uv run pytest -q
    tests/conformance/test_dbt_speaks_both_engines.py`. The deploy runs the
    script directly. Every dry run bills 0 bytes; the cost is wall clock, not
    money, which is why it is a pre-deploy gate and not an inline one.
    """
    if os.environ.get("TOOROW_BIGQUERY_DRY_RUN") != "1":
        pytest.skip("set TOOROW_BIGQUERY_DRY_RUN=1 to run the BigQuery dry-run half")
    ok, detail = _bigquery_credentials()
    if not ok:
        pytest.skip(f"no BigQuery credentials reachable: {detail}")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "BigQuery refused SQL this project compiles. The dry run bills 0 bytes and "
        "names each node:\n" + proc.stdout[-8000:] + proc.stderr[-2000:]
    )
