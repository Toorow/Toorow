"""The state of a review remark is spelled in ONE place, and every layer reads it.

THE DEFECT THIS FILE CLOSES, measured 2026-08-31. `core/context_review.py`
declared `STATUSES = ("open", "accepted", "declined")` and then never used it:
`status = 'open'` was typed by hand in THREE SQL statements of that same file --
the duplicate-key re-read, the `list_open` queue predicate and the `resolve_
request` UPDATE guard -- and the resolvable pair was retyped in a fourth place as
a validation tuple. A declaration nothing reads is not an owner; it is a comment
that happens to be a tuple.

WHY IT IS NOT COSMETIC. The same word lives in the database: migration 215 puts
it in the CHECK on `app.context_review_requests.status` AND in the partial unique
index `uq_context_review_request_open`. Add a fourth state there -- `superseded`,
`expired`, anything -- and `list_open` keeps serving a queue that silently means
something else, `resolve_request` keeps refusing a row it should reach, and
nothing in this repository turns red. That is the exact shape
`test_execution_state_registry.py` and `test_pull_job_state_registry.py` were
written for, one object each; this is the fourth object, and its spelling
(`status`, not `state`) is one no regex of those two guards looks for.

THE MODEL IS `core.candidate_fate` (story 54.2): the vocabulary is declared once,
imported by its readers, and a guard refuses a second site that retypes it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from core import context_review

ROOT = Path(__file__).resolve().parents[3]
SERVER = ROOT / "server"
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"

#: The module that owns the vocabulary. It is the ONLY Python file allowed to
#: write these words as literals.
OWNER = SERVER / "core" / "context_review.py"

#: The table whose `status` column this vocabulary describes.
TABLE = "app.context_review_requests"


def test_the_registry_is_closed_and_its_halves_agree() -> None:
    """Every resolution is a status, and `open` is never a verdict."""
    assert context_review.STATUSES, "the registry is empty"
    assert set(context_review.RESOLUTION_STATUSES) < set(context_review.STATUSES)
    assert context_review.STATUS_OPEN not in context_review.RESOLUTION_STATUSES
    for status in context_review.RESOLUTION_STATUSES:
        assert context_review.is_resolution(status), status
    assert not context_review.is_resolution(context_review.STATUS_OPEN)
    assert not context_review.is_resolution("a_status_no_build_knows")


def test_the_open_predicate_is_composed_from_the_constant() -> None:
    """The SQL fragment is derived, so renaming the state rewrites the queries."""
    assert context_review.open_status_predicate() == (
        f"status = '{context_review.STATUS_OPEN}'"
    )
    assert context_review.open_status_predicate("r.status").startswith("r.status = ")


def test_the_database_accepts_exactly_the_declared_statuses() -> None:
    """The CHECK of migration 215 and the Python registry are one vocabulary."""
    declared: set[str] | None = None
    for path in sorted(MIGRATIONS.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        if "context_review_requests" not in text:
            continue
        for match in re.finditer(
            r"status\s+TEXT[^,]*?CHECK\s*\(\s*status\s+IN\s*\((?P<body>[^)]*)\)",
            text,
            re.IGNORECASE | re.DOTALL,
        ):
            declared = set(re.findall(r"'([a-z_]+)'", match.group("body")))
    assert declared is not None, (
        "no CHECK on context_review_requests.status found in the migrations -- "
        "this guard would be measuring nothing"
    )
    assert declared == set(context_review.STATUSES), (
        "the migration CHECK and core.context_review disagree: "
        f"only in SQL={sorted(declared - set(context_review.STATUSES))}, "
        f"only in Python={sorted(set(context_review.STATUSES) - declared)}"
    )


#: What the sweep below covered on 2026-08-31: `server/core` held this many
#: Python modules, and exactly this many of them name the table. Both are
#: floors, not targets. A guard that scans a moved tree finds no offender and
#: reports the vocabulary clean -- criterion 13 of
#: `docs/product-architecture/module-boundaries.md`.
_CORE_MODULES_SCANNED = 530
_MODULES_NAMING_THE_TABLE = 2


def _product_files() -> list[Path]:
    return [
        path
        for path in sorted((SERVER / "core").rglob("*.py"))
        if "tests" not in path.parts
    ]


def test_no_module_retypes_the_status_of_a_review_request() -> None:
    """A hand-typed `status = 'open'` on this table is a second vocabulary.

    Read from the AST, so the word inside a docstring or a comment -- where
    naming the states is exactly right -- never trips it.
    """
    pattern = re.compile(
        r"status\s*(?:=|IN|<>|!=)\s*\(?\s*'(" + "|".join(context_review.STATUSES) + r")'",
        re.IGNORECASE,
    )
    scanned = _product_files()
    assert len(scanned) >= _CORE_MODULES_SCANNED, (
        f"this guard read {len(scanned)} modules of server/core, "
        f"{_CORE_MODULES_SCANNED} were there on 2026-08-31. The tree moved, so "
        "the verdict below is about nothing."
    )
    offenders: list[str] = []
    examined: list[str] = []
    for path in scanned:
        text = path.read_text(encoding="utf-8")
        if TABLE not in text and path != OWNER:
            continue
        examined.append(path.name)
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if pattern.search(node.value):
                offenders.append(f"server/core/{path.name}:{node.lineno}")
    assert len(examined) >= _MODULES_NAMING_THE_TABLE, (
        f"only {len(examined)} module(s) still name {TABLE} ({examined}); "
        f"{_MODULES_NAMING_THE_TABLE} did on 2026-08-31. Either the table was "
        "renamed -- in which case this guard watches nothing -- or its readers "
        "left, and the count belongs in this constant."
    )
    assert not offenders, (
        "these statements spell the state of a review remark themselves; it "
        "belongs to `core.context_review` -- import `open_status_predicate()` or "
        f"one of its constants: {offenders}"
    )


def test_the_owner_keeps_no_second_copy_of_the_resolvable_pair() -> None:
    """`("accepted", "declined")` retyped anywhere is the same defect, one file in."""
    source = OWNER.read_text(encoding="utf-8")
    assert "STATUSES" in source, (
        f"{OWNER.name} no longer declares STATUSES; the owner moved and this "
        "guard is reading a file that owns nothing."
    )
    tree = ast.parse(source)
    known = set(context_review.STATUSES)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            continue
        names = [
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        if len([name for name in names if name in known]) >= 2:
            offenders.append(f"server/core/context_review.py:{node.lineno}")
    # The one legitimate literal is `STATUSES` itself; it is built from the three
    # constants, so it holds Names and not Constants and never lands here.
    assert not offenders, (
        "a literal list of review statuses survives beside the declaration: "
        f"{offenders}. Build it from STATUS_OPEN / STATUS_ACCEPTED / "
        "STATUS_DECLINED instead."
    )
