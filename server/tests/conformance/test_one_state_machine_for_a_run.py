"""One function writes ``app.datastream_executions.state``, and it is the machine.

THE DEFECT THIS FILE CLOSES -- AI-223. `datastream_publication` owns a typed,
closed state machine (`advance_state`: it locks the row, refuses an invalid
transition, stamps `state_changed_at`, closes the run's open step spans and
writes an audit row). Other statements wrote the same column with their own
`UPDATE`, each re-deriving a fragment of that machine in its own words --
:data:`CONVERTED_WRITERS` below is the census, and every count this repository
states about it is derived from that tuple rather than retyped.

Two of them (`datastream_activation`) wrote `state` WITHOUT `state_changed_at`
at all, and the table carries no trigger to make up for it -- migration 042 says
so in its own header. A run published through the wizard therefore measured its
duration up to `ready` and never up to `published`, and nothing closed its open
step spans. Story 58.10 paid two review verdicts on the belief that the machine
was already a seam; it was not, and a comment claiming so is not a guard.

This test is the guard. It does not name a state, a caller or a story: it reads
every SQL text the REPOSITORY holds -- Python and `.sql` alike, `server/` and
everything beside it -- and refuses a `SET state` on `app.datastream_executions`
written anywhere but inside `advance_state`. The next writer is refused before it
is committed, whoever writes it.

WHAT THE FIRST VERSION OF THIS GUARD PROVED, AND WHAT IT DID NOT (review of
2026-08-21). It proved a BRANCH, not a SCOPE, and three doors stood open:

* it read `server/` only, so `scripts/`, `infra/nango/migrations/*.sql` and
  `dbt/` could hold the eighth writer and stay green;
* it recognised a statement only inside a SINGLE string literal, so an author
  who wrote ``head = "UPDATE app.datastream_executions "`` on one line and
  ``tail = "SET state = %s ..."`` on the next was INVISIBLE to it. The literals
  of one function are now read joined as well as apart;
* its only shape was `UPDATE ... SET`. `INSERT ... ON CONFLICT DO UPDATE SET
  state` and `MERGE ... WHEN MATCHED THEN UPDATE SET state` write the same
  column and were not looked for. Both are looked for now.
"""

from __future__ import annotations

import ast
import os
import re
import warnings
from pathlib import Path

#: The repository root -- `server/tests/conformance/<this file>` is three levels
#: down. The scan is REPOSITORY-wide on purpose: a state write is a state write
#: whether it is typed in `server/core`, in a one-off under `scripts/`, or in a
#: migration nobody re-reads.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The one function allowed to write the column, and the file it lives in,
#: named from the repository root like everything else the scan reports.
OWNER_FILE = "server/core/datastream_publication.py"
OWNER_FUNCTION = "advance_state"

TABLE = "app.datastream_executions"

#: The private writers AI-223 converted, one entry per STATEMENT. MEASURED, not
#: remembered: check out the parent of the commit that converted them and run
#: this file's own scanner over it --
#:
#:     for f in datastream_publication datastream_activation execution_progress; do
#:         git show dddbb5ec^:server/core/$f.py > <tmp>/core/$f.py
#:     done
#:     # then run `_set_clauses` / `_ASSIGNS_STATE` of this file over <tmp>
#:
#: which reports EIGHT statements in SIX functions on that tree. One of the six
#: is `advance_state` itself, the machine: the private census is therefore seven
#: statements in five functions, and that subtraction is the whole reason the
#: retyped "six" was wrong in two different directions at once.
#:
#: Every number this repository states about the conversion -- in this file and
#: in `docs/product-architecture/datastream-workbench-and-wizard.md` -- is
#: derived from here. The ratified document said "six" and the docstring above
#: it said "SIX" over a list of seven; both were retyped rather than counted.
CONVERTED_WRITERS: tuple[tuple[str, str, str], ...] = (
    ("server/core/datastream_activation.py", "publish_activate_mutation", "ready -> publishing"),
    (
        "server/core/datastream_activation.py",
        "publish_activate_mutation",
        "publishing -> published",
    ),
    (
        "server/core/datastream_publication.py",
        "begin_managed_file_promotion",
        "ready -> publishing",
    ),
    ("server/core/datastream_publication.py", "commit_publication", "ready -> publishing"),
    ("server/core/datastream_publication.py", "commit_publication", "publishing -> published"),
    ("server/core/datastream_publication.py", "reconcile_execution", "publishing -> published"),
    ("server/core/datastream_publication.py", "_reconcile_fail_closed", "* -> failed"),
)

#: How many private `SET state` STATEMENTS AI-223 converted.
PRIVATE_STATEMENT_COUNT = len(CONVERTED_WRITERS)

#: How many private FUNCTIONS held them. Not the same number, which is exactly
#: why writing either of them by hand went wrong.
PRIVATE_FUNCTION_COUNT = len({(path, function) for path, function, _ in CONVERTED_WRITERS})

