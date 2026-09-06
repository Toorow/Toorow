"""Who still reads the SUPERSEDED business taxonomy store, and why -- or the gate fails.

WHY THIS GUARD EXISTS. `app.mdm_business_domains` and
`app.mdm_business_classifications` stopped being an authority on 2026-08-25: the
four identity writers of `core.business_taxonomy` answer 409
`legacy_store_is_read_only`, the authority is `app.master_data_nodes` /
`app.master_data_object_versions`, and the creation command writes a row into the
old store BORN SUPERSEDED so the readers keep working. `governance.md` §
*Decision 2* names that arrangement and names its remainder in one sentence: the
readers are *"re-pointed reader by reader, never by a big-bang"*.

That order takes several sessions, and between them the only thing that can
quietly undo the work is a NEW reader appearing -- a copy-pasted
`JOIN app.mdm_business_domains` in a module that should have asked
`core.business_identity_catalogue`. This file is the ratchet: every surviving
reader is declared here WITH ITS REASON, and the set is compared against what the
tree actually contains.

IT FAILS IN BOTH DIRECTIONS, and that is deliberate:

  * an UNDECLARED reader means someone reintroduced the dependency, or moved a
    reader without noticing it carried a second statement;
  * a DECLARED reader that no longer reads means the ledger below has gone stale
    -- a list of debts that outlive their subject is how a debt becomes
    permanent. Retargeting one is the same commit as deleting its line here.

AND IT DOES NOT MERELY COUNT. A count passes when one reader is swapped for
another, which is exactly the drift it exists to catch, so the comparison is on
the SET of module paths and the moved readers are named one by one in
:func:`test_the_retargeted_readers_did_not_come_back`.

WHAT IT DOES NOT CLAIM. It reads SOURCE, not behaviour: it cannot tell whether a
declared reader asks the authority first. That is what
`tests/integration/test_business_identity_catalogue_pg.py` proves, against a real
database, by mutating the authority and watching the reader follow.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SERVER = _REPO_ROOT / "server"

#: A statement that READS OR WRITES one of the four superseded tables -- not a
#: mention in prose. A comment naming `app.mdm_business_domains` to explain why a
#: module no longer reads it is exactly the kind of sentence this repository wants
#: to keep. `app.mdm_business_links` is deliberately NOT matched: a link is a
#: Context Hub relation, not an identity, and `governance.md` says in its own
#: words that the convergence does not move it.
#:
#: `\s+` SPANS NEWLINES, and it has to be searched against text that still has
#: them. Review round 1: this pattern was applied line by line, so the perfectly
#: ordinary formatting
#:
#:     FROM
#:         app.mdm_business_domains d
#:
#: was invisible to the whole ratchet -- and four modules under `server/core`
#: already end a line with `FROM`. A guard that a line break defeats is not a
#: guard, so :func:`_statement_lines` searches the JOINED source instead.
_SQL_USE = re.compile(
    r"(?:FROM|JOIN|INTO|UPDATE)\s+app\.mdm_business_"
    r"(?:domains|classifications|domain_versions|classification_versions)\b",
    re.IGNORECASE,
)

#: The SURVIVING readers, each with the reason it survives.
DECLARED: dict[str, str] = {
    "core/business_identity_catalogue.py": (
        "THE FALLBACK, and the reason this list can shrink at all. It reads the "
        "superseded row SECOND, after `app.master_data_nodes`, and only for an id "
        "the authority holds no node for -- the organization that has not "
        "converged. Re-pointing the readers onto it is what moves them; this is "
        "the ONE module allowed to hold the layer below."
    ),
    "core/master_data.py": (
        "THE VERSION WRITER, and it reads the superseded ledger in order to STAY "
        "OUT OF ITS WAY. `_NEXT_NODE_VERSION_NUMBER` mints the next node version "
        "above EVERY ledger that numbers the identity, superseded one included "
        "(AI-324, decided by Jean 2026-08-31: a version number designates one "
        "content, ever). Asking `business_identity_catalogue` here would invert "
        "the layering -- the catalogue reads the authority this module IS -- and "
        "would put a read that decides a WRITE behind a resolver built for "
        "screens."
    ),
    "core/master_data_convergence.py": (
        "THE WRITER, which is not a reader to move. `plan_convergence` reads what "
        "is left to converge, `_converge_row` stamps `superseded_by_node_id`, and "
        "`write_superseded_projection` inserts the born-superseded row every "
        "creation in the authority projects. `governance.md` § Decision 2 is that "
        "projection, ratified; stopping it is not this story's to reverse."
    ),
}

#: The readers story 49.2 moved onto `core.business_identity_catalogue`, named so
#: the win cannot silently undo itself. `feedback_review` and `governance_read
#: _model` carried more than one statement each, which is why the ledger is a set
#: of MODULES and this list is the per-module assertion beside it.
RETARGETED = (
    "core/governance_read_model.py",
    "core/business_taxonomy.py",
    "core/context_seed.py",
    "core/golden_questions.py",
    "core/project_settings.py",
    "core/datastream_workbench.py",
    "core/semantic_model.py",
    "core/analyze_render_mcp.py",
    "core/analyze_workbench.py",
    "core/feedback_review.py",
    "core/multi_source_plan.py",
)


def _prose_lines(source: str) -> set[int]:
    """The lines that are COMMENT or DOCSTRING -- prose, never a statement.

    A module that stopped reading the superseded store explains why in a comment,
    and the sentence quotes the query it removed. A guard that counted those would
    make the honest explanation indistinguishable from the defect, and the only
    way to pass it would be to delete the explanation.
    """
    prose: set[int] = set()
    lines = source.splitlines()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            # Only a comment that OWNS its line is prose. A trailing lint
            # annotation (the S608 one is this repository's habit on an
            # interpolated-SQL line) sits AFTER a statement, and blanking the
            # whole line would hide that statement (review of 49-2, round 2).
            if lines[token.start[0] - 1][: token.start[1]].strip() == "":
                prose.add(token.start[0])
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                prose.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return prose


def _statement_lines(source: str) -> list[int]:
    """The lines where a STATEMENT touches one of the four tables.

    Prose lines are BLANKED rather than skipped -- their newline stays, so an
    offset still maps to the line it came from -- and the regex is then run over
    the whole text at once. That is what lets a match span a line break, which
    the line-by-line version could not do (see :data:`_SQL_USE`).
    """
    prose = _prose_lines(source)
    # A trailing comment is cut at its own start; the statement before it stays.
    trailing = {
        token.start[0]: token.start[1]
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and token.start[0] not in prose
    }
    scrubbed = "\n".join(
        "" if number in prose else line[: trailing.get(number, len(line))]
        for number, line in enumerate(source.splitlines(), 1)
    )
    return sorted(
        {
            scrubbed.count("\n", 0, match.start()) + 1
            for match in _SQL_USE.finditer(scrubbed)
        }
    )


#: Product modules under `server/` on 2026-08-31, measured by `_scanned_sources`.
#: A FLOOR, not an equality: a new module must not redden this file, an emptied
#: scan must. Criterion 13 of `docs/product-architecture/module-boundaries.md`:
#: `assert not undeclared` is true of a tree nobody read.
_SCANNED_SOURCES_AT_2026_08_31 = 679


def _scanned_sources() -> list[Path]:
    """Every product module this guard opens. Its LENGTH is the guard's coverage."""
    return [
        path
        for path in sorted(_SERVER.rglob("*.py"))
        if "tests" not in path.relative_to(_SERVER).parts
    ]


