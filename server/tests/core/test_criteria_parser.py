"""The parser that decides what `Incomplete if` index 13 names, under test.

220 lines of `scripts/finished_work_audit.py` read every ratified document of the
repository and hand a numbered list to three instruments — the audit itself,
`scripts/check_ledger_anchors.py`, and `scripts/screens.py work <id>`, which is
the entry point of five agents and three skills. On 2026-08-21 that parser had
NO test at all: `grep -rn "criteria_split\\|_inline_clause\\|_accept_clause" server
scripts` returned only the scripts themselves.

An unmeasured parser is worse here than elsewhere, because `completeness-ledger.json`
keys a verdict by the ZERO-BASED INDEX of a bullet in the list this parser
returns. A change that shifts one index does not crash, does not print anything,
and silently makes every verdict below it attest a criterion nobody judged.

So this file pins the five things the parser promises:

  0. THE WALK ITSELF, against a corpus written by hand — the reference that does
     NOT come from the code under test. Read the next paragraph; it is the
     reason this section exists at all.
  1. THE BULLET PREFIX IS UNTOUCHED. No inline form may enter `listed`.
  2. THE THREE INLINE FORMS ARE READ, each in the shape a ratified page uses.
  3. PROSE ABOUT THE CLAUSES IS NOT A CLAUSE — the eight references that are
     rejected today stay rejected.
  4. THE INVARIANT THE DOCSTRING NOW STATES, including the half that does NOT
     hold: `appended` is document-ordered and is *not* append-only, which is
     harmless only as long as no verdict is recorded on an inline clause.

Plus `--unanchored-drift`, exercised against a throwaway git repository so the
classification "re-pointed" / "rewritten in place" / "stable" is proven rather
than believed.

THE FIRST VERSION OF THIS FILE COMPARED THE PARSER TO ITSELF, AND THAT IS WHY
SECTION 0 EXISTS. `test_the_bullet_prefix_of_every_ratified_document_is_byte_identical`
ran `criteria_split` against `criteria_split` with the five inline patterns
switched off. Both halves share the bullet walk — the walk that produces the 484
indexes `completeness-ledger.json` keys its verdicts on — so no mutation inside
it could ever make this file red. Replayed 2026-08-21, one mutation at a time,
file restored to the byte between runs, SIX of them left all 32 tests green:

    the `replace` branch losing its `target.pop()`   file-source-ingestion 23 -> 24 bullets
    the heading regex reading `#{2}` and not `#{2,3}`               484 -> 433 bullets
    a bullet's continuation line dropped              21 surfaces change TEXT, every anchor breaks
    the blockquote fence never stripped                   datastream 19 -> 18 clauses
    `Replaces the last ... bullet` no longer read      file-source-ingestion, TEXT at one index
    a blank line no longer closing its bullet                    glossary, TEXT at one index

Section 0 measures the walk against `criteria_corpus/`: six documents written by
hand, and `expected.json`, whose two lists were written by READING them. Every
one of the six mutations above turns that section red. The command that replays
them is in `criteria_corpus/expected.json`.

Everything here is OFFLINE and touches no database.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

#: IMPORTED, NOT `runpy.run_path`. The sibling audit tests load the script into a
#: throwaway namespace, which is enough to CALL a function but not to REPLACE one
#: of its globals: `run_path` hands back a copy, while the functions keep the
#: namespace they were compiled in. Two of the tests below switch a pattern off
#: and one asserts the three scripts share ONE function object, so the module has
#: to be the real one.
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

AUDIT = importlib.import_module("finished_work_audit")
ANCHORS = importlib.import_module("check_ledger_anchors")
SCREENS = importlib.import_module("screens")

#: The five patterns that make a line an inline criterion. Replacing all five by
#: a never-matching regex turns the parser back into the bullet-only walk it was
#: before 2026-08-21, which is how test 1 compares the two without owning a
#: second copy of the walk.
_INLINE_PATTERN_NAMES = (
    "_INLINE_LIST_OPENER",
    "_ADD_TO_LIST_INLINE",
    "_INLINE_CLAUSE",
    "_INDENTED_CLAUSE",
    "_MIDLINE_CLAUSE",
)
_NEVER = re.compile(r"(?!)")


def _bullet_only_split(doc: Path) -> tuple[list[str], list[str]]:
    """`criteria_split` with every inline form disabled."""
    saved = {name: getattr(AUDIT, name) for name in _INLINE_PATTERN_NAMES}
    for name in _INLINE_PATTERN_NAMES:
        setattr(AUDIT, name, _NEVER)
    try:
        return AUDIT.criteria_split(doc)
    finally:
        for name, pattern in saved.items():
            setattr(AUDIT, name, pattern)


# ---------------------------------------------------------------------------
# 0. THE WALK, AGAINST A REFERENCE THAT IS NOT THE WALK.
# ---------------------------------------------------------------------------

CORPUS = Path(__file__).resolve().parent / "criteria_corpus"

#: The hand-written answer for each document of the corpus. It is DATA, and it
#: has no regeneration command on purpose: a `--write-expected` flag would turn
#: this file back into a copy of the parser, which is the defect being repaired.
_EXPECTED = json.loads((CORPUS / "expected.json").read_text(encoding="utf-8"))


def _corpus_documents() -> list[str]:
    return [name for name in _EXPECTED if not name.startswith("_")]


def test_every_corpus_document_has_a_hand_written_answer():
    """A `.md` added to the corpus and forgotten in `expected.json` proves nothing."""
    on_disk = {path.name for path in CORPUS.glob("*.md")}
    declared = set(_corpus_documents())
    assert on_disk == declared, (
        "the corpus and its hand-written answer disagree about which documents "
        f"exist: only on disk {sorted(on_disk - declared)}, only declared "
        f"{sorted(declared - on_disk)}"
    )


@pytest.mark.parametrize("name", _corpus_documents())
def test_the_walk_returns_what_a_reader_reads(name):
    """`criteria_split` against a list nobody derived from `criteria_split`.

    THIS IS THE ONLY TEST IN THIS FILE THAT CAN SEE A CHANGE TO THE WALK. Every
    other one either builds its fixture around a single feature, or compares the
    parser to a variant of itself. The six mutations named in the module
    docstring survived all of them; each turns this one red, and the corpus says
    which shape it broke.
    """
    listed, appended = AUDIT.criteria_split(CORPUS / name)
    expected = _EXPECTED[name]
    assert listed == expected["listed"], (
        f"{name}: the established list is not what the document says. "
        f"An index moved here is an index moved in `completeness-ledger.json`.\n"
        f"reads: {expected['reads']}"
    )
    assert appended == expected["appended"], (
        f"{name}: the appended clauses are not what the document says.\n"
        f"reads: {expected['reads']}"
    )
    # And the order the ledger depends on, on the same documents.
    assert AUDIT.incomplete_if(CORPUS / name) == expected["listed"] + expected["appended"]


# ---------------------------------------------------------------------------
# 1. THE BULLET PREFIX IS UNTOUCHED — the whole reason the ledger still works.
# ---------------------------------------------------------------------------


def test_no_inline_form_enters_the_bullet_list(tmp_path):
    """A page that writes a clause BEFORE its own list must not gain an index 0.

    This is `governance.md:92` in miniature: a criterion stated in prose above
    the `## Incomplete if` heading. Read at its document position it would insert
    a bullet at index 0 and re-point every recorded verdict of that surface.
    """
    doc = tmp_path / "page.md"
    doc.write_text(
        "# A ratified page\n"
        "\n"
        "*Incomplete if*: a clause stated at column zero, ahead of the list itself.\n"
        "\n"
        "## Incomplete if\n"
        "\n"
        "- first bullet of the list\n"
        "- second bullet of the list\n"
        "\n"
        "## Amendment, one\n"
        "\n"
        "Add to **Incomplete if**: a clause carried on the opener's own line.\n"
        "\n"
        "**Incomplete if** (adds to the list above):\n"
        "\n"
        "- third bullet, under an inline opener\n"
        "\n"
        "## Amendment, two\n"
        "\n"
        "    *Incomplete if*: a clause indented inside a numbered item of this page.\n"
        "\n"
        "A sentence that ends here. *Incomplete if*: a clause stated mid-paragraph.\n"
        "\n"
        "> - a quoted bullet that belongs to no list\n",
        encoding="utf-8",
    )

    listed, appended = AUDIT.criteria_split(doc)

    assert listed == [
        "first bullet of the list",
        "second bullet of the list",
    ], "an inline form reached the bullet list; every recorded index below it just moved"
    assert appended == [
        "a clause stated at column zero, ahead of the list itself",
        "a clause carried on the opener's own line",
        "third bullet, under an inline opener",
        "a clause indented inside a numbered item of this page",
        "a clause stated mid-paragraph",
    ]
    # And the concatenation order is the contract the ledger depends on.
    assert AUDIT.incomplete_if(doc) == listed + appended


def test_the_bullet_prefix_of_every_ratified_document_is_byte_identical():
    """Disable the inline reader; every `listed` must come back unchanged.

    WHAT THIS PROVES, AND WHAT IT CANNOT. It proves SEPARABILITY across the whole
    ratified set: with the five inline patterns off, nothing the inline reader
    contributes is left in `listed`, and nothing the bullet walk contributes has
    leaked into `appended`. That is worth having and no other test says it.

    It cannot see a change to the WALK, and the first version of this file
    treated it as if it could. Both sides of the comparison call
    `criteria_split`, so a mutation inside the shared walk moves the two answers
    together and this test stays green — six of them did, on 2026-08-21. The walk
    is measured in section 0, against `criteria_corpus/`.
    """
    changed = []
    leaked = []
    for surface in AUDIT.SURFACES:
        listed, _ = AUDIT.criteria_split(surface.doc)
        bullet_only_listed, bullet_only_appended = _bullet_only_split(surface.doc)
        if listed != bullet_only_listed:
            changed.append(surface.key)
        if bullet_only_appended:
            leaked.append(surface.key)

    assert changed == [], (
        "the inline reader now contributes to the bullet list on "
        f"{changed} -- every ledger index of those surfaces has moved"
    )
    assert leaked == [], (
        f"a bullet reached the appended list on {leaked} with every inline "
        "pattern disabled; the two lists are no longer separable"
    )


# ---------------------------------------------------------------------------
# 2. THE THREE INLINE FORMS — each in the shape a ratified page actually uses.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        # `analyze-and-test.md` — a list opener that is neither a heading nor
        # `Add to **Incomplete if**:`. Four of them, and every bullet under the
        # four was invisible.
        ("**Incomplete if** (adds to the list above):", True),
        ("*Incomplete if* (this amendment only):", True),
        ("`Incomplete if` (adds to the list above) :", True),
        # Not an opener: it carries no parenthesis, so `_ADD_TO_LIST` owns it.
        ("Add to **Incomplete if**:", False),
    ],
)
def test_the_inline_list_opener_is_recognised(line, expected):
    assert bool(AUDIT._INLINE_LIST_OPENER.match(line)) is expected


def test_add_to_the_list_carries_its_criterion_on_the_same_line():
    """`Add to **Incomplete if**: <clause>` — the anchor used to require EOL."""
    line = (
        "Add to **Incomplete if**: a control of the Query Spec is accepted and "
        "not applied to the reading it governs."
    )
    match = AUDIT._ADD_TO_LIST_INLINE.match(line)
    assert match is not None
    assert AUDIT._accept_clause(match.group(1)) == (
        "a control of the Query Spec is accepted and not applied to the reading "
        "it governs."
    )
    # The end-of-line form stays the business of `_ADD_TO_LIST`, which opens a
    # list into `listed` -- reading it here would move it to `appended`.
    assert AUDIT._ADD_TO_LIST_INLINE.match("Add to **Incomplete if**:") is None


@pytest.mark.parametrize(
    "line, previous_blank, expected",
    [
        # At column 0, in the paragraph it closes (`governance.md:1274`).
        (
            "*Incomplete if*: a declared object kind is absent from the Registries lens.",
            False,
            "a declared object kind is absent from the Registries lens.",
        ),
        # Indented inside a numbered item (`proactive-assertions.md:310`) -- read
        # ONLY after a blank line, because an indented marker is otherwise the
        # continuation of the sentence above it.
        (
            "    *Incomplete if*: an assertion is emitted with no function attached.",
            True,
            "an assertion is emitted with no function attached.",
        ),
        (
            "    *Incomplete if*: an assertion is emitted with no function attached.",
            False,
            None,
        ),
        # Mid-paragraph, after a full stop (`governance.md:1289`).
        (
            "The rule holds at every grain. *Incomplete if*: a rule is applied "
            "without its scope being named.",
            False,
            "a rule is applied without its scope being named.",
        ),
        # Inside a table cell: the pipe ends the clause, so the next column does
        # not become part of the criterion (`datastream-...:1306`).
        (
            "| Publication | It must hold. *Incomplete if*: a version is bound "
            "without evidence | ratified |",
            False,
            "a version is bound without evidence",
        ),
    ],
)
def test_the_three_clause_positions_are_read(line, previous_blank, expected):
    assert AUDIT._inline_clause(line, previous_blank) == expected


# ---------------------------------------------------------------------------
# 3. PROSE ABOUT THE CLAUSES IS NOT A CLAUSE.
# ---------------------------------------------------------------------------


#: The eight tails a ratified document writes after an `Incomplete if` marker
#: that are NOT criteria — measured 2026-08-21 by walking the 24 documents with
#: `_INLINE_CLAUSE`, `_INDENTED_CLAUSE` and `_MIDLINE_CLAUSE` and collecting
#: every match `_accept_clause` refused. Seven open a line; the eighth
#: (`first-figure-path`) sits mid-line. If one of them ever becomes a criterion,
#: the surface it belongs to gains an index and every verdict under it moves.
_PROSE_REFERENCES_THAT_ARE_NOT_CRITERIA = (
    # visualization-and-rendering.md, Chart Template ratification (db993d70):
    # the sentence EXPLAINS why the member-id/column rule is deliberately NOT a
    # new bullet -- criterion [20] already asks for that proof, and inserting a
    # bullet would re-point every appended criterion of the surface.
    "bullet on purpose",
    # analyze-and-test.md, amendment of 2026-08-31 (rule D perimeter derived):
    # a HISTORY sentence about what the old Incomplete if used to ask -- it
    # narrates the retired reading, it does not state a failure condition.
    "below asked whoever added a narrative-producing function to",
    # datastream-workbench-and-wizard.md
    "criteria now carry a dated verdict with its command or file in evidence",
    # analyze-and-test.md
    'list at line 509 forbids in its own words ("Analyze creates nothing")',
    # governance.md
    "clause below, live.",
    # organization-settings.md -- a marker closing a sentence, nothing more
    ".",
    # first-figure-path.md, mid-line
    "bullet 1 — *a step of",
    # execution-substrate.md, three of them
    "which is precisely how a development-time fallback reached production",
    "clause 2 of this document wearing different clothes — a run that",
    "added as the last clause of the list below. (It is left where it is.)",
    # capabilities/currency-fx.md -- l'arbitrage WORDING A du 2026-08-22 CITE la
    # puce 7 pour dire ce qu'elle tranche. Une citation d'une puce n'est pas une
    # puce : la compter ajouterait un critere a une surface ratifiee et
    # decalerait tout verdict sous elle -- exactement le defaut que 67-22 vient
    # de reparer cinq fois dans le ledger.
    'bullet 7 (`:46`) reads *"a posed rate resolves outside the',
    # context-hub.md (49-6, 2026-08-28) -- a sentence ABOUT the earlier clause:
    # the amendment says what is forbidden (a second writer) and keeps the
    # original submission below for the record. A reference, not a criterion.
    "clause demanding their removal is amended by this decision:",
    # mcp-tool-surface.md (67-1) -- the marker HEADS a numbered list (10., 11.):
    # the criteria are the numbered lines, the header's tail names the scope.
    "for this amendment:",
)


@pytest.mark.parametrize("tail", _PROSE_REFERENCES_THAT_ARE_NOT_CRITERIA)
def test_prose_about_the_list_is_not_counted_as_a_criterion(tail):
    assert AUDIT._accept_clause(tail) is None, (
        "this sentence makes `Incomplete if` its OBJECT; counting it adds a "
        "criterion to a ratified surface and moves every verdict under it"
    )


def test_a_real_clause_is_still_accepted():
    """The rejection above must not be a blanket refusal."""
    assert AUDIT._accept_clause(
        "a declared object kind is absent from the Registries lens"
    ) is not None


def test_a_clause_is_a_sentence_not_a_fragment():
    """`_CLAUSE_MIN_CHARS` is load-bearing, and nothing else covers it.

    Every one of the eight prose references above is refused by the reference
    regex or by the leading-character rule, so lowering the length threshold
    breaks none of them — and yet lowering it is exactly what turns a trailing
    `` `Incomplete if`. `` into a criterion, adding an index to a ratified
    surface. So the boundary is asserted on both sides, right at 20 characters.
    """
    assert AUDIT._CLAUSE_MIN_CHARS == 20
    nineteen = "a reading is stale"  # 18 chars: below the boundary
    twenty = "a reading is stale here and now"  # comfortably above it
    assert len(nineteen) < AUDIT._CLAUSE_MIN_CHARS <= len(twenty)
    assert AUDIT._accept_clause(nineteen) is None
    assert AUDIT._accept_clause(twenty) == twenty


def test_a_marker_that_only_closes_a_sentence_is_not_a_criterion():
    """`organization-settings.md:16` — `` … no `Incomplete if`. `` and nothing more."""
    assert AUDIT._accept_clause(".") is None
    assert AUDIT._accept_clause("") is None


def test_the_number_of_rejected_references_is_derived_never_retyped():
    """Walk the 24 documents and count what `_accept_clause` refuses.

    The count is not asserted against a frozen number — a retyped number is the
    defect this very file was written after (`finished_work_audit.py` claimed 54
    while its own command printed 55). What IS asserted is that every rejection
    is one of the eight named above, so a NEW rejection is a reading someone
    owes rather than a silent loss.
    """
    named = {tail.strip() for tail in _PROSE_REFERENCES_THAT_ARE_NOT_CRITERIA}
    unnamed: list[str] = []
    for surface in AUDIT.SURFACES:
        if not surface.doc.exists():
            continue
        previous_blank = True
        for line in surface.doc.read_text(encoding="utf-8").splitlines():
            unquoted = AUDIT._BLOCKQUOTE.sub("", line)
            was_blank, previous_blank = previous_blank, not unquoted.strip()
            for pattern, gated in (
                (AUDIT._INLINE_CLAUSE.match(unquoted), True),
                (AUDIT._INDENTED_CLAUSE.match(unquoted) if was_blank else None, True),
                (AUDIT._MIDLINE_CLAUSE.search(unquoted), True),
            ):
                if not pattern or not gated:
                    continue
                tail = pattern.group(1)
                if unquoted.lstrip().startswith("|"):
                    tail = tail.split("|", 1)[0]
                if AUDIT._accept_clause(tail) is None:
                    stripped = tail.strip()
                    if not any(stripped.startswith(k) or k.startswith(stripped) for k in named):
                        unnamed.append(f"{surface.key}: {stripped[:80]}")
                break

    assert unnamed == [], (
        "a marker was refused that nobody has read yet; decide whether it is a "
        "criterion and name it in this file:\n  " + "\n  ".join(unnamed)
    )


# ---------------------------------------------------------------------------
# 4. THE INVARIANT — including the half of it that does NOT hold.
# ---------------------------------------------------------------------------


def test_the_appended_list_is_not_append_only(tmp_path):
    """`appended` is DOCUMENT-ordered, so an earlier clause shifts the later ones.

    The docstring of `incomplete_if` used to promise the opposite — "the second
    starts at the old length and can only ever grow at the end". It does not,
    and this test is what keeps the corrected wording honest. A future reader who
    "repairs" the parser to make the old sentence true will find this test in
    their way and read why it is not the fix.
    """
    body = (
        "# A ratified page\n"
        "\n"
        "## Incomplete if\n"
        "\n"
        "- first bullet\n"
        "- second bullet\n"
        "\n"
        "## Amendment\n"
        "\n"
        "{early}"
        "*Incomplete if*: the later clause, ratified first.\n"
    )
    before = tmp_path / "before.md"
    after = tmp_path / "after.md"
    before.write_text(body.format(early=""), encoding="utf-8")
    after.write_text(
        body.format(early="*Incomplete if*: an earlier clause, ratified later.\n\n"),
        encoding="utf-8",
    )

    listed_before, appended_before = AUDIT.criteria_split(before)
    listed_after, appended_after = AUDIT.criteria_split(after)

    # The bullets do not move. That is the guarantee.
    assert listed_before == listed_after == ["first bullet", "second bullet"]
    # The clauses DO. That is the limit of the guarantee.
    assert appended_before == ["the later clause, ratified first"]
    assert appended_after == [
        "an earlier clause, ratified later",
        "the later clause, ratified first",
    ]
    assert appended_before[0] == appended_after[1], (
        "the clause kept its text and changed index -- an inline clause is NOT "
        "append-only, which is exactly why no verdict may be keyed on one"
    )


def test_a_verdict_in_the_appended_zone_carries_its_anchor():
    """Past `len(listed)` an index is not safe by construction, so it must be anchored.

    An index below `len(listed)` names a bullet, and a bullet cannot be moved by
    anything the inline reader does. An index at or beyond it names an inline
    clause, whose position depends on where in the page the clause was written --
    `incomplete_if` says so in its own words, and the test above MEASURES it: an
    inline clause written earlier in the page shifts every clause below it.

    THIS GUARD USED TO ASK FOR SOMETHING ELSE, AND THE DAY IT FIRED IS THE DAY IT
    HAD TO CHANGE. It asserted that NO key reaches the zone at all, and its own
    docstring named the succession: "the day one is, this test goes red and the
    verdict must carry a `criterion` anchor, so `check_ledger_anchors.py` reports
    a drift instead of re-pointing in silence." That day is 2026-08-24, when
    `analyze-and-test[52]` and `[53]` recorded the two amendments of 2026-08-17 --
    inline clauses 14 and 15 of 25 on that surface -- which had been counted OPEN
    by `finished_work_audit.py` for a week with nothing said about them anywhere.
    Refusing to record them would not have made the index safer; it would only
    have kept two judged criteria unregistered, which the ledger's own rule reads
    as open.

    So the guarantee moves rather than disappearing, and it moves to the thing
    that can carry it. A key in the appended zone is admissible when, and only
    when, it carries a `criterion` anchor: `check_ledger_anchors.py` compares that
    anchor with the clause at that index on every run, so a clause that shifts
    turns into a RED DRIFT rather than a silent re-pointing. An unanchored key
    there stays refused -- it is exactly the `analyze-and-test[10]` failure of
    2026-08-08 with no way to notice it.

    THE OTHER HALF IS UNCHANGED, AND IT IS THE HARDER ONE. A key past the end of
    BOTH lists names no criterion at all, anchor or not, and a criterion that does
    not exist cannot have been judged. That check must run on every surface with a
    document, including the ones holding no inline clause: the guard once opened
    with `if not appended: continue`, which read as an optimisation and was not
    one. Measured 2026-08-21: 20 ledger surfaces have a document, the guard ran on
    SIX and skipped fourteen, while ten of the twenty carry a key on their very
    last bullet, one edit away from the boundary. Injecting `context-hub["999"]`
    left it green; the same injection on `governance` turned it red.
    """
    ledger = json.loads(AUDIT.LEDGER.read_text(encoding="utf-8"))
    doc_by_surface = {surface.key: surface.doc for surface in AUDIT.SURFACES}

    refused: list[str] = []
    for surface, entries in sorted(ledger.items()):
        if surface.startswith("_") or not isinstance(entries, dict):
            continue
        doc = doc_by_surface.get(surface)
        if doc is None:
            continue
        listed, appended = AUDIT.criteria_split(doc)
        for key in sorted((k for k in entries if k.isdigit()), key=int):
            if int(key) < len(listed):
                continue
            entry = entries[key]
            offset = int(key) - len(listed)
            if offset >= len(appended):
                refused.append(
                    f"{surface}[{key}] addresses NO criterion at all: the surface "
                    f"holds {len(listed)} bullets and {len(appended)} inline clauses"
                )
            elif not (isinstance(entry, dict) and entry.get("criterion")):
                refused.append(
                    f"{surface}[{key}] addresses inline clause {offset} of "
                    f"{len(appended)}, whose index moves with the page, and carries "
                    "no `criterion` anchor -- nothing could tell it apart from its "
                    "neighbour after one edit"
                )

    assert refused == [], (
        "a verdict is keyed past the stable zone without the anchor that makes it "
        "checkable — or names nothing:\n  " + "\n  ".join(refused)
    )


def test_every_ratified_document_with_criteria_is_named_by_a_surface():
    """The recurring defect: the document arrives, the table does not move.

    `SURFACES` carries the note four separate times — 2026-08-01, 08-02, 08-05,
    08-17 — each one recording a batch of ratified criteria that no instrument
    read because nothing named their page. It happened a fifth time: measured
    2026-08-21, `capabilities/placement-mapping.md` (20), `unresolved-values.md`
    (17), `module-boundaries.md` (15), `page-structure.md` (7) and
    `product-sheet.md` (5) held 64 criteria outside every instrument, so they
    were neither open nor closed.

    A comment cannot stop the sixth time; this can. Any file under
    `docs/product-architecture/` that carries a criterion must be named by an
    entry, or the writer must decide it is not a ratified surface and say so
    here.
    """
    #: Pages that carry no `Incomplete if` of their own and are not surfaces:
    #: indexes, registers of decisions, and research notes.
    not_a_surface = {"README.md", "capabilities/README.md"}

    docs = AUDIT.DOCS
    named = {surface.doc.resolve() for surface in AUDIT.SURFACES}
    unread: list[str] = []
    for page in sorted(list(docs.glob("*.md")) + list((docs / "capabilities").glob("*.md"))):
        relative = page.relative_to(docs).as_posix()
        if page.resolve() in named or relative in not_a_surface:
            continue
        listed, appended = AUDIT.criteria_split(page)
        if listed or appended:
            unread.append(f"{relative}: {len(listed) + len(appended)} criteria read by nothing")

    assert unread == [], (
        "a ratified page carries criteria that no `SURFACES` entry names, so "
        "they count neither open nor closed:\n  " + "\n  ".join(unread)
    )


def test_the_named_instruments_read_the_same_list():
    """`check_ledger_anchors` and `screens.py` must not own a second parser.

    Two parsers of one list are two answers to "what does index 13 name". The
    third one lived in `scripts/screens.py` until 2026-08-21 and answered 267
    where this one answered 480, including 0 for four surfaces whose lists are
    numbered `1.`.
    """
    assert ANCHORS.incomplete_if is AUDIT.incomplete_if
    assert SCREENS.incomplete_if is AUDIT.incomplete_if
    # And the answer itself agrees, surface by surface — identity of the symbol
    # would still allow a caller to pre-process the document before handing it
    # over.
    for surface in AUDIT.SURFACES:
        assert SCREENS.incomplete_if(surface.doc) == AUDIT.incomplete_if(surface.doc)


#: What "reading the list" looks like in code, as opposed to mentioning it in
#: prose: a REGEX over the marker, or a string walk anchored on it. Comments
#: never reach the AST, and a docstring is skipped explicitly, so a module that
#: merely talks about `Incomplete if` is not accused of parsing it.
_REGEX_META = (r"\s", r"\*", r"\d", "(?", "^#", "[^", ".+?", ".*?")
_WALK_METHODS = frozenset(
    {"startswith", "endswith", "partition", "rpartition", "split",
     "removeprefix", "find", "index", "count"}
)
_MARKER_TEXT = "Incomplete if"

#: The one module allowed to own the walk. Everything else imports from it.
_CANONICAL = REPO_ROOT / "scripts" / "finished_work_audit.py"


def _docstring_constants(tree) -> set[int]:
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            found.add(id(body[0].value))
    return found


def _carries_marker(node) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _MARKER_TEXT in node.value
    )


def _parses_the_marker(source: str) -> list[str]:
    """The lines of *source* that match on the marker instead of importing it."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    docstrings = _docstring_constants(tree)
    reasons: list[str] = []
    for node in ast.walk(tree):
        if _carries_marker(node) and id(node) not in docstrings:
            if any(token in node.value for token in _REGEX_META):
                reasons.append(f"line {node.lineno}: a regex over the marker")
                continue
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _WALK_METHODS
            and any(_carries_marker(arg) for arg in node.args)
        ):
            reasons.append(f"line {node.lineno}: .{node.func.attr}() on the marker")
        if isinstance(node, ast.Compare) and any(isinstance(op, ast.In) for op in node.ops):
            if any(_carries_marker(side) for side in [node.left, *node.comparators]):
                reasons.append(f"line {node.lineno}: a membership test on the marker")
    return reasons