#: Directory names the scan never descends into: build output, dependencies,
#: virtualenvs, scratch copies of old source, and the test tree itself (it holds
#: doubles and fixtures that name the table on purpose). Any directory whose
#: name starts with `.` is skipped too -- `.git`, `.venv`, `.codex-tmp`.
_SKIPPED_DIRECTORIES = frozenset(
    {
        "__pycache__",
        "node_modules",
        "venv",
        "site-packages",
        "target",  # `dbt/target` -- compiled artefacts, not authored SQL.
        "dist",
        "build",
        "tests",
    }
)

#: The two languages a `SET state` can be typed in. `.sql` matters because
#: migrations and dbt models are outside every Python scan by construction.
_SOURCE_SUFFIXES = frozenset({".py", ".sql"})

#: `UPDATE app.datastream_executions [AS alias] SET ...`
_UPDATE = re.compile(
    r"\bUPDATE\s+" + re.escape(TABLE) + r"\b(?:\s+AS\s+\w+)?\s+SET\s+",
    re.IGNORECASE,
)
#: `INSERT INTO app.datastream_executions ... ON CONFLICT ... DO UPDATE SET ...`
#: -- an upsert writes the column of an EXISTING row, which is a transition by
#: another name. Bounded by `[^;]` so it cannot bridge two statements.
_UPSERT = re.compile(
    r"\bINSERT\s+INTO\s+"
    + re.escape(TABLE)
    + r"\b[^;]*?\bON\s+CONFLICT\b[^;]*?\bDO\s+UPDATE\s+SET\s+",
    re.IGNORECASE | re.DOTALL,
)
#: `MERGE INTO app.datastream_executions ... WHEN MATCHED THEN UPDATE SET ...`
#: -- Postgres 15 spells the same write this third way.
_MERGE = re.compile(
    r"\bMERGE\s+INTO\s+"
    + re.escape(TABLE)
    + r"\b[^;]*?\bWHEN\s+MATCHED\b[^;]*?\bUPDATE\s+SET\s+",
    re.IGNORECASE | re.DOTALL,
)
_STATEMENT_STARTS = (_UPDATE, _UPSERT, _MERGE)

#: What ends a SET clause. `FROM` matters: `execution_progress` writes progress
#: columns with `UPDATE ... AS e SET ... FROM ... AS prior WHERE`, and its WHERE
#: reads `state` without assigning it.
_END_OF_SET = re.compile(r"\b(WHERE|FROM|RETURNING)\b", re.IGNORECASE)
#: An assignment to the `state` column inside a SET clause.
_ASSIGNS_STATE = re.compile(r"(?:^|[\s,(])state\s*=", re.IGNORECASE)


def _repository_sources(root: Path | None = None) -> list[Path]:
    """Every authored `.py` and `.sql` file in the repository.

    Tests are excluded -- they hold doubles that name the table on purpose --
    and so is everything generated, vendored or scratch.
    """
    base = root if root is not None else REPO_ROOT
    found: list[Path] = []
    # `os.walk` and not `rglob`, because the skipped directories are PRUNED
    # rather than filtered afterwards: `node_modules` alone holds more files
    # than the repository does, and walking it turns this guard into something
    # nobody runs.
    for directory, subdirectories, filenames in os.walk(base):
        subdirectories[:] = [
            name
            for name in subdirectories
            if name not in _SKIPPED_DIRECTORIES and not name.startswith(".")
        ]
        for filename in filenames:
            if Path(filename).suffix in _SOURCE_SUFFIXES:
                found.append(Path(directory) / filename)
    return sorted(found)