def test_the_scan_still_reaches_the_product() -> None:
    """A guard that read nothing must be red HERE, not serenely green below."""
    scanned = _scanned_sources()
    assert len(scanned) >= _SCANNED_SOURCES_AT_2026_08_31, (
        f"this guard opened {len(scanned)} modules under {_SERVER}; "
        f"{_SCANNED_SOURCES_AT_2026_08_31} were there on 2026-08-31. The tree "
        "moved or the glob broke, so every verdict below is about nothing."
    )


def _modules_that_read_the_superseded_store() -> dict[str, list[int]]:
    """`{path relative to server/ -> line numbers}`. Derived, never listed."""
    found: dict[str, list[int]] = {}
    for path in _scanned_sources():
        source = path.read_text(encoding="utf-8")
        if "app.mdm_business_" not in source:
            continue
        hits = _statement_lines(source)
        if hits:
            found[path.relative_to(_SERVER).as_posix()] = hits
    return found


def test_no_module_reads_the_superseded_store_without_declaring_why() -> None:
    actual = _modules_that_read_the_superseded_store()
    undeclared = {
        module: lines for module, lines in actual.items() if module not in DECLARED
    }
    assert not undeclared, (
        "These modules read the superseded business taxonomy store and are not "
        "declared in this file. Ask `core.business_identity_catalogue` instead -- "
        "it answers from `app.master_data_nodes` first and keeps the superseded "
        "row as the layer below -- or add a line here saying why this one cannot: "
        + "; ".join(f"{module}:{lines}" for module, lines in sorted(undeclared.items()))
    )


