"""Who still reads `app.target_fields`, and why -- one line each, or the gate fails.

WHY THIS GUARD EXISTS. `app.target_fields` is the legacy field dictionary. Its
five write doors answer 409 `legacy_store_is_read_only` since 2026-08-25
(`governance.md`, amendment of that day), so the store holds exactly what
migration 023 seeded and can never grow again. The rest of story 49.3 is moving
its READERS onto the Semantic Model, in the order the known-debt entry sets:
the readers, then the console, then the reads.

That order takes several sessions. Between them, the only thing that can quietly
undo the work is a NEW reader appearing -- a copy-pasted `FROM app.target_fields`
in a module that should have asked `core.governed_field_catalogue`. This file is
the ratchet: every surviving reader is declared here WITH ITS REASON, and the set
is compared against what the tree actually contains.

IT FAILS IN BOTH DIRECTIONS, and that is deliberate:

  * an UNDECLARED reader means someone reintroduced the dependency, or moved a
    reader without noticing it carries a second statement;
  * a DECLARED reader that no longer reads means the ledger below has gone stale
    -- a list of debts that outlive their subject is how a debt becomes
    permanent. Retargeting one is the same commit as deleting its line here.

WHAT IT DOES NOT CLAIM. It reads SOURCE, not behaviour: it cannot tell whether a
declared reader asks the successor first. That is what
`tests/core/test_governed_field_catalogue_pg.py` proves, against a real database,
by mutating the successor and watching the reader follow.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SERVER = _REPO_ROOT / "server"

#: A statement that READS OR WRITES the table -- not a mention in prose. A
#: comment naming `app.target_fields` to explain why a module no longer reads it
#: is exactly the kind of sentence this repository wants to keep.
_SQL_USE = re.compile(
    r"(?:FROM|JOIN|INTO|UPDATE)\s+app\.target_fields\b", re.IGNORECASE
)

#: The SURVIVING readers, each with the reason it survives. `app.target_fields
#: _versions` is a different table and is covered by the same rule through the
#: `\b` above, which stops the match before the suffix.
DECLARED: dict[str, str] = {
    "core/governed_field_catalogue.py": (
        "THE FALLBACK, and the reason this list can shrink at all. It reads the "
        "dictionary SECOND, after the Semantic Model, so a name the successor does "
        "not govern still resolves. Retargeting the readers onto it is what moves "
        "them; this is the ONE module allowed to hold the layer below."
    ),
    "core/datamodel.py": (
        "THE STORE ITSELF. Its `list_target_fields` / `get_target_field` are what "
        "`GET /api/datamodel/fields*` serves, and those reads are step THREE of the "
        "order -- the console readers come before them. Retargeting this module "
        "would BE that step, not prepare it."
    ),
    "core/platform_canonical_vocabulary.py": (
        "A PROJECTION SOURCE, not a consumer. `load_dictionary` reads the 15 "
        "dictionary rows in order to MINT `app.mdm_canonical_fields` from them "
        "(`governance.md`, table `Registry column | Comes from`). Pointing it at "
        "its own output would make the projection derive from itself."
    ),
    "core/platform_semantic_concepts.py": (
        "THE OTHER PROJECTION SOURCE. `load_dictionary_types` reads "
        "`{name -> data_type}` to TYPE the platform Concepts it mints. Same reason: "
        "an instrument must not measure its own copy."
    ),
    "core/business_taxonomy.py": (
        "The `target_field` BUSINESS LINK TARGET TYPE, keyed by name -- and its "
        "version lookup in `_path_version`. The successors already have their own "
        "target types in the same enum (`canonical_field` by mdm id, "
        "`semantic_concept` by concept id), and this module's own comment forbids "
        "merging them: 'the two field types are two vocabularies with two "
        "resolution keys'. What reads a stored link BACK is Context Hub's console "
        "(`KnowledgeGraphPage`), which is step TWO. Answering a Concept name here "
        "before that step would mint links nothing can resolve."
    ),
    "core/governance_read_model.py": (
        "THE READ SIDE OF THE SAME BUSINESS LINK, and it arrived on 2026-09-01 "
        "(`ce647c59`, story 49.2) when the used-by panel stopped counting into "
        "the void and started RENDERING its consumers. It resolves the label of "
        "an `mdm_business_links` row whose `target_type = 'target_field'`, and "
        "that target type is addressed BY NAME -- migration 130's own validator "
        "reads `app.target_fields` on `name`, which is why the join is a join and "
        "not a call. `core/business_taxonomy.py` above is declared for exactly "
        "this reason on the WRITE side, and its line says why the two field types "
        "may not be merged; a reader of the link it stores cannot move before it. "
        "Retargeting the pair is one commit, and it deletes both lines."
    ),
    "core/context_store.py": (
        "`_node_exists_in_scope` for the `target_field` GRAPH NODE TYPE -- the "
        "same object as the business link above, under the graph's own enum, and "
        "blocked on the same console step. Its OTHER reader moved: "
        "`assert_mdm_tags_resolve` goes through `core.governed_field_catalogue`."
    ),
}


def _prose_lines(source: str) -> set[int]:
    """The lines that are COMMENT or DOCSTRING -- prose, never a statement.

    A module that stopped reading the dictionary explains why in a comment, and
    the sentence quotes the query it removed. A guard that counted those would
    make the honest explanation indistinguishable from the defect, and the only
    way to pass it would be to delete the explanation. Measured while writing
    this file: `report_chain` and `datastream_mapping_api` both tripped it that
    way, and both had already moved.
    """
    prose: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
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


#: Product modules under `server/` on 2026-08-31, measured by `_scanned_sources`.
#: A FLOOR, not an equality: adding a module must not redden this file, but a
#: renamed tree that empties the scan must. Criterion 13 of
#: `docs/product-architecture/module-boundaries.md` -- without it every
#: assertion below is `not {}` over a scan that read nothing.
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


def _modules_that_read_the_dictionary() -> dict[str, list[int]]:
    """`{path relative to server/ -> line numbers}`. Derived, never listed."""
    found: dict[str, list[int]] = {}
    for path in _scanned_sources():
        source = path.read_text(encoding="utf-8")
        if "app.target_fields" not in source:
            continue
        prose = _prose_lines(source)
        hits = [
            n
            for n, line in enumerate(source.splitlines(), 1)
            if n not in prose and _SQL_USE.search(line)
        ]
        if hits:
            found[path.relative_to(_SERVER).as_posix()] = hits
    return found


def test_no_module_reads_the_legacy_dictionary_without_declaring_why() -> None:
    actual = _modules_that_read_the_dictionary()
    undeclared = {
        module: lines for module, lines in actual.items() if module not in DECLARED
    }
    assert not undeclared, (
        "These modules read `app.target_fields` and are not declared in this "
        "file. Ask `core.governed_field_catalogue` instead -- it puts the Semantic "
        "Model first and keeps the dictionary as the layer below -- or add a line "
        "here saying why this one cannot: "
        + "; ".join(f"{module}:{lines}" for module, lines in sorted(undeclared.items()))
    )


def test_a_declared_reader_that_stopped_reading_is_removed_from_the_ledger() -> None:
    actual = _modules_that_read_the_dictionary()
    stale = sorted(module for module in DECLARED if module not in actual)
    assert not stale, (
        "These modules no longer read `app.target_fields`, so their line in "
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
        "move yet, not a placeholder: " + ", ".join(thin)
    )


def test_the_retargeted_readers_did_not_come_back() -> None:
    """The four this step moved. Naming them keeps the win from silently undoing.

    `context_store.py` is deliberately absent: it still reads the dictionary for
    its OTHER site, so a module-level assertion there would be false. The
    behaviour of the one that moved is held by
    `tests/core/test_mdm_tags_resolve.py`.
    """
    moved = (
        "core/report_chain.py",
        "core/mdm_references.py",
        "core/datastream_mapping_api.py",
    )
    actual = _modules_that_read_the_dictionary()
    regressed = [module for module in moved if module in actual]
    assert not regressed, (
        "These readers were moved onto the Semantic Model and read the legacy "
        "dictionary again: " + ", ".join(regressed)
    )