def test_no_module_anywhere_re_implements_the_walk():
    """Find the NEXT second parser, without being told where to look.

    THE TEST ABOVE PINS THREE SYMBOLS, AND THAT IS ITS WHOLE BLIND SPOT. A fourth
    reader existed the day it was written: `scripts/working_context_corpus.py`
    carried the old broken walk — first `## Incomplete if` block, stop at the next
    `##` — and answered **359 criteria against 546**, with **ZERO** for
    `page-structure`, `user-bridge` and `visualization-and-rendering` and a
    different answer at index 0 for three more. It was not an audit script: it
    feeds `skills()` and `knowledge()`, which `scripts/push_working_context.py`
    pushes into the Context Hub, so it PUBLISHED a wrong list. Naming three
    symbols could never have found it.

    So this test names none. It walks every Python file of `scripts/` and
    `server/` and asks who matches on the marker in code. Whoever does must be
    the canonical parser, or must import from it.
    """
    offenders: list[str] = []
    for root in (REPO_ROOT / "scripts", REPO_ROOT / "server"):
        for path in sorted(root.rglob("*.py")):
            if path.resolve() == _CANONICAL.resolve():
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            reasons = _parses_the_marker(source)
            if not reasons:
                continue
            offenders.append(
                f"{path.relative_to(REPO_ROOT).as_posix()} -- " + "; ".join(reasons)
            )

    assert offenders == [], (
        "a module reads the `Incomplete if` list itself instead of importing "
        "`finished_work_audit.incomplete_if`. Two parsers are two answers to "
        "'what does index 13 name':\n  " + "\n  ".join(offenders)
    )