def test_a_declared_reader_that_stopped_reading_is_removed_from_the_ledger() -> None:
    actual = _modules_that_read_the_superseded_store()
    stale = sorted(module for module in DECLARED if module not in actual)
    assert not stale, (
        "These modules no longer read the superseded store, so their line in "
        "DECLARED is a debt that outlived its subject. Delete it in the commit "
        "that retargets them: " + ", ".join(stale)
    )


def test_every_declared_reader_says_why_it_survives() -> None:
    """A ledger entry with no reason is a list, and a list is not a decision."""
    thin = sorted(
        module for module, reason in DECLARED.items() if len(reason.split()) < 15
    )
    assert not thin, (
        "A declared reader must carry the sentence that explains why it cannot "
        "move, not a placeholder: " + ", ".join(thin)
    )


def test_the_retargeted_readers_did_not_come_back() -> None:
    """Named one by one, so swapping one reader for another cannot pass.

    The set comparison above answers "is the ledger complete". It does NOT answer
    "did THIS module stay moved": a commit that retargets one module and
    reintroduces the dependency in another leaves the ledger's size unchanged.
    """
    actual = _modules_that_read_the_superseded_store()
    regressed = [module for module in RETARGETED if module in actual]
    assert not regressed, (
        "These readers were re-pointed at `core.business_identity_catalogue` by "
        "story 49.2 and read the superseded store again: " + ", ".join(regressed)
    )


def test_the_resolver_is_the_only_module_the_readers_import_for_this() -> None:
    """Every retargeted module names the catalogue. One resolver, not fourteen.

    Without this, a module could satisfy the ratchet by deleting its read
    altogether -- which passes a guard about `FROM` clauses and loses the answer
    a person was reading.
    """
    missing = [
        module
        for module in RETARGETED
        if "business_identity_catalogue" not in (_SERVER / module).read_text(encoding="utf-8")
    ]
    assert not missing, (
        "These modules were re-pointed but no longer name the resolver, so they "
        "stopped answering rather than started answering from the authority: "
        + ", ".join(missing)
    )


def test_a_read_split_across_two_lines_is_not_invisible() -> None:
    """The control that failed in review round 1, as its own assertion.

    Both spellings below are the same statement, and both are what a formatter
    produces on a long line. Measured before the repair: the single-line form was
    found and the split form was NOT, so any reader could have slipped back in
    under a line break -- silently, because the ratchet would have gone on
    reporting green.
    """
    single = 'SQL = """SELECT id FROM app.mdm_business_domains d WHERE d.org_id = 1"""\n'
    split = (
        'SQL = """\n'
        "    SELECT id\n"
        "      FROM\n"
        "          app.mdm_business_domains d\n"
        "     WHERE d.org_id = 1\n"
        '"""\n'
    )

    assert _statement_lines(single) == [1]
    assert _statement_lines(split), (
        "a read split across a line break is invisible to the ratchet -- "
        "the regex is being applied line by line again"
    )
    assert _statement_lines(split) == [3]


def test_a_trailing_comment_does_not_hide_the_statement_before_it() -> None:
    """A trailing S608 lint annotation after an interpolated-SQL line is native here."""
    annotated = 'SQL = "SELECT id FROM app.mdm_business_domains d"  # noqa: S608\n'
    assert _statement_lines(annotated) == [1]
    still_prose = "X = 1  # it used to read FROM app.mdm_business_domains d\n"
    assert _statement_lines(still_prose) == []


def test_prose_that_names_the_store_is_still_not_a_statement() -> None:
    """The other half. Blanking prose must not turn into counting it.

    A module that stopped reading explains why, and the sentence quotes the
    query it removed. Both shapes are here because the repair above rewrote how
    prose is removed, and a rewrite that let a docstring through would make the
    honest explanation indistinguishable from the defect.
    """
    commented = "# it used to read FROM app.mdm_business_domains d\nX = 1\n"
    documented = (
        'def f():\n'
        '    """It used to read\n'
        "    FROM\n"
        "        app.mdm_business_domains d\n"
        '    """\n'
        "    return 1\n"
    )

    assert _statement_lines(commented) == []
    assert _statement_lines(documented) == []