def _string_value(node: ast.AST) -> str | None:
    """The literal text of a string constant or the literal parts of an f-string.

    An f-string is how `execution_progress` composes its progress writes; reading
    only its constant parts is enough, because a column NAME interpolated at
    runtime would itself be a defect this repository does not have.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return " ".join(
            part.value
            for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _literals_by_function(path: Path) -> dict[str, list[str]]:
    """Every string literal in the file, in source order, per enclosing function.

    ALL of them, not only the ones that name the table: a statement split across
    two literals puts the table in one and the `SET` in the other, and a filter
    applied literal by literal is exactly what made that split invisible.
    """
    try:
        # A file this guard merely READS must not make it print. Widening the
        # scan to the whole repository brought in sources that raise
        # `SyntaxWarning` on parse (`scratch/50_7_recipient_path.py`), and a
        # guard whose output is noise is a guard people stop reading.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - fails elsewhere, loudly.
        return {}

    found: dict[str, list[str]] = {}

    def walk(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, child.name)
                continue
            text = _string_value(child)
            if text is not None:
                found.setdefault(function, []).append(" ".join(text.split()))
            walk(child, function)

    walk(tree, "<module>")
    return found


def _texts_by_function(path: Path) -> list[tuple[str, str]]:
    """The SQL-bearing texts of one file, paired with the function they sit in.

    For a `.py` file each function yields its literals ONE BY ONE *and* JOINED.
    The joined reading is what closes the concatenation door: a statement whose
    table name and whose `SET` clause were typed as two separate strings shows
    up in the join even though neither half carries both.

    For a `.sql` file the whole text is one reading -- a migration has no
    functions to attribute a statement to, and its file name is the caller.
    """
    if path.suffix == ".sql":
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:  # pragma: no cover - authored SQL is UTF-8.
            return []
        return [("<sql>", " ".join(text.split()))] if TABLE in text else []

    texts: list[tuple[str, str]] = []
    for function, literals in _literals_by_function(path).items():
        joined = " ".join(literals)
        if TABLE not in joined:
            continue
        texts.extend((function, literal) for literal in literals if TABLE in literal)
        texts.append((function, joined))
    return texts


def _sql_by_function(path: Path) -> list[tuple[str, str]]:
    """The table-naming texts of one file -- kept as the name other tests use."""
    return _texts_by_function(path)


def _set_clauses(sql: str) -> list[str]:
    """The SET clause of every write to `app.datastream_executions` in one text.

    All three shapes: a plain `UPDATE`, the `DO UPDATE SET` of an upsert, and the
    `UPDATE SET` of a matched `MERGE` branch.
    """
    clauses = []
    for pattern in _STATEMENT_STARTS:
        for match in pattern.finditer(sql):
            rest = sql[match.end() :]
            end = _END_OF_SET.search(rest)
            clauses.append(rest[: end.start()] if end else rest)
    return clauses


def _writers_in(root: Path) -> list[tuple[str, str]]:
    """Every (relative path, function) under `root` that assigns the `state` column."""
    writers = []
    for path in _repository_sources(root):
        for function, sql in _texts_by_function(path):
            if any(_ASSIGNS_STATE.search(clause) for clause in _set_clauses(sql)):
                entry = (path.relative_to(root).as_posix(), function)
                if entry not in writers:
                    writers.append(entry)
    return writers


def _writers() -> list[tuple[str, str]]:
    """Every (relative path, function) in this repository that writes the column."""
    return _writers_in(REPO_ROOT)


# ---------------------------------------------------------------------------


def test_only_the_state_machine_writes_the_state_column() -> None:
    """A `SET state` anywhere else is a private copy of a machine that exists.

    The repair is never to add `state_changed_at` to the new statement -- it is
    to call `core.datastream_publication.advance_state`, which also refuses the
    invalid transition, closes the step spans and writes the audit row. If the
    transition you need is not in the machine, ADD IT to `_FORWARD` /
    `is_valid_transition` with its rule; do not write around it.
    """
    unexpected = [
        (path, function)
        for path, function in _writers()
        if (path, function) != (OWNER_FILE, OWNER_FUNCTION)
    ]
    assert unexpected == [], (
        "these write app.datastream_executions.state with their own UPDATE "
        f"instead of calling advance_state: {unexpected}"
    )


def test_the_machine_still_writes_the_column_it_owns() -> None:
    """The guard above cannot be satisfied by deleting the write entirely."""
    assert (OWNER_FILE, OWNER_FUNCTION) in _writers()


def test_the_scan_reaches_beyond_the_server_package() -> None:
    """A guard that reads one package proves a branch, not a rule.

    Named directories, not a count: `scripts/` holds one-offs that talk to the
    same database, `infra/nango/migrations/` holds every schema statement ever
    applied, and `dbt/` holds authored SQL. If any of the three stops being
    read, this guard would go green on a smaller repository than the one that
    exists.
    """
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in _repository_sources()}
    assert any(name.startswith("scripts/") and name.endswith(".py") for name in scanned)
    assert any(name.startswith("infra/nango/migrations/") for name in scanned)
    assert any(name.startswith("dbt/") and name.endswith(".sql") for name in scanned)
    # And the `.sql` half is really parsed: migration 042 creates the table.
    assert "infra/nango/migrations/042_datastream_candidate_registry.sql" in scanned


def test_the_scan_reaches_the_files_that_used_to_hold_the_copies() -> None:
    """A guard that reads nothing passes forever.

    Named here because the converted writers lived in exactly two files: if a
    refactor moves them and the scan stops seeing those files, the guard would
    go green on an empty reading.
    """
    scanned = {path.relative_to(REPO_ROOT).as_posix() for path in _repository_sources()}
    assert "server/core/datastream_publication.py" in scanned
    assert "server/core/datastream_activation.py" in scanned
    assert "server/core/execution_progress.py" in scanned
    # And the scan really parses them: those three files hold SQL naming the table.
    for name in (
        "server/core/datastream_publication.py",
        "server/core/datastream_activation.py",
        "server/core/execution_progress.py",
    ):
        assert _texts_by_function(REPO_ROOT / name), name


def test_a_progress_write_is_not_mistaken_for_a_state_write() -> None:
    """`execution_progress` updates the same table and reads `state` in its WHERE.

    The scan must look at the SET clause only; treating the whole statement as
    one blob would report a false writer and teach the next reader to ignore it.
    """
    progress = _texts_by_function(REPO_ROOT / "server/core/execution_progress.py")
    statements = [sql for _, sql in progress if _UPDATE.search(sql)]
    assert statements, "execution_progress no longer updates the executions table"
    assert any("state = ANY" in sql or "state = ANY" in sql.upper() for sql in statements), (
        "the fixture for this test is gone: no progress write reads `state` any more"
    )
    for sql in statements:
        for clause in _set_clauses(sql):
            assert not _ASSIGNS_STATE.search(clause), sql


def test_a_statement_composed_from_two_literals_is_still_seen(tmp_path: Path) -> None:
    """The door the review walked through: `head + tail`, and neither half alone.

    Before this, the scan filtered literal by literal on the table name, so a
    writer whose table name and whose `SET state` were typed as two strings was
    reported as `ecrivain detecte: False`. It is detected now, and this test is
    the attack itself so that nobody re-narrows the reading.
    """
    attacker = tmp_path / "sneaky.py"
    attacker.write_text(
        "def write_state(cur, run_id, state):\n"
        f'    head = "UPDATE {TABLE} "\n'
        '    tail = "SET state = %s WHERE id = %s"\n'
        "    cur.execute(head + tail, (state, run_id))\n",
        encoding="utf-8",
    )
    assert _writers_in(tmp_path) == [("sneaky.py", "write_state")]


def test_an_upsert_that_updates_the_state_is_still_seen(tmp_path: Path) -> None:
    """`INSERT ... ON CONFLICT DO UPDATE SET state` is a transition, spelled sideways."""
    attacker = tmp_path / "upsert.py"
    attacker.write_text(
        "def upsert_run(cur, run_id, state):\n"
        "    cur.execute(\n"
        f'        "INSERT INTO {TABLE} (id, state) VALUES (%s, %s) "\n'
        '        "ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state",\n'
        "        (run_id, state),\n"
        "    )\n",
        encoding="utf-8",
    )
    assert _writers_in(tmp_path) == [("upsert.py", "upsert_run")]


def test_a_merge_branch_that_updates_the_state_is_still_seen(tmp_path: Path) -> None:
    """The third spelling, and the one a `.sql` file is most likely to use."""
    attacker = tmp_path / "merge.sql"
    attacker.write_text(
        f"MERGE INTO {TABLE} AS e\n"
        "USING app.pull_jobs AS j ON j.execution_id = e.id\n"
        "WHEN MATCHED THEN UPDATE SET state = 'failed';\n",
        encoding="utf-8",
    )
    assert _writers_in(tmp_path) == [("merge.sql", "<sql>")]


def test_a_state_write_outside_the_server_package_is_still_seen(tmp_path: Path) -> None:
    """The scope door: a one-off script and a migration are read like any source."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "backfill.py").write_text(
        "def backfill(cur):\n"
        f'    cur.execute("UPDATE {TABLE} SET state = \'failed\' WHERE state = \'publishing\'")\n',
        encoding="utf-8",
    )
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "999_backfill.sql").write_text(
        f"UPDATE {TABLE} SET state = 'failed' WHERE state = 'publishing';\n",
        encoding="utf-8",
    )
    assert _writers_in(tmp_path) == [
        ("migrations/999_backfill.sql", "<sql>"),
        ("scripts/backfill.py", "backfill"),
    ]


def test_the_ratified_document_carries_the_census_this_guard_holds() -> None:
    """The number in the ratified document is DERIVED here, never retyped there.

    `docs/product-architecture/datastream-workbench-and-wizard.md` said "six
    `UPDATE` prives" and "Les six passent desormais par `advance_state`" while
    the census held seven statements in five functions. A count written in prose
    on one side of the repository and measured on the other drifts the first
    time a writer is added; this test makes the drift a failure.
    """
    document = REPO_ROOT / "docs" / "product-architecture" / "datastream-workbench-and-wizard.md"
    text = document.read_text(encoding="utf-8")
    marker = f"**{PRIVATE_STATEMENT_COUNT} statements, {PRIVATE_FUNCTION_COUNT} fonctions**"
    assert marker in text, (
        f"the ratified document must carry the census marker {marker!r}; "
        "if the census changed, change CONVERTED_WRITERS and the document together"
    )
    # And every converted writer is named there, so the list cannot shrink to a
    # number that happens to match.
    for _, function, _ in CONVERTED_WRITERS:
        assert function in text, function