def test_the_detector_above_would_have_caught_the_fourth_reader():
    """A finder that finds nothing is indistinguishable from a finder that works.

    So the walk `working_context_corpus.py` carried until 2026-08-21 is replayed
    here, verbatim, and the detector has to name it — while the prose of this very
    file, which quotes the marker constantly, does not trip it.
    """
    the_old_fourth_reader = (CORPUS / "fourth-reader-2026-08-21.py.txt").read_text(
        encoding="utf-8"
    )
    assert _parses_the_marker(the_old_fourth_reader), (
        "the detector no longer recognises the very walk it was written for"
    )
    assert _parses_the_marker(Path(__file__).read_text(encoding="utf-8")) == [], (
        "the detector accuses this test file, whose markers are fixture text "
        "and prose"
    )


# ---------------------------------------------------------------------------
# 5. `--unanchored-drift` — the work order that says which verdicts to re-read.
# ---------------------------------------------------------------------------


_PAGE = """# A ratified page

## Incomplete if

{bullets}
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    )


def _drift_repo(
    tmp_path: Path,
    second_revision_bullets: str,
    first_revision_bullets: str = "- alpha\n- beta\n- gamma\n",
) -> Path:
    repo = tmp_path / "repo"
    docs = repo / "docs" / "product-architecture"
    docs.mkdir(parents=True)
    _git(repo.parent, "init", "--quiet", str(repo))

    page = docs / "surface.md"
    page.write_text(_PAGE.format(bullets=first_revision_bullets), encoding="utf-8")
    (docs / "completeness-ledger.json").write_text(
        json.dumps(
            {"surface": {"1": {"date": "2026-01-01", "evidence": "read", "verdict": "false"}}}
        ),
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "the verdict is written")

    page.write_text(_PAGE.format(bullets=second_revision_bullets), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "the document moves")
    return repo


def _run_drift(repo: Path):
    docs = repo / "docs" / "product-architecture"
    names = ("REPO", "DOCS", "LEDGER", "_DOC_BY_SURFACE")
    saved = {name: getattr(ANCHORS, name) for name in names}
    ANCHORS.REPO = repo
    ANCHORS.DOCS = docs
    ANCHORS.LEDGER = docs / "completeness-ledger.json"
    ANCHORS._DOC_BY_SURFACE = {"surface": docs / "surface.md"}
    try:
        return ANCHORS.unanchored_drift()
    finally:
        for name, value in saved.items():
            setattr(ANCHORS, name, value)


def test_unanchored_drift_names_a_verdict_whose_bullet_moved(tmp_path):
    """A bullet inserted above index 1 re-points the verdict, and it is reported."""
    repo = _drift_repo(tmp_path, "- alpha\n- INSERTED\n- beta\n- gamma\n")

    moved, rewritten, stable, unreadable = _run_drift(repo)

    assert stable == [] and unreadable == [] and rewritten == []
    assert len(moved) == 1
    assert moved[0].startswith("surface[1]")
    assert "now at [2]" in moved[0], moved[0]
    assert "beta" in moved[0] and "INSERTED" in moved[0]


def test_unanchored_drift_leaves_an_unmoved_verdict_alone(tmp_path):
    """A bullet added BELOW the verdict moves nothing, and must not be reported."""
    repo = _drift_repo(tmp_path, "- alpha\n- beta\n- gamma\n- APPENDED\n")

    moved, rewritten, stable, unreadable = _run_drift(repo)

    assert moved == [] and unreadable == [] and rewritten == []
    assert len(stable) == 1 and stable[0].startswith("surface[1]")


def test_unanchored_drift_reports_a_bullet_that_left_the_list(tmp_path):
    """A retired bullet is not silently re-anchored onto its neighbour.

    A DIFFERENT criterion written at the judged index opens on other words, so
    it is not a rewrite: the criterion that was judged exists at no index, and
    that is the worst of the three outcomes rather than the mildest.
    """
    repo = _drift_repo(tmp_path, "- alpha\n- a different criterion entirely\n- gamma\n")

    moved, rewritten, stable, unreadable = _run_drift(repo)

    assert stable == [] and unreadable == [] and rewritten == []
    assert len(moved) == 1
    assert "gone from the list" in moved[0], moved[0]


# --- and the distinction the count was missing entirely -------------------


def test_a_bullet_reworded_at_its_own_index_is_not_a_re_pointing(tmp_path):
    """`project-settings[2]`, in miniature — the only one of the 22, measured.

    "Country, Tax & Fees or Competitors activates without an impact preview"
    became "Country, Tax & Fees, Competitors **or Placement Mapping** activates
    …". Same index, same criterion, more words. Reported as RE-POINTED it puts a
    re-reading on a work order that has nothing to re-point; the verdict's
    correspondence never moved, only the evidence behind it may have aged.
    """
    repo = _drift_repo(
        tmp_path,
        "- alpha\n"
        "- Country, Tax & Fees, Competitors or Placement Mapping activates\n"
        "- gamma\n",
        first_revision_bullets=(
            "- alpha\n- Country, Tax & Fees or Competitors activates\n- gamma\n"
        ),
    )

    moved, rewritten, stable, unreadable = _run_drift(repo)

    assert moved == [] and stable == [] and unreadable == []
    assert len(rewritten) == 1
    assert rewritten[0].startswith("surface[1]")
    assert "STILL at [1]" in rewritten[0], rewritten[0]


def test_a_reworded_bullet_that_also_moved_is_still_a_re_pointing(tmp_path):
    """`context-hub[11]` and `datastream[11]`, which used to read "gone".

    Both were reported as having left the list because the comparison was
    equality: their text HAD changed, so no twin was found — while the criterion
    itself sits two indexes further down. "Gone" and "now at [n]" send a reader
    to two different places.
    """
    repo = _drift_repo(
        tmp_path,
        "- alpha\n"
        "- gamma\n"
        "- what a retrieval discarded is recomputed and kept on nothing\n",
        first_revision_bullets=(
            "- alpha\n"
            "- what a retrieval discarded is recomputed on every walk\n"
            "- gamma\n"
        ),
    )

    moved, rewritten, stable, unreadable = _run_drift(repo)

    assert rewritten == [] and stable == [] and unreadable == []
    assert len(moved) == 1
    assert "rewritten and now at [2]" in moved[0], moved[0]


def test_a_shared_opening_that_names_two_bullets_decides_nothing(tmp_path):
    """The threshold alone is not the rule, and this is the half that proves it.

    `REWRITE_MIN_OPENING` is met here — the two texts share far more than twelve
    characters — but the opening they share also opens the NEXT bullet. An
    ambiguous opening cannot say which criterion the verdict judged, so the
    verdict is treated as re-pointed and goes on the work order. Measured on the
    real ledger, this is the shape of `context-hub[10]`: nine shared characters
    opening two bullets.
    """
    repo = _drift_repo(
        tmp_path,
        "- alpha\n"
        "- a remark on a node is written and never read again\n"
        "- a remark on a node lands only in the audit trail\n",
        first_revision_bullets=(
            "- alpha\n- a remark can only be closed and not acted on\n- gamma\n"
        ),
    )

    moved, rewritten, stable, unreadable = _run_drift(repo)

    assert rewritten == [], rewritten
    assert len(moved) == 1 and stable == [] and unreadable == []


def test_the_opening_that_decides_a_rewrite_is_a_measured_boundary():
    """`REWRITE_MIN_OPENING` is load-bearing, so it is asserted on both sides.

    Measured 2026-08-21 over the 22 drifted verdicts: the one real rewrite in
    place shares 19 characters that open exactly ONE bullet, while coincidences
    between unrelated bullets share 0, 1, 2 or 9 characters and open 2, 20, 21 or
    28. Twelve sits between the two, and nothing else in this repository knows
    that.
    """
    assert ANCHORS.REWRITE_MIN_OPENING == 12
    listing = ["Country, Tax & Fees, Competitors or Placement Mapping activates", "elsewhere"]
    assert ANCHORS._same_criterion(
        "Country, Tax & Fees or Competitors activates", listing[0], listing
    )
    # Nine characters, the widest coincidence measured, must NOT qualify.
    coincidence = ["a remark on a node lands only in the audit trail", "elsewhere"]
    assert not ANCHORS._same_criterion(
        "a remark can only be closed and not acted on", coincidence[0], coincidence
    )


# ---------------------------------------------------------------------------
# 6. `screens/board.json` — the CACHE that serves this list to five agents.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def resolved_board():
    """`resolve()` once. It reads ~570 files and costs about seven seconds."""
    return SCREENS.resolve()


def test_the_board_cache_is_confronted_with_the_sources(resolved_board):
    """The comparison nothing ever made — and that is the whole finding.

    `load_board()` preferred `screens/board.json` the moment the file existed. No
    command rewrote it, no test compared it to `resolve()`, and `SESSIONS.md` had
    already recorded what that costs on 2026-08-03: `backward` still answering
    `30/31 ... 1 are not` after the `skill` type had left `navigation.ts`, where
    moving the file aside gave `30/30 ... 0 are not`. Eighteen days later the
    board still held the 474 criteria of 2026-08-17 while the parser read 1095.

    Two other scripts read that file by its path — `screen_expectations.py` and
    `working_context_corpus.py`, the second of which PUBLISHES into the Context
    Hub — so a stale board did not stay inside this tool. Both now come through
    `load_board()`, and this test is what makes `load_board()` worth trusting.
    """
    assert SCREENS.load_board() == resolved_board, (
        "`screens/board.json` disagrees with the sources it was derived from; "
        "the staleness guard let a perished reading through"
    )
    # And when the file IS current, the file itself has to agree — otherwise the
    # assertion above would be satisfied by a guard that always falls back, which
    # would make this test say nothing about the cache at all.
    if SCREENS.board_is_stale() is None:
        on_disk = [
            SCREENS.Screen(**entry)
            for entry in json.loads(SCREENS.read(SCREENS.BOARD_JSON))["screens"]
        ]
        assert on_disk == resolved_board, (
            "`screens/board.json` is younger than every source it derives from "
            "and still disagrees with `resolve()`; the freshness check is "
            "watching the wrong files"
        )


def test_a_board_older_than_its_sources_is_not_served(tmp_path, monkeypatch, capsys,
                                                      resolved_board):
    """A stale cache is replaced by a fresh reading, and the person is told.

    What they see: one line naming the file that moved and the command that
    refreshes the cache on disk. Not a deployment state, not a table name — the
    gesture that repairs it.
    """
    from dataclasses import asdict  # noqa: PLC0415

    fake = tmp_path / "board.json"
    invented = asdict(resolved_board[0]) | {"id": "a-screen-that-does-not-exist"}
    fake.write_text(
        json.dumps({"generated": "2026-01-01T00:00:00+00:00", "screens": [invented]}),
        encoding="utf-8",
    )
    import os  # noqa: PLC0415

    os.utime(fake, (0, 0))  # older than every source of the repository
    monkeypatch.setattr(SCREENS, "BOARD_JSON", fake)

    assert SCREENS.board_is_stale() is not None
    served = SCREENS.load_board()

    assert [s.id for s in served] != ["a-screen-that-does-not-exist"]
    assert served == resolved_board
    message = capsys.readouterr().err
    assert "screens/board.json" in message
    assert "python scripts/screens.py resolve" in message


def test_a_board_newer_than_every_source_is_served_as_it_stands(tmp_path, monkeypatch,
                                                                resolved_board):
    """The other half: without the guard, THIS is what every reader got.

    The cache path is real and it is taken whenever the board is current. So the
    staleness check is the only thing standing between a reader and a file that
    may say anything at all — which is why the fabricated screen below comes back
    verbatim once the file is younger than its sources.
    """
    from dataclasses import asdict  # noqa: PLC0415

    fake = tmp_path / "board.json"
    invented = asdict(resolved_board[0]) | {"id": "a-screen-that-does-not-exist"}
    fake.write_text(
        json.dumps({"generated": "2126-01-01T00:00:00+00:00", "screens": [invented]}),
        encoding="utf-8",
    )
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415

    os.utime(fake, (time.time() + 86_400, time.time() + 86_400))
    monkeypatch.setattr(SCREENS, "BOARD_JSON", fake)

    assert SCREENS.board_is_stale() is None
    assert [s.id for s in SCREENS.load_board()] == ["a-screen-that-does-not-exist"]


def test_the_board_watches_the_directories_and_not_a_list_of_paths(monkeypatch, tmp_path):
    """A guard pinned to a filename goes quiet the day the code moves.

    It has happened twice in this repository already, and both times in silence:
    the navigation registry split into seven files on 2026-08-12 (`NAV_SURFACES`)
    and the object dispatch left `ContentRouter` the same day
    (`routed_tabs_across`). So the sources are enumerated by walking directories,
    and a file created inside one of them — a name nobody wrote down — must make
    the board stale.
    """
    fake = tmp_path / "board.json"
    fake.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(SCREENS, "BOARD_JSON", fake)
    assert SCREENS.board_is_stale() is None, "the fixture board must start current"

    newcomer = SCREENS.UI / "src" / "shell" / "navigation" / "_staleness_probe.ts"
    newcomer.write_text("// written by a test, removed by it\n", encoding="utf-8")
    try:
        assert SCREENS.board_is_stale() == newcomer.relative_to(SCREENS.ROOT).as_posix()
    finally:
        newcomer.unlink()


def test_unanchored_drift_never_writes_an_anchor(tmp_path):
    """It is a measurement. Stamping today's text into an unknown mapping is the
    one move `check_ledger_anchors.py` exists to prevent."""
    repo = _drift_repo(tmp_path, "- alpha\n- INSERTED\n- beta\n- gamma\n")
    ledger = repo / "docs" / "product-architecture" / "completeness-ledger.json"
    before = ledger.read_bytes()


    _run_drift(repo)

    assert ledger.read_bytes() == before
