#!/usr/bin/env python3
"""Finished-work audit — the perimeter is never chosen by whoever runs it.

    python scripts/finished_work_audit.py             # verdict per surface
    python scripts/finished_work_audit.py datastream  # one surface, detailed
    python scripts/finished_work_audit.py --plan      # ranked delivery plan
    python scripts/finished_work_audit.py --gate      # regressions only, for hooks
    python scripts/finished_work_audit.py --baseline  # accept current findings as debt
    python scripts/finished_work_audit.py --inline-criteria  # clauses read outside a list

Why this exists. Over two days, eleven explicit requests to audit the existing
code produced seven audits and zero detections, because the auditor chose the
perimeter every time — and chose, every time, the perimeter of the code he had
just touched. An audit scoped from recent work cannot, by construction, find
what was never touched. So here the perimeter is enumerated from the inventory:
every ratified document, every component file, every contracted tab. Nothing is
scoped from a diff, a complaint or a screenshot.

Three independent findings, none of which can be argued away:

1. **Orphans.** Components built, tested, and mounted nowhere. A barrel
   re-export does not count as a mount — an edge that exists but leads nowhere
   is exactly the defect being looked for. This one check found 13 of 114 files.

2. **Contract structure.** Tabs the ratified document requires, against the tabs
   the router actually dispatches. A `default:` branch is reported on sight: it
   turns a missing tab into a plausible screen, which is the failure mode that
   symptom-driven verification can never see.

3. **Criteria ledger.** Each `Incomplete if` bullet of each ratified document
   needs a recorded verdict *with the evidence that produced it*. Unrecorded
   counts as still true, so forgetting can never read as progress.

`--gate` reports only what is not already in `known-debt.json`. Standing debt is
visible but does not block; a *new* orphan or a *new* aliased tab does. That is
the whole self-control mechanism: accepting a regression requires writing it
down, deliberately, in a file someone else can read.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs" / "product-architecture"
LEDGER = DOCS / "completeness-ledger.json"
BASELINE = DOCS / "known-debt.json"
UI = ROOT / "ui" / "admin" / "src"


# --------------------------------------------------------------------------
# Ratified documents, and the code meant to implement them.
# --------------------------------------------------------------------------


@dataclass
class Surface:
    key: str
    doc: Path
    owns: tuple[str, ...] = ()  # UI directories this surface is accountable for
    tab_table_header: str | None = None
    router: Path | None = None
    router_object: str | None = None


ROUTER = UI / "shell" / "ContentRouter.tsx"

#: LES DEUX TABLES DE DISPATCH, qui ont quitte `ContentRouter` avec AD-42
#: (2026-08-12). Cet audit lisait UN CHEMIN, et un garde sur un chemin devient
#: faux en silence le jour ou le code bouge : il a signale << four Data objects
#: do not share DataObjectWorkbench >> alors que les quatre le partagent
#: toujours, une ligne plus loin dans un autre fichier. Il lit desormais les
#: trois ensemble, donc il suit le CODE.
ROUTER_SURFACES = (
    ROUTER,
    UI / "shell" / "objectSurfaces.tsx",
    UI / "shell" / "collectionSurfaces.tsx",
)

SURFACES = [
    Surface("datastream", DOCS / "datastream-workbench-and-wizard.md",
            owns=("datastreams",), tab_table_header="| Tab |",
            router=ROUTER, router_object="datastream"),
    # `datamodel` was this surface's owned directory until 2026-08-25. It held
    # exactly two files -- `FieldDetailDrawer.tsx` and `NewFieldDialog.tsx` --
    # both orphaned since their host `FieldsTable.tsx` was deleted, and both went
    # with the legacy field-dictionary write routes they were the only callers of
    # (story 49.3 AC1). The directory no longer exists, so the claim is dropped
    # rather than left pointing at a phantom path: a perimeter that claims a path
    # that is not there owns nothing while LOOKING like coverage, which is the
    # exact failure the `qualite` note below describes.
    Surface("data", DOCS / "data.md"),
    Surface("overview", DOCS / "overview.md"),
    # `rapports` was this surface's owned directory and NO SUCH DIRECTORY EXISTS:
    # it was replaced by `analyze/` (QueryDoor, QuerySpecWorkbench,
    # ResultWorkbench, VisualizationMount) and `analyze-artifacts/` (Reports,
    # Renders), which `analyze-and-test.md:169` ratifies by naming those screens
    # in English. The claim produced the `self_check` finding "claims
    # ui/admin/src/rapports/, which does not exist" and, worse, owned NOTHING
    # while looking like coverage -- the one failure the note below describes.
    # The two real directories are claimed rather than the claim dropped: a
    # surface with no `owns` sends its orphans to the unattributed bucket, which
    # is visible but nobody's.
    Surface("analyze-and-test", DOCS / "analyze-and-test.md",
            owns=("analyze", "analyze-artifacts")),
    # `qualite` was a fourth entry here and no such directory exists, under that
    # name or any other -- it is a survivor of the same first version the note
    # below describes. A perimeter that claims a phantom path owns nothing while
    # LOOKING like coverage, which is the one failure an audit must not have.
    # Data quality lives in a single top-level file, not a directory, so it stays
    # in the unattributed bucket where it is visible rather than being claimed by
    # a surface that would then report it as covered.
    Surface("governance", DOCS / "governance.md",
            owns=("governance", "authorizations", "orgs")),
    # No directory maps to Project settings with confidence; leaving it empty
    # sends its orphans to the unattributed bucket, which is visible. Guessing
    # a name creates a surface that silently owns nothing — the first version of
    # this table declared five directories that did not exist.
    Surface("project-settings", DOCS / "project-settings.md"),
    # Ajoutee 2026-08-05 (AI-177). Le document existait depuis le 2026-08-04 et le
    # ledger portait deja ses verdicts sous la cle `organization-settings`, mais la
    # table ne le nommait pas : ses 6 criteres etaient hors de tout instrument, donc
    # ni ouverts ni fermes -- exactement le defaut decrit plus bas pour les quatre
    # documents de capacite. Aucun repertoire possede : la surface est rendue par
    # `ui/admin/src/shell/pages/` avec les autres pages de reglages, et revendiquer
    # `shell/` ferait porter a cette surface tout le chrome de la console.
    Surface("organization-settings", DOCS / "organization-settings.md"),
    # Added 2026-07-31 with the page itself. Sixteen stories (22.9-22.24) shipped
    # against a target that lived only in _bmad-output/, so this surface had no
    # `Incomplete if` and nothing about it could be judged finished. It declares
    # NO owned directory on purpose: its only UI component sits under
    # datastreams/onboarding/ and is already attributed to the datastream surface
    # (and recorded as an orphan). Claiming a directory here would move a finding
    # rather than resolve it.
    Surface("file-source-ingestion", DOCS / "file-source-ingestion.md"),
    # Ajoutee 2026-08-01 avec la page. Elle ne decrit AUCUNE surface : elle decrit
    # le chemin ENTRE les surfaces, et ses criteres sont transverses par nature
    # (un maillon qui produit un format que le suivant refuse n'appartient a
    # aucune des deux extremites). Elle est enregistree quand meme : un
    # << Incomplete if >> que rien ne lit est exactement le defaut que ce depot
    # trouve en boucle. Aucun repertoire possede -- elle n'en a pas.
    Surface("data-path", DOCS / "data-path.md"),
    Surface("context-hub", DOCS / "context-hub.md", owns=("connaissances",)),
    Surface("competitors", DOCS / "capabilities" / "competitors.md"),
    Surface("country", DOCS / "capabilities" / "country.md"),
    Surface("currency-fx", DOCS / "capabilities" / "currency-fx.md"),
    Surface("reporting-timezone", DOCS / "capabilities" / "reporting-timezone.md"),
    Surface("tax-fees", DOCS / "capabilities" / "tax-fees.md"),
    # Added 2026-08-21. `capabilities/placement-mapping.md` has EXACTLY the shape
    # of the three entries above — a bulleted `## Incomplete if`, in the same
    # directory, written by the same hand — and no entry named it, so its 20
    # criteria sat outside every instrument. That is not a scope decision; it is
    # the omission the notes above already describe three times: the document
    # arrives, the table does not move.
    Surface("placement-mapping", DOCS / "capabilities" / "placement-mapping.md"),
    # Ajoutees 2026-08-02. Ces quatre documents portent un `## Incomplete if` et
    # n'etaient dans AUCUNE entree de cette table : leurs 28 criteres etaient donc
    # hors de tout instrument -- ni ouverts, ni fermes, simplement invisibles. La
    # regle du ledger (« un critere non enregistre compte pour VRAI ») ne peut
    # rien contre un critere que le script ne lit pas.
    #
    # C'est le meme defaut que file-source-ingestion et data-path avaient le
    # 2026-08-01, et il se represente a chaque page ecrite apres cette table : le
    # document arrive, la table ne bouge pas. Les quatre entrent donc SANS
    # repertoire possede -- aucun ne decrit une surface d'ecran, et revendiquer un
    # repertoire deplacerait des trouvailles au lieu de les resoudre.
    #
    # Ils apparaissent immediatement a 6/6, 6/6, 4/4 et 12/12 ouverts. Ce n'est pas
    # une regression : c'est le chiffre qui etait deja vrai et que personne ne
    # voyait. `gate()` ne bloque pas sur les criteres ouverts, donc aucune session
    # voisine ne se retrouve arretee par cette inscription.
    # Ajoutees 2026-08-17 (chantier 67-22, meme classe que les deux notes
    # ci-dessus : « le ledger ne mesure pas ce qu'il ne connait pas »). Cinq
    # documents ratifies portaient des criteres qu'AUCUN instrument ne lisait --
    # 42 au total, ni ouverts ni fermes. Les rapports 03, 04, 10 et 12 de
    # `reviews/audit-2026-08-17/` les nomment un par un ; le rapport 10 le dit
    # sans detour : « aucune entree dans completeness-ledger.json, donc tous
    # leurs Incomplete if comptent ouverts, y compris ceux manifestement clos
    # dans le code ».
    #
    # Aucun repertoire possede, pour la raison deja donnee plus haut : aucun de
    # ces cinq ne decrit une surface d'ecran a lui seul. `visualization-and-
    # rendering` est rendu par les primitives dataviz que `analyze-and-test`
    # possede deja, et `user-bridge` decrit le controle, pas un ecran.
    #
    # `visualization-and-rendering.md` n'a AUCUN bloc `## Incomplete if` : ses
    # neuf criteres vivent sous deux `Add to **Incomplete if**:` d'amendement.
    # `execution-substrate.md` et `mcp-tool-surface.md` numerotent les leurs.
    # Les trois etaient invisibles pour deux raisons differentes du meme
    # parseur, reparees ensemble dans `incomplete_if`.
    Surface("visualization-and-rendering", DOCS / "visualization-and-rendering.md"),
    Surface("first-figure-path", DOCS / "first-figure-path.md"),
    Surface("execution-substrate", DOCS / "execution-substrate.md"),
    Surface("mcp-tool-surface", DOCS / "mcp-tool-surface.md"),
    Surface("user-bridge", DOCS / "user-bridge.md"),
    Surface("caveats-register", DOCS / "caveats-register.md"),
    Surface("element-control-loop", DOCS / "element-control-loop.md"),
    Surface("glossary", DOCS / "glossary.md"),
    Surface("proactive-assertions", DOCS / "proactive-assertions.md"),
    # Added 2026-08-21 with `placement-mapping` above. Four more ratified
    # documents carried an `Incomplete if` that no entry named — 44 criteria,
    # neither open nor closed. No owned directory, for the reason already given
    # three times higher up: `page-structure` describes the shared chrome
    # (claiming `shell/` would make this surface accountable for the whole
    # console), `module-boundaries` describes how `server/` is divided,
    # `unresolved-values` a lens several workspaces render, and `product-sheet` a
    # written deliverable rather than a screen.
    Surface("unresolved-values", DOCS / "unresolved-values.md"),
    Surface("module-boundaries", DOCS / "module-boundaries.md"),
    Surface("page-structure", DOCS / "page-structure.md"),
    Surface("product-sheet", DOCS / "product-sheet.md"),
]


# --------------------------------------------------------------------------
# Parsing the documents — the contract is read, never restated here.
# --------------------------------------------------------------------------


#: A heading that opens a criteria list. `## Incomplete if`, and the H3 variants
#: amendments use (`### Incomplete if — session revocation`). `user-bridge.md`
#: writes the same heading in French; a criterion is not less binding for the
#: language of its title.
_CRITERIA_HEADING = re.compile(
    r"^#{2,3}\s+(?:Incomplete if\b|Ce document est incomplet tant que\b)", re.I
)

#: `### Replaces the last `Incomplete if` bullet` — a SUBSTITUTION, not an
#: addition. `file-source-ingestion.md:309` uses it: reading it as an addition
#: would leave the replaced bullet in the list and shift every index after it.
_REPLACES_HEADING = re.compile(
    r"^#{2,3}\s+Replaces the last\s+`?Incomplete if`?\s+bullet", re.I
)

#: An amendment that appends to the list without opening a heading of its own —
#: `visualization-and-rendering.md` and `governance.md` both write it this way,
#: at the end of a ratified amendment.
_ADD_TO_LIST = re.compile(r"^Add to\s+\*\*Incomplete if\*\*\s*:\s*$", re.I)

#: A bullet, dashed or NUMBERED. `execution-substrate.md` and
#: `mcp-tool-surface.md` number theirs, and a parser that reads only `- ` read
#: their lists as EMPTY — 14 and 9 criteria that were neither open nor closed.
_CRITERION_BULLET = re.compile(r"^(?:-|\d+\.)\s+(.*)$")

_ANY_HEADING = re.compile(r"^#{1,6}\s+")

#: --------------------------------------------------------------------------
#: THE INLINE FORMS. Three shapes below were ratified in the body of a document
#: and read by NOTHING, because every pattern above expects a heading or a
#: bullet. Until 2026-08-21 they were neither open nor closed -- the same defect
#: class the header of `incomplete_if` already records twice, in its third
#: occurrence.
#:
#: NO COUNT IS WRITTEN HERE, AND THAT IS THE REPAIR. This comment said "54
#: ratified criteria across seven surfaces" while
#: `python scripts/finished_work_audit.py --inline-criteria` printed 55, in the
#: very commit that wrote both — a number retyped from a run is stale as soon as
#: a document gains a clause, which happens weekly. The command derives it, per
#: surface and in total, and it is the only place the figure is allowed to live.
#:
#: They are read into a SEPARATE list and appended after every bullet, never
#: interleaved at their document position. See `incomplete_if` for why that is
#: the whole point rather than a detail.
#: --------------------------------------------------------------------------

#: A blockquote fence, stripped before the inline patterns are tried and never
#: before the bullet ones. `datastream-workbench-and-wizard.md:3344` writes a
#: ratified clause inside a `>` block; stripping the fence for EVERY line would
#: promote quoted bullets into the established list and move its indexes.
_BLOCKQUOTE = re.compile(r"^\s*>\s?")

#: The marker itself, however a document emphasises it: `**Incomplete if**`,
#: `*Incomplete if*`, `` `Incomplete if` ``, or bare.
_MARKER = r"(?:\*\*|\*|`)?Incomplete if(?:\*\*|\*|`)?"

#: `**Incomplete if** (adds to the list above):` — a list opener that is neither
#: a heading nor `Add to **Incomplete if**:`. `analyze-and-test.md` uses it four
#: times and every bullet under those four was invisible.
_INLINE_LIST_OPENER = re.compile(rf"^{_MARKER}\s*\([^)]*\)\s*:\s*$", re.I)

#: `Add to **Incomplete if**: a control of the Query Spec is accepted and not
#: applied; …` — the same opener as `_ADD_TO_LIST`, carrying its criterion on
#: the SAME line instead of in a list under it. `_ADD_TO_LIST` anchors on end of
#: line, so these three fell through as ordinary prose.
_ADD_TO_LIST_INLINE = re.compile(
    r"^Add to\s+\*\*Incomplete if\*\*\s*:\s*(\S.*)$", re.I
)

#: `*Incomplete if*: a declared object kind is absent from …` — a clause stated
#: where it applies, in the paragraph that ratifies it, with no list around it.
#: Three positions, because ratified documents use all three: at the head of a
#: line, indented inside a numbered item (`proactive-assertions.md:310`), and
#: mid-paragraph after a full stop (`governance.md:1289`, and inside a table
#: cell at `datastream-workbench-and-wizard.md:1306`).
_INLINE_CLAUSE = re.compile(rf"^{_MARKER}\s*(?:[:—–]|--)?\s*(\S.*)$")
_INDENTED_CLAUSE = re.compile(rf"^\s+{_MARKER}\s*(?:[:—–]|--)?\s*(\S.*)$")
_MIDLINE_CLAUSE = re.compile(rf"(?<=[.!?—])\s+{_MARKER}\s*(?:[:—–]|--)?\s*(\S.*)$")

#: What separates a CLAUSE from PROSE ABOUT the clauses. A criterion states a
#: condition; a reference makes `Incomplete if` the object of a sentence --
#: "`Incomplete if` clause 2 of this document", "*Incomplete if* list at line
#: 509", "`Incomplete if` criteria now carry a dated verdict", "no `Incomplete
#: if` — which is precisely how a fallback reached production". Seven such
#: references start a line in the ratified set and not one of them is a
#: criterion, so they are named here rather than counted.
_CLAUSE_IS_A_REFERENCE = re.compile(
    r"^(?:clauses?|lists?|criteri(?:a|on)|bullets?|which|added|itself|above|below)\b",
    re.I,
)

#: A clause is a sentence, not a fragment: `` `Incomplete if`. `` closing a
#: sentence in `organization-settings.md:16` is not a criterion of anything.
_CLAUSE_MIN_CHARS = 20


def _accept_clause(tail: str) -> str | None:
    """*tail* when it states a criterion, None when it merely refers to one."""
    tail = tail.strip()
    if len(tail) < _CLAUSE_MIN_CHARS or _CLAUSE_IS_A_REFERENCE.match(tail):
        return None
    if not re.match(r"[\w`À-ÿ«\"'(\[]", tail):
        return None
    return tail


def _inline_clause(line: str, previous_blank: bool) -> str | None:
    """The criterion stated on *line*, or None when the line merely mentions one.

    INDENTATION IS ONLY READ AFTER A BLANK LINE, and that is not a nicety. An
    indented marker is either the head of its own paragraph or the CONTINUATION
    of the sentence above it — `user-bridge.md:546` writes "quand elle ferme un
    critère `Incomplete if` — la preuve est la commande", which is a rule about
    evidence, not a criterion. At column 0 the ambiguity does not exist, so the
    blank line is not required there (`governance.md:1274` states a real clause
    directly under the paragraph it closes).
    """
    opening = _INLINE_CLAUSE.match(line)
    if opening:
        return _accept_clause(opening.group(1))
    if previous_blank:
        indented = _INDENTED_CLAUSE.match(line)
        if indented:
            return _accept_clause(indented.group(1))
    embedded = _MIDLINE_CLAUSE.search(line)
    if embedded:
        tail = embedded.group(1)
        # A table row states its clause inside one cell; the pipe ends it, and
        # swallowing the next column would put another subject in the criterion.
        if line.lstrip().startswith("|"):
            tail = tail.split("|", 1)[0]
        return _accept_clause(tail)
    return None


def _flush(buffer: list[str]) -> str:
    return re.sub(r"\s+", " ", " ".join(buffer)).strip().rstrip(";.")


def incomplete_if(doc: Path) -> list[str]:
    """Every `Incomplete if` bullet of *doc*, in document order.

    THE PARSER READ THE FIRST BLOCK AND STOPPED. Its regex ran from
    `## Incomplete if` to the next `## `, so an amendment that added criteria
    under its own H3 — or after `Add to **Incomplete if**:` — contributed
    nothing at all. Measured 2026-08-17: `organization-settings.md:161` holds
    six session-revocation criteria under an H3, and its own text names the
    reason they were put there (`finished_work_audit.py:166 parses the FIRST
    block`); `visualization-and-rendering.md` holds nine under two
    `Add to **Incomplete if**:` lists and has no first block at all.

    Order is document order, and appended blocks come after the opening one, so
    the zero-based keys `completeness-ledger.json` records keep pointing at the
    bullets they judged. `scripts/check_ledger_anchors.py` reads this same
    function, so the two instruments cannot disagree about what bullet an index
    names.

    AND THE THIRD OCCURRENCE OF THE SAME DEFECT: a criterion written as a CLAUSE
    rather than as a bullet. `*Incomplete if*: a declared object kind is absent
    from the Registries lens …` is ratified prose in the paragraph it governs,
    and no heading and no `- ` ever announced it, so nothing read it. Measured
    2026-08-21 by `--inline-criteria` on this same file.

    WHY THEY ARE APPENDED AND NEVER INTERLEAVED. The ledger keys a verdict by the
    ZERO-BASED INDEX of a bullet in this list. `governance.md:92` states its
    clause before the document's own `## Incomplete if`; reading it at its
    document position would insert a criterion at index 0 and silently re-point
    all 35 recorded verdicts of that surface one bullet down — the exact failure
    `check_ledger_anchors.py` exists to refuse. So the walk fills TWO lists: the
    bullets the parser already read, in the order it already read them, and the
    newly-readable clauses, concatenated after them.

    WHAT THAT BUYS, EXACTLY — and this paragraph used to overstate it. It said
    the second list "can only ever grow at the end". IT DOES NOT. `appended` is
    filled in DOCUMENT order like everything else here, so a clause written
    EARLIER in the page shifts every appended clause below it. Measured
    2026-08-21 on `governance.md`: inserting one inline clause at line 60, ahead
    of the one at line 92, moved all 13 appended entries of that surface down by
    one — while its 47 bullets did not move at all.

    So the guarantee is the FIRST list, and only it: no inline form can enter
    `listed`, therefore no recorded verdict can be re-pointed as long as every
    recorded key addresses a bullet.

    AND THE DAY A VERDICT LEFT THAT ZONE HAS ARRIVED — 2026-08-24, when
    `analyze-and-test[52]` and `[53]` recorded the two amendments of 2026-08-17,
    inline clauses 14 and 15 of that surface's 25. Both had been counted OPEN
    here while nothing anywhere said what they were judged against, which is the
    state the ledger's own rule calls open and the review that found them called
    a hole. Refusing the record would not have made the index safer.

    So the guarantee moved to the thing that can carry it past `len(listed)`: the
    `criterion` anchor. `scripts/check_ledger_anchors.py` compares it with the
    clause at that index on every run, so a clause that shifts is a RED DRIFT
    rather than a silent re-pointing.
    `server/tests/core/test_criteria_parser.py::test_a_verdict_in_the_appended_zone_carries_its_anchor`
    pins exactly that: in the appended zone an anchored key is admissible, an
    unanchored one is refused, and a key past the end of BOTH lists is refused
    outright — a criterion that does not exist cannot have been judged.
    """
    listed, appended = criteria_split(doc)
    return listed + appended


def criteria_split(doc: Path) -> tuple[list[str], list[str]]:
    """The bullets this parser always read, and the clauses it learned to read.

    Kept separate so `--inline-criteria` can report the second half without
    re-deriving it, and so the concatenation order is stated in one place.
    """
    if not doc.exists():
        return [], []
    # `listed` holds what the bullet parser always read, in its original order:
    # nothing below may insert into it, only `replace` may pop its last entry.
    # `appended` holds the clauses and the amendment lists this function learned
    # to read on 2026-08-21, and is concatenated after it.
    listed: list[str] = []
    appended: list[str] = []
    buffer: list[str] = []
    target = listed
    mode: str | None = None
    previous_blank = True

    def close_bullet() -> None:
        nonlocal buffer
        if buffer:
            target.append(_flush(buffer))
            buffer = []

    for line in doc.read_text(encoding="utf-8").splitlines():
        # The fence is removed for the inline patterns ONLY. A quoted `- ` is
        # not a bullet of the established list, and promoting one would move
        # every index under it.
        unquoted = _BLOCKQUOTE.sub("", line)
        was_blank, previous_blank = previous_blank, not unquoted.strip()

        if _ANY_HEADING.match(line):
            close_bullet()
            target = listed
            if _REPLACES_HEADING.match(line):
                mode = "replace"
            elif _CRITERIA_HEADING.match(line):
                mode = "collect"
            else:
                mode = None
            continue
        if _ADD_TO_LIST.match(line):
            close_bullet()
            target = listed
            mode = "collect"
            continue

        # --- the inline forms, all of them appended ---
        if _INLINE_LIST_OPENER.match(unquoted):
            close_bullet()
            target = appended
            mode = "collect"
            continue
        stated = _ADD_TO_LIST_INLINE.match(unquoted)
        clause = (
            _accept_clause(stated.group(1)) if stated
            else _inline_clause(unquoted, was_blank)
        )
        if clause:
            close_bullet()
            target = appended
            buffer = [clause]
            mode = "clause"
            continue
        if mode == "clause":
            # A clause runs to the end of its paragraph, exactly like the bullet
            # it should have been. A blank line, a heading or a bullet ends it.
            if not unquoted.strip() or _CRITERION_BULLET.match(unquoted):
                close_bullet()
                mode = None
                continue
            buffer.append(unquoted.strip())
            continue
        # --- the established forms, byte for byte as before ---

        if mode is None:
            continue
        bullet = _CRITERION_BULLET.match(line)
        if bullet:
            close_bullet()
            if mode == "replace" and target:
                target.pop()
                mode = "collect"
            buffer = [bullet.group(1)]
            continue
        if buffer and line.startswith((" ", "\t")) and line.strip():
            buffer.append(line.strip())
            continue
        if not line.strip():
            close_bullet()
            continue
        # Prose under a collecting heading closes the list only when the block
        # was opened inline: a heading block may carry a paragraph between its
        # title and its bullets (`organization-settings.md:163-173`).
        close_bullet()
    close_bullet()
    return listed, appended


#: A heading and its depth, so a section can be bounded by the next heading of
#: the same rank instead of by the TITLE of whatever happens to follow it.
_HEADING_DEPTH = re.compile(r"^(#{1,6})\s+(.*?)\s*$")


def section_body(text: str, heading: str, depth: int = 2) -> str | None:
    """The body of the `## <heading>` section of *text*, None when it is absent.

    THIS IS HERE SO NOTHING ELSE HAS TO SPELL `Incomplete if` TO FIND THE END OF A
    SECTION. `server/tests/conformance/test_country_layer_contract.py` sliced its
    layer table with `text.split("\\n## Layer contract\\n")[1].split("\\n## Incomplete
    if")[0]`: it never needed the criteria list, only the boundary that happened
    to follow its table — and that made it a second module matching on the
    marker, which is the one thing
    `test_criteria_parser.py::test_no_module_anywhere_re_implements_the_walk`
    refuses. A caller that bounds a section by naming the next section's title
    also breaks the day that title moves, silently and with no criterion involved.

    The bound is the next heading of the SAME rank or shallower, so a subsection
    written under the section stays inside it — `geographic-reporting.md` carries
    `### The one reader of the retired posture` under `## Layer contract`, and it
    belongs to that section.
    """
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        found = _HEADING_DEPTH.match(line)
        if found is None:
            continue
        if start is None:
            if len(found.group(1)) == depth and found.group(2) == heading:
                start = index + 1
            continue
        if len(found.group(1)) <= depth:
            return "\n".join(lines[start:index])
    if start is None:
        return None
    return "\n".join(lines[start:])


def contracted_tabs(doc: Path, header: str) -> list[str]:
    tabs, started = [], False
    for line in doc.read_text(encoding="utf-8").splitlines():
        if line.startswith(header):
            started = True
            continue
        if started:
            if not line.startswith("|"):
                break
            name = re.match(r"\|\s*\*\*(.+?)\*\*", line)
            if name:
                tabs.append(name.group(1).strip())
    return tabs


def routed_tabs_across(routers: Sequence[Path], obj: str) -> tuple[set[str], bool]:
    """The tabs an object routes, read across EVERY file that may hold its switch.

    THE SIXTH GUARD PINNED TO A PATH. AD-42 moved the object dispatch out of
    `ContentRouter` and `df59c3b3` repaired five readers for it; this one kept
    reading the single file and answered "7/7 contracted tabs unrouted" on the
    day the seven moved to `objectSurfaces.tsx` -- measured 2026-08-12, where
    `routed_tabs` returns the full set. A guard that names a path is wrong the
    day the code moves, and silently: the finding it produces looks exactly like
    a real regression.
    """
    keys: set[str] = set()
    catch_all = False
    for router in routers:
        if not router.exists():
            continue
        found, has_default = routed_tabs(router, obj)
        keys |= found
        catch_all = catch_all or has_default
    return keys, catch_all


def routed_tabs(router: Path, obj: str) -> tuple[set[str], bool]:
    text = router.read_text(encoding="utf-8")
    block = re.search(rf'objectType === "{obj}".*?^  \}}', text, re.M | re.S)
    if not block:
        return set(), False
    body = block.group(0)
    keys = {k for k in re.findall(r'^\s*case "([a-z0-9-]+)":', body, re.M)}
    # A single typed Workbench owner may dispatch its real page components
    # internally. Read the explicit route union instead of requiring a switch in
    # ContentRouter; a typed union is still auditable and has no catch-all branch.
    if not keys and "DatastreamWorkbenchRoute" in body:
        typed = re.search(r'tab=\{[^}]*?as\s+([^}]+)\}', body, re.S)
        if typed:
            keys = set(re.findall(r'"([a-z0-9-]+)"', typed.group(1)))
        # AND WHEN THE UNION IS A NAME, FOLLOW IT (story 58.6). The cast in
        # `ContentRouter` used to spell the six tabs out, which made this audit
        # the SEVENTH registry of a list that already had six -- and adding the
        # conditional `Cost` tab to every other one left this reading `0/6
        # routed` while the router routed all seven. A named type is not less
        # auditable; it just has to be read where it is declared.
        if not keys:
            union = router.parent / "pages" / "datastreamTabs.ts"
            if union.exists():
                declared = re.search(
                    r"export type Tab\s*=([^;]+);", union.read_text(encoding="utf-8")
                )
                if declared:
                    keys = set(re.findall(r'"([a-z0-9-]+)"', declared.group(1)))
    return keys, bool(re.search(r"^\s*default:", body, re.M))


# --------------------------------------------------------------------------
# Orphan sweep — the inventory, not the diff.
# --------------------------------------------------------------------------


DATA_COLLECTIONS = (
    ("data-overview", "Data Overview"),
    ("datastreams", "Datastreams"),
    ("events", "Events"),
    ("sources", "Sources"),
    ("imports", "Imports"),
    ("connectors", "Connectors"),
)

DATA_WORKBENCHES = {
    "source-account": ("overview", "accounts", "health", "used-by"),
    "import": ("overview", "raw-evidence", "validation", "publication"),
    "event-configuration": ("overview", "source-mapping", "collection", "usage"),
    "connector": ("overview", "capabilities", "coverage", "versions"),
}

DATA_MIGRATED_FILES = (
    "shell/pages/DataOverview.tsx",
    "shell/pages/DataWorkspace.tsx",
    "shell/pages/Sources.tsx",
    "shell/pages/Imports.tsx",
    "BusinessContextPanel.tsx",
    "shell/pages/ConnectorsCatalog.tsx",
    "shell/DataTree.tsx",
    "data/DataObjectWorkbench.tsx",
)

DATA_BANNED_PATTERNS = {
    "/api/datastreams?project_id": "legacy Datastream collection join",
    "/api/connections?project_id": "legacy credential collection join",
    "/api/context-events?project_id": "legacy event observation editor",
    "/api/connectors/available?project_id": "legacy Connector enablement catalog",
    "project_modules.enabled": "Project capability toggle presented as a Connector",
    "application.css": "legacy application stylesheet dependency",
    'from "@mui': "MUI import in migrated Data UI",
    "from '@mui": "MUI import in migrated Data UI",
    'from "@emotion': "Emotion import in migrated Data UI",
    "from '@emotion": "Emotion import in migrated Data UI",
}


def data_surface_contract_findings(
    ui_root: Path | None = None,
    router_path: Path | None = None,
) -> list[str]:
    """Enforce the Story 47.1 Data perimeter instead of trusting screenshots."""
    ui_root = ui_root or UI
    router_path = router_path or ROUTER
    findings: list[str] = []
    navigation = ui_root / "shell" / "navigation.ts"
    if not navigation.exists() or not router_path.exists():
        return ["Data audit cannot read navigation.ts and ContentRouter.tsx"]

    # Le registre est en sept fichiers depuis AD-42 : `navigation.ts` garde le
    # vocabulaire et l assemblage, chaque espace declare ses sections a cote.
    navigation_text = chr(10).join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in [navigation, *sorted((navigation.parent / "navigation").glob("*.ts"))]
        if path.exists()
    )
    # Le chemin passe en argument d abord, puis ses deux tables soeurs -- sans
    # doublon quand c est le routeur par defaut.
    surfaces = list(dict.fromkeys((router_path, *ROUTER_SURFACES)))
    router_text = chr(10).join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in surfaces
        if path.exists()
    )
    for slug, label in DATA_COLLECTIONS:
        # Whitespace-tolerant: a substring match reported the registration as
        # MISSING the first time someone wrapped the call across lines, which is
        # a false finding about a real registration. The gate must measure the
        # registry, not its formatting.
        registered = re.search(
            rf'section\(\s*"{re.escape(slug)}"\s*,\s*"{re.escape(label)}"',
            navigation_text,
        )
        if not registered:
            findings.append(f"missing Data collection registration: {slug}")
        if f'"data/{slug}"' not in router_text:
            findings.append(f"missing Data collection owner route: data/{slug}")

    for object_type, tabs in DATA_WORKBENCHES.items():
        registration = re.search(
            rf'type:\s*"{re.escape(object_type)}"[^\n]+tabs:\s*\[([^\]]+)\]',
            navigation_text,
        )
        if not registration:
            findings.append(f"missing Data workbench registration: {object_type}")
            continue
        registered_tabs = tuple(re.findall(r'"([a-z0-9-]+)"', registration.group(1)))
        if registered_tabs != tabs:
            findings.append(
                f"wrong {object_type} tabs: {', '.join(registered_tabs) or 'none'}"
            )
        if f'type: "{object_type}"' not in router_text:
            findings.append(f"unrouted Data object type: {object_type}")

    if 'import DataObjectWorkbench from "../data/DataObjectWorkbench"' not in router_text:
        findings.append("four Data objects do not share DataObjectWorkbench")

    for relative in DATA_MIGRATED_FILES:
        path = ui_root / relative
        if not path.exists():
            findings.append(f"missing migrated Data owner: ui/admin/src/{relative}")
            continue
        # THE FILE'S CODE, NEVER ITS PROSE. Every pattern below names something
        # the screen must not DO, and a header comment that explains why a screen
        # stopped doing it contains the same words -- `DataTree.tsx:6` says
        # "`application.css` is not loaded" and was reported, for a month, as
        # loading it. `_code_only` is the same masker the silent-fallback sweep
        # has used since Story 48.3; it simply was never applied here.
        source = _code_only(path.read_text(encoding="utf-8", errors="replace"), path)
        for pattern, reason in DATA_BANNED_PATTERNS.items():
            if pattern in source:
                findings.append(f"ui/admin/src/{relative}: {reason}")
        if re.search(r'import\s+["\'][^"\']+\.css["\']', source):
            findings.append(f"ui/admin/src/{relative}: dedicated legacy stylesheet import")
        if re.search(r'\b(?:if|switch)\s*\([^\n]*(?:provider|connector_id)', source, re.I):
            findings.append(f"ui/admin/src/{relative}: source-specific client classification")
    return findings

def _is_test(path: Path) -> bool:
    return "__tests__" in path.parts or path.name.endswith((".test.tsx", ".test.ts"))


def _strip_reexports(text: str) -> str:
    """A barrel `export { X } from "./X"` is not a mount."""
    return re.sub(
        r"^\s*export\s*\{[^}]*\}\s*from\s*[\"'][^\"']+[\"'];?\s*$", "", text, flags=re.M
    )


#: One import, static or lazy. `lazy(() => import("./X"))` is how most screens
#: are mounted here, so a scanner that reads only `from "…"` calls every routed
#: page an orphan.
_IMPORT_SPEC = re.compile(r'(?:\bfrom\s*|\bimport\s*\(\s*)["\']([^"\']+)["\']')

#: A barrel line: `export { A, B } from "./A"`. Read here, unlike
#: `_strip_reexports`, which removes it — the two need it for opposite reasons.
_REEXPORT = re.compile(r'^\s*export\s*(?:type\s*)?\{([^}]*)\}\s*from\s*["\']([^"\']+)["\'];?\s*$', re.M)

#: The named bindings of one import statement, `import { A, B as C } from "…"`.
_IMPORT_NAMES = re.compile(r'\bimport\s*(?:type\s*)?\{([^}]*)\}\s*from\s*["\']([^"\']+)["\']')


def _resolve(importer: Path, spec: str) -> list[Path]:
    """A relative specifier as the absolute, extension-less paths it can mean.

    Two, not one: `from "../ui"` means `../ui/index` when `../ui` is a
    directory, and that single missing case is what kept `ui/Evidence.tsx` on
    the orphan list — every screen reaches it through the `ui` barrel and not
    one of them spells `index`.
    """
    if not spec.startswith("."):
        return []
    base = (importer.parent / spec).resolve()
    return [base.with_suffix(""), base / "index"]


def orphan_components() -> list[str]:
    """Components exported but reached by no production file.

    REACHED means IMPORTED, and it took a false negative to make that the rule.

    The previous version searched every production file for the component's
    NAME. Three things rescue a name that nothing imports: a mention in a
    comment, an unrelated local declaration of the same identifier, and a
    sibling in the same dead branch. All three occurred at once in
    `ui/admin/src/orgs/`: `OrgDetailPanel` appears in a comment in `types.ts`,
    `OrgSettings.tsx` declares its own unrelated `interface OrgMember`, and the
    two panels below it are imported by `OrgDetailPanel` itself. The result was
    a 1750-line branch that no route reaches, reported as mounted, for months.

    It also silently falsified a claim written in the product code:
    `ContentRouter.tsx:19` states that this script "now reports them as mounted
    nowhere -- that report IS the inventory" about `ReportsPanel`,
    `NotebooksPanel` and `RenderGalleryPage`. It did not report them. A story
    was closed on an inventory that did not exist.

    So a file is reached when a production file imports it BY PATH — statically
    or through `lazy(() => import(…))` — or when a barrel re-exports it and a
    production file imports one of those names FROM that barrel. The second
    clause is not a loophole, it is the majority case here: `ui/Evidence.tsx` is
    reached only as `EvidenceRows` through `ui/index.ts`, and dropping it would
    trade this false negative for a false positive.

    Measured on the day of the change: 11 orphans -> 22, and none of the
    original 11 lost.
    """
    if not UI.exists():
        return []
    source = {p: p.read_text(encoding="utf-8", errors="replace") for p in UI.rglob("*.ts*")}
    production = {p: t for p, t in source.items() if not _is_test(p)}

    # Every module a production file imports, by resolved path. `_strip_reexports`
    # is applied first so a barrel does NOT count as importing what it merely
    # forwards — otherwise every barrelled file is mounted by definition.
    direct: set[Path] = set()
    for path, text in production.items():
        for spec in _IMPORT_SPEC.findall(_strip_reexports(text)):
            direct.update(_resolve(path, spec))

    # barrel path -> {exported name -> the module it comes from}
    barrels: dict[Path, dict[str, list[Path]]] = {}
    for path, text in production.items():
        for names, spec in _REEXPORT.findall(text):
            targets = _resolve(path, spec)
            if not targets:
                continue
            table = barrels.setdefault(path.resolve().with_suffix(""), {})
            for raw in names.split(","):
                name = raw.split(" as ")[0].strip()
                if name:
                    table[name] = targets

    # Which (barrel, name) pairs a production file actually imports.
    through_barrel: set[Path] = set()

    # `export { default } from "./X"` forwards X AS the barrel's own default, so
    # no consumer ever writes the name `X` — `shell/AuthGate.tsx` does exactly
    # this and the named-import clause below cannot see it. Whoever imports the
    # barrel reaches X, and that is the whole rule.
    for barrel, table in barrels.items():
        if "default" in table and barrel in direct:
            through_barrel.update(table["default"])
    for path, text in production.items():
        for names, spec in _IMPORT_NAMES.findall(text):
            table: dict[str, list[Path]] = {}
            for candidate in _resolve(path, spec):
                table.update(barrels.get(candidate, {}))
            if not table:
                continue
            for raw in names.split(","):
                name = raw.split(" as ")[0].strip()
                if name in table:
                    through_barrel.update(table[name])

    orphans = []
    for path in sorted(UI.rglob("*.tsx")):
        if _is_test(path) or path.stem in ("main", "App"):
            continue
        if not re.search(r"export (default |function |const )", source[path]):
            continue
        key = path.resolve().with_suffix("")
        if key not in direct and key not in through_barrel:
            orphans.append(path.relative_to(ROOT).as_posix())
    return orphans


# --------------------------------------------------------------------------
# Server sweep — the same defect class, on the other side of the wire.
# --------------------------------------------------------------------------


#: Route declarations live in 32 of the 48 `server/core/*_api.py` modules, not in
#: `admin_api.py` alone. `admin_api` only SPREADS them (`*answerable_topic_routes`),
#: so a scanner that reads that one file is blind to most of the surface.
#:
#: Measured on 2026-08-01, by enumerating the mounted Starlette router rather than
#: a regex: 393 `/api/` routes are mounted, this function saw 137 of them, and 256
#: were invisible. Epic 52's ten routes were all in the invisible set -- which is
#: exactly why a review found two of them with no caller at all while this gate
#: was green, and why four stories cited `--gate -> 0` as proof of a door.
_API_MODULE_GLOB = "server/core/*_api.py"


def _declared_api_routes() -> dict[str, str]:
    """Every `/api/` path declared anywhere under `server/core/*_api.py`.

    Reads the modules that DECLARE routes instead of the one that mounts them.
    Two shapes are matched: a literal path, and an f-string built on a module
    constant (`f"{_BASE}/{{topic_key}}/queries"`), which is how most of the newer
    modules are written and which no literal-only regex ever found.
    """
    declared: dict[str, str] = {}
    for module in sorted(ROOT.glob(_API_MODULE_GLOB)):
        text = module.read_text(encoding="utf-8", errors="replace")
        # The module's own path constants, so an f-string route can be expanded.
        constants = {
            name: value
            for name, value in re.findall(
                r'^(_[A-Z][A-Z0-9_]*)\s*=\s*["\'](/api/[^"\']*)["\']', text, re.MULTILINE
            )
        }
        for method, path in re.findall(
            r'@(?:app\.)?route\(\s*["\']([^"\']+)["\']|'
            r'add_route\(\s*["\']([^"\']+)["\']',
            text,
        ):
            route = method or path
            if route.startswith("/api/"):
                declared[route] = module.name
        for route in re.findall(r'Route\(\s*f?["\']([^"\']*/api/[^"\']+)["\']', text):
            declared[route] = module.name
        for prefix_name, tail in re.findall(
            r'Route\(\s*f["\']\{(_[A-Z][A-Z0-9_]*)\}([^"\']*)["\']', text
        ):
            base = constants.get(prefix_name)
            if base:
                # `f"{_BASE}/{{topic_key}}"` keeps its doubled braces after the
                # regex; the report should read like a path, not like a template.
                declared[(base + tail).replace("{{", "{").replace("}}", "}")] = module.name
    return declared


def unreachable_routes() -> list[str]:
    """API routes no production UI file ever calls.

    The mirror image of an orphan component: a capability that exists and has
    no door. Looking only at the UI half of this is how an audit ends up
    reporting that everything on screen is fine while nothing is plumbed.
    """
    if not UI.exists():
        return []

    declared = _declared_api_routes()
    if not declared:
        return []

    callers = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in UI.rglob("*.ts*")
        if not _is_test(p)
    )
    unreached = []
    for route in sorted(declared):
        # Compare on the literal prefix before the first path parameter: the UI
        # builds these with template strings, so the tail is never literal.
        stem = route.split("{")[0].rstrip("/")
        if stem and stem not in callers:
            unreached.append(route)
    return unreached


def unexercised_modules() -> list[str]:
    """Connector modules with no fixture proving a pull was ever decoded.

    Eleven Datastreams on one source is not coverage of thirty-seven sources.
    A module whose fixtures directory holds no recorded provider response has
    never had its extraction path executed against real shapes.
    """
    modules = ROOT / "server" / "modules"
    if not modules.exists():
        return []
    unexercised = []
    for module in sorted(p for p in modules.iterdir() if p.is_dir()):
        if module.name.startswith("_"):
            continue
        fixtures = list(module.rglob("fixtures/*.json")) + list(module.rglob("fixtures/*.csv"))
        if not fixtures:
            unexercised.append(module.name)
    return unexercised


def _code_only(text: str, path: Path) -> str:
    """Blank out comments and docstrings, keeping every line number intact.

    A silent fallback is something the code DOES. The detector matched raw file
    text, so a comment explaining that `COALESCE(fx_rate, 1.0)` was removed counted
    as the defect it describes -- and the Story 48.3 FX-at-read repair, whose whole
    job is deleting that fallback, tripped the gate five times for documenting it.
    Prose about a defect is not the defect.

    What stays visible, deliberately: ordinary and triple-quoted STRING literals in
    Python. SQL is routinely written as a triple-quoted string, and
    `cur.execute(<triple-quoted>SELECT COALESCE(fx_rate, 1.0)...)` is exactly the dangerous
    case. Only genuine DOCSTRINGS are masked, identified through the AST rather than
    by quoting style, so the distinction is structural instead of guessed.

    AND THE SAME DEFECT, ON THE OTHER SIDE OF THE WIRE (2026-09-02). TSX had no
    branch here at all, so every scan of a component read its comments as code.
    Measured: `data_surface_contract_findings` reported
    `ui/admin/src/shell/DataTree.tsx: legacy application stylesheet dependency`
    against a header comment at `DataTree.tsx:6` that says the opposite --
    "`application.css` is not loaded" -- and the file imports no sheet at all.
    That finding was one of the repository's two structural findings for a month.

    TSX IS SCANNED, NOT REGEXED, and the difference is load-bearing: `//` also
    opens the middle of every URL, so blanking from it with a regex would eat the
    rest of the line -- including the `"/api/datastreams?project_id="` a banned
    pattern is looking for, turning one false positive into a false negative. The
    walk tracks string and template state, so a `//` inside a literal is code and
    a `//` outside one is prose. Line numbers are preserved, as everywhere here.
    """
    masked = list(text)

    def blank(start: int, end: int) -> None:
        for index in range(start, min(end, len(masked))):
            if masked[index] != "\n":
                masked[index] = " "

    if path.suffix == ".py":
        for match in re.finditer(r"#[^\n]*", text):
            blank(match.start(), match.end())
        try:
            import ast  # noqa: PLC0415

            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            lines = [0]
            for line in text.splitlines(keepends=True):
                lines.append(lines[-1] + len(line))
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if not isinstance(body, list) or not body:
                    continue
                first = body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                    and first.end_lineno is not None
                ):
                    blank(lines[first.lineno - 1], lines[first.end_lineno])
    elif path.suffix in (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts"):
        index, length, quote = 0, len(text), ""
        while index < length:
            char = text[index]
            if quote:
                if char == "\\":
                    index += 2
                    continue
                if char == quote:
                    quote = ""
                index += 1
                continue
            if char in "\"'`":
                quote = char
                index += 1
                continue
            following = text[index + 1] if index + 1 < length else ""
            if char == "/" and following == "/":
                end = text.find("\n", index)
                end = length if end == -1 else end
                blank(index, end)
                index = end
                continue
            if char == "/" and following == "*":
                end = text.find("*/", index + 2)
                end = length if end == -1 else end + 2
                blank(index, end)
                index = end
                continue
            index += 1
    else:
        # SQL and its Jinja: `-- to end of line`, `/* block */`, `{# jinja #}`.
        for pattern in (r"--[^\n]*", r"/\*.*?\*/", r"\{#-?.*?-?#\}"):
            for match in re.finditer(pattern, text, re.S):
                blank(match.start(), match.end())

    return "".join(masked)


def silent_fallbacks() -> list[str]:
    """Defaults that turn a missing value into a plausible one.

    This is the failure mode that clicking can never reveal, because the screen
    renders and the number looks like a number. `COALESCE(fx_rate, 1.0)`
    converts at parity and says nothing.
    """
    findings = []
    patterns = [
        (r"COALESCE\(\s*(fx_rate|rate|exchange_rate)\s*,\s*1(\.0+)?\s*\)",
         "currency conversion falls back to parity"),
    ]
    for base in (ROOT / "server", ROOT / "dbt"):
        if not base.exists():
            continue
        for path in list(base.rglob("*.sql")) + list(base.rglob("*.py")):
            # `dbt/target/` holds compiled copies of the same models; counting
            # them would inflate the finding with its own artefacts.
            if "target" in path.parts or "__pycache__" in path.parts:
                continue
            text = _code_only(path.read_text(encoding="utf-8", errors="replace"), path)
            for pattern, why in patterns:
                for match in re.finditer(pattern, text, re.I):
                    line = text[: match.start()].count("\n") + 1
                    findings.append(
                        f"{path.relative_to(ROOT).as_posix()}:{line} — {why}"
                    )
    return findings


# --------------------------------------------------------------------------
# Standing conventions — stated a dozen times, enforced by nothing until now.
# --------------------------------------------------------------------------

FRENCH = re.compile(
    r"\b(?:Extraits?|R[ée]glages?|Param[èe]tres?|Donn[ée]es?|Cr[ée]er|Enregistrer|"
    r"Annuler|Fermer|Ajouter|Supprimer|Modifier|Rechercher|Suivant|Pr[ée]c[ée]dent|"
    r"Aper[çc]u|Aucun[e]?|Charg\w+|[ÀÉÈÊÎÔÛÇàéèêîôûç]\w*)\b"
)


def _is_character_table(value: str) -> bool:
    """True when *value* is an alphabet, not a sentence.

    `"àáâãäåçèéêëìíîïñòóôõöùúûüýÿ"` is the left-hand side of the `str.maketrans`
    at `server/core/context_search.py:152` — the accent folding a French keyboard
    makes necessary, with `"aaaaaaceeeeiiiinooooouuuuyy"` facing it. It reached
    the report as "1 non-English string on screen", and it is not a string on any
    screen: it is DATA the search compares with.

    The test is structural, not a list of files: copy is made of words, and a word
    carries an ASCII letter or a space next to it. A run of accented characters
    with neither is a character class, wherever it is written.
    """
    return not any(character.isspace() for character in value) and not re.search(
        r"[A-Za-z]", value
    )


def french_in_ui() -> list[str]:
    """User-visible copy that is not English.

    Only JSX text nodes and the props that reach the screen. Identifiers,
    comments and directory names are code, not copy, and are left alone.
    """
    findings = []
    for path in sorted(UI.rglob("*.tsx")):
        if _is_test(path):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("//", "*", "/*")):
                continue
            visible = re.findall(
                r'(?:label|title|placeholder|aria-label|heading|caption)\s*=\s*["\']([^"\']+)["\']'
                r'|>\s*([A-ZÀ-Ü][^<>{}]{3,})\s*<',
                line,
            )
            for groups in visible:
                text = next((g for g in groups if g), "")
                if FRENCH.search(text) and not _is_character_table(text):
                    findings.append(
                        f"{path.relative_to(ROOT).as_posix()}:{number} — {text.strip()[:60]}"
                    )
    return findings


def french_in_server() -> list[str]:
    """User-visible copy emitted by the SERVER, which the UI sweep cannot see.

    `french_in_ui` scans `*.tsx` and nothing else -- its name says so -- and it
    reported six findings while `narrative.py` and `cards.py` alone emit hundreds
    of French strings that land on a screen: card status labels, the narrative
    text of a rendered report, validation messages returned in a 422 body. Half
    the surface was never measured, so "6 non-English strings" read as almost
    done when the real figure is two orders of magnitude larger.

    Comments and docstrings are masked with `_code_only`, so prose ABOUT the copy
    is not counted as copy -- the same distinction the silent-fallback sweep
    needed. What is left is string literals, which is where product copy lives.

    ONE FILE IS EXCLUDED, AND IT IS THE POINT OF THE RULE, NOT A HOLE IN IT.
    `server/core/narrative_phrases.py` is the phrase CATALOGUE ratified on
    2026-08-25 (`docs/product-architecture/analyze-and-test.md`, « how a
    narrative sentence is rendered »): a récit is rendered in the reader's
    language, so its words leave the logic and live in ONE keyed catalogue, one
    column per language. Counting that catalogue as server copy would report the
    ratified target as the defect, and would leave the words nowhere to go.

    The exclusion is not a blind spot: `scripts/check_narrative_no_raw.py`
    rule D reddens on any prose literal that goes BACK into a narrative
    producer, and `server/tests/core/test_narrative_phrases.py` holds the
    catalogue's own contract (English keys, one declared default language, no
    column a request cannot ask for).
    """
    findings: list[str] = []
    core = ROOT / "server" / "core"
    if not core.exists():
        return []
    literal = re.compile(r'"([^"\n]{3,})"' + "|" + r"'([^'\n]{3,})'")
    for path in sorted(core.rglob("*.py")):
        if "test" in path.name or path.name == "narrative_phrases.py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for number, line in enumerate(_code_only(text, path).splitlines(), 1):
            for groups in literal.findall(line):
                value = next((g for g in groups if g), "")
                if FRENCH.search(value) and not _is_character_table(value):
                    findings.append(
                        f"{path.relative_to(ROOT).as_posix()}:{number} — {value.strip()[:60]}"
                    )
    return findings


CSS_TARGET_LINES = 1400


def css_line_count(sheets: list[Path] | None = None, exempt: set[str] | None = None) -> int:
    sheets = sheets if sheets is not None else sorted(UI.rglob("*.css"))
    if exempt:
        sheets = [p for p in sheets if p.relative_to(ROOT).as_posix() not in exempt]
    return sum(p.read_text(encoding="utf-8", errors="replace").count("\n") for p in sheets)


def css_pending_exemptions(debt: dict) -> set[str]:
    """Sheets a `pending` entry attributes to another session's in-flight work.

    The hook's own answer 2 says to record another session's finding in `pending`
    so it stops blocking and stays loud. That worked for every finding EXCEPT the
    CSS ratchet, which was appended after the `pending` filter and so could not be
    answered at all -- leaving only answer 3, `--baseline`, which accepts every
    current finding as permanent debt including the neighbour's unfinished sheet.
    Measured on 2026-08-07: `ui/admin/src/connaissances/context-hub.css`, 600
    lines, UNTRACKED, held the gate shut against a four-file repair that added
    nine lines of comment.

    A path is named, never a line count: the count is read from the file, so an
    entry cannot drift into an open-ended allowance as the neighbour keeps
    writing. The sheet still appears in the report, and the day its session lands
    the entry must go -- an exemption for a COMMITTED file is standing debt, and
    standing debt goes through `--baseline` with everything else.
    """
    exempt: set[str] = set()
    for entry in debt.get("pending", []):
        exempt.update(entry.get("css_exempt", []))
    return exempt


def css_volume_finding(sheets: list[Path]) -> str:
    """A measurement, not a defect — but one that must only ever go down.

    The gate compares it to the recorded figure and blocks on an increase, so
    every migration has to lower the total. Reporting it as a plain finding
    would block forever, since the number is never zero.
    """
    return (
        f"{css_line_count(sheets)} CSS lines across {len(sheets)} stylesheets "
        f"(target ~{CSS_TARGET_LINES})"
    )


#: COLOURS A PRIMITIVE CANNOT CARRY, each named with the reason it cannot.
#:
#: The sweep below counted 17 hardcoded colours and NOT ONE was a violation: 3
#: were a comment (`ui/NavItem.tsx`, a token correspondence table, now masked by
#: `_code_only`), 8 painted two boot screens that render before any theme exists,
#: and 6 are the reference values a contrast ratio is computed against. A finding
#: whose every member is legitimate teaches people to stop reading the report.
#:
#: THE EXEMPTION NAMES THE COLOURS, NOT THE FILE, and that is the whole design.
#: Exempting a path would make every future colour in it invisible -- a silent
#: baseline wearing a reason. Listing the literals means a NEW colour in an
#: exempt file still fires, and `self_check` reports an exempt colour the file no
#: longer carries, so an exemption cannot outlive its cause.
COLOUR_EXEMPTIONS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "shell/AuthGate.tsx",
        ("#0f1115", "#f5f5f5", "#ff6b6b"),
        "the sign-in screen paints itself before the shell, its theme sheet and "
        "its org branding exist; a token here would resolve to nothing",
    ),
    (
        "shell/BrowserAuthGate.tsx",
        ("#0f1115", "#f5f5f5", "#ff6b6b", "#ffcc66"),
        "the same boot screen on the browser identity path, for the same reason",
    ),
    (
        "shell/orgTheme.tsx",
        ("#111111", "#FAFAFA", "#FFFFFF", "#F8F9FA"),
        "the inks and grounds an org's OWN branding colour is measured against "
        "by `contrastRatio` -- reference values of a measurement, not paint a "
        "screen applies",
    ),
)


def _exempt_colours(relative: str) -> set[str]:
    """The colours *relative* is allowed to spell out, lowercased for comparison."""
    return {
        colour.lower()
        for path, colours, _reason in COLOUR_EXEMPTIONS
        if path == relative
        for colour in colours
    }


def stylesheet_drift() -> list[str]:
    """Hardcoded values and duplicate selectors: the vocabulary reinvented.

    Scans every stylesheet, never one hardcoded path. The first version read
    `app.css` alone; when that file was replaced by a 27-line layered entry
    point, the check reported zero duplicates across 61 untouched stylesheets.
    A check pinned to one path goes green the moment the world moves.
    """
    findings = []
    sheets = sorted(p for p in UI.rglob("*.css"))

    # The target is a closed vocabulary at roughly 1,400 lines. Deduplication
    # alone bottoms out near 4,000 because 1,117 visual behaviours are written
    # exactly once each — the per-screen prefix convention lets every screen
    # invent its own. Tracking the total is what makes a migration provable and
    # keeps it from creeping back up unnoticed.
    findings.append(css_volume_finding(sheets))

    # An entry point nobody imports, or a layer file with no content, is the
    # same orphan defect as an unmounted component — in the visual layer.
    importers = "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in UI.rglob("*.ts*") if not _is_test(p)
    )
    empty = [p.relative_to(ROOT).as_posix() for p in sheets if p.stat().st_size == 0]
    if empty:
        findings.append(
            f"{len(empty)} empty stylesheet(s) declared but never filled: "
            f"{', '.join(s.rsplit('/', 1)[-1] for s in empty[:6])}"
        )
    for sheet in sheets:
        if sheet.stat().st_size == 0:
            continue
        # A sheet may be reached from TSX, or @import-ed from another sheet.
        # Match the bare filename: the path prefix differs at every call site.
        reachable = sheet.name in importers or any(
            sheet.name in other.read_text(encoding="utf-8", errors="replace")
            for other in sheets if other != sheet
        )
        if not reachable:
            findings.append(f"stylesheet imported by nothing: {sheet.relative_to(ROOT).as_posix()}")

    # Same reading as the ratified command in CLAUDE.md:
    #   grep -oE "^\.[a-z0-9-]+" <sheet> | sort | uniq -d
    # Across 61 sheets the damaging case is no longer a class repeated inside
    # one file — it is the same class owned by several files at once, where
    # nobody can tell which definition is live.
    owners: dict[str, set[str]] = {}
    for css in sheets:
        if css.stat().st_size == 0:
            continue
        for selector in set(re.findall(r"^\.([a-z0-9-]+)", css.read_text(encoding="utf-8"), re.M)):
            owners.setdefault(selector, set()).add(css.name)
    shared = {s: files for s, files in owners.items() if len(files) > 1}
    if shared:
        # The recorded finding must be stable while the work is in progress.
        # Embedding live counts made the text change on every migration; naming
        # the worst FIVE was the same mistake one step removed, because deleting
        # any stylesheet reshuffles which five they are -- so a CSS deletion
        # manufactured a "new" finding for debt that was already accepted, and
        # blocked the gate on someone else's refactor.
        #
        # One name is the identity. It only moves when the actual worst offender
        # moves, which is a real shift worth re-recording. The rest is detail and
        # belongs on the report line, not in the string the gate matches.
        worst_selector = sorted(shared.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][0]
        findings.append(
            f"classes defined in several stylesheets at once, worst: .{worst_selector}"
        )
    hexes = 0
    for path in UI.rglob("*.tsx"):
        if _is_test(path):
            continue
        # The code, never the prose: `ui/NavItem.tsx:20-29` writes the token
        # correspondence table it REPLACED, in a block comment, hex by hex.
        source = _code_only(path.read_text(encoding="utf-8", errors="replace"), path)
        allowed = _exempt_colours(path.relative_to(UI).as_posix())
        hexes += sum(
            1 for colour in re.findall(r"#[0-9A-Fa-f]{6}\b", source)
            if colour.lower() not in allowed
        )
    if hexes:
        findings.append(f"{hexes} hardcoded colour(s) in TSX instead of a primitive")
    return findings


# A class whose name ends in one of these is a component, whatever prefix a
# screen puts in front of it. `.dso-btn-primary` is a button; `.imports-chip` is
# a chip; `.kg-drawer` is a drawer.
VOCABULARY = (
    "btn", "button", "chip", "badge", "pill", "panel", "card", "dialog", "modal",
    "drawer", "sheet", "alert", "banner", "toast", "tooltip", "popover", "menu",
    "dropdown", "tab", "tabs", "table", "input", "field", "select", "checkbox",
    "radio", "switch", "toggle", "avatar", "breadcrumb", "spinner", "progress",
    "empty", "header", "footer", "metric", "kpi", "stepper", "step",
)


def reimplemented_components() -> list[str]:
    """Screens that re-declared a component under their own prefix.

    This is the mechanism behind the whole migration, caught at its source. The
    Datastream wizard wrote `dso-btn`, `dso-btn-ghost`, `dso-btn-primary`,
    `dso-btn-small` — a third button scale in one project — because a prefixed
    class cannot be reused, so the next screen copies it, and copies it wrong.

    Re-reading before writing was already a written rule and did not prevent it.
    A rule that fires on its own does.
    """
    # The base sheet holds the vocabulary itself: `.primary-button` and
    # `.page-header` are the canonical classes, not a screen's private copy.
    # Counting them as offences put the reference among the faults and inflated
    # the finding by a third.
    BASE_SHEETS = {"application.css", "theme.css", "shell.css"}

    found: dict[str, list[str]] = {}
    for sheet in sorted(UI.rglob("*.css")):
        if sheet.stat().st_size == 0 or sheet.name in BASE_SHEETS:
            continue
        for selector in set(re.findall(r"^\.([a-z][a-z0-9]*(?:-[a-z0-9]+)+)", sheet.read_text(encoding="utf-8"), re.M)):
            parts = selector.split("-")
            # Skip the first segment: it is the screen prefix. What follows is
            # the concept the screen decided to own.
            for index in range(1, len(parts)):
                if parts[index] in VOCABULARY:
                    found.setdefault(f"{parts[0]}-*-{parts[index]}", []).append(selector)
                    break
    return [
        f"`{concept}` re-declared locally: {', '.join(sorted(names)[:4])}"
        + (f" (+{len(names) - 4})" if len(names) > 4 else "")
        for concept, names in sorted(found.items(), key=lambda kv: -len(kv[1]))
    ]


def legacy_leaks() -> list[str]:
    """Migrated screens still leaning on the old vocabulary.

    A screen counts as migrated once it imports the component barrel. From that
    point it may use `theme.css` and `styles/console.css` and nothing else — no
    class that exists only in `shell/application.css` or the 60 legacy sheets.

    Without this, "migrated" means only that a screen's own stylesheet was
    deleted, while the screen keeps reaching into the shared legacy sheet that
    every page still loads. That is invisible: the page renders correctly and
    the line count falls, so the leak reads as progress.
    """
    if not UI.exists():
        return []

    target = {"theme.css", "console.css"}
    legacy: set[str] = set()
    for sheet in UI.rglob("*.css"):
        if sheet.name in target or sheet.stat().st_size == 0:
            continue
        legacy.update(re.findall(r"^\.([a-z][a-z0-9-]*)", sheet.read_text(encoding="utf-8"), re.M))

    leaks = []
    for path in sorted(UI.rglob("*.tsx")):
        if _is_test(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not re.search(r'from "\.\.?/*(?:\.\./)*ui"', text):
            continue  # not migrated yet; the legacy vocabulary is still its own
        used = {c for group in re.findall(r'className=[{"`\']([^"`\'}]+)', text)
                for c in group.split() if not c.startswith(("[", "hover:", "focus"))}
        # Tailwind utilities share no names with the legacy classes, so anything
        # matching a legacy selector is a genuine reach into the old sheet.
        offenders = sorted(used & legacy)
        # Arbitrary-variant selectors hide the same reach inside brackets.
        offenders += sorted({m for m in re.findall(r"\[&_\.([a-z][a-z0-9-]*)\]", text) if m in legacy})
        if offenders:
            leaks.append(
                f"{path.relative_to(ROOT).as_posix()} still uses legacy classes: "
                f"{', '.join(offenders[:5])}"
            )
    return leaks


def duplicate_components() -> list[str]:
    """The same component name defined in more than one file.

    Duplicating is cheaper than reading, once. It is more expensive than
    reading, every time after that — and it hides which copy is live.
    """
    seen: dict[str, list[str]] = {}
    for path in UI.rglob("*.tsx"):
        if _is_test(path):
            continue
        seen.setdefault(path.stem, []).append(path.relative_to(ROOT).as_posix())
    return [
        f"{name} defined in {len(paths)} files: {', '.join(paths)}"
        for name, paths in sorted(seen.items())
        if len(paths) > 1
    ]


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------


@dataclass
class Result:
    surface: str
    structural: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    open_criteria: list[str] = field(default_factory=list)
    total_criteria: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if not (self.structural or self.orphans or self.open_criteria):
            return "DONE"
        # Built-but-unmounted is wiring, not construction: say so, because the
        # cost of the two is not remotely the same and the person is deciding.
        if self.orphans and not self.structural:
            return f"WIRE ({len(self.orphans)})"
        if self.structural:
            return "REBUILD"
        return f"REPAIR ({len(self.open_criteria)})"


def evaluate(surface: Surface, ledger: dict, orphans: list[str]) -> Result:
    criteria = incomplete_if(surface.doc)
    result = Result(surface=surface.key, total_criteria=len(criteria))

    recorded = ledger.get(surface.key, {})
    for index, criterion in enumerate(criteria):
        entry = recorded.get(str(index))
        if not entry or entry.get("verdict") != "false" or not entry.get("evidence"):
            result.open_criteria.append(criterion)

    result.orphans = [
        o for o in orphans if any(f"/src/{d}/" in o for d in surface.owns)
    ]

    if surface.key == "data":
        result.structural.extend(data_surface_contract_findings())

    if surface.key == "analyze-and-test":
        # Story 65.11: inventory validity blocks the existing Analyze audit,
        # while an honestly blocked retirement remains a visible note.  The
        # dedicated checker owns the census; duplicating its rules here would
        # let one list drift green while the other missed a new legacy path.
        try:
            from check_legacy_analytics_migration import (  # noqa: PLC0415
                inventory_summary,
                load_inventory,
                validate_inventory,
            )
        except ModuleNotFoundError:
            from scripts.check_legacy_analytics_migration import (  # type: ignore[no-redef]  # noqa: PLC0415
                inventory_summary,
                load_inventory,
                validate_inventory,
            )
        inventory_errors = validate_inventory()
        result.structural.extend(
            f"legacy analytics inventory: {error}" for error in inventory_errors
        )
        try:
            summary = inventory_summary(load_inventory())
        except (OSError, ValueError) as exc:
            result.structural.append(f"legacy analytics inventory summary unavailable: {exc}")
        else:
            # The counted line stays VISIBLE and the blocking finding stays
            # STABLE -- see `InventorySummary.stable_line()`. Announcing the
            # debt by a count made every census movement a new finding, and
            # each one cost an exemption in `known-debt.json`.
            result.notes.append(summary.line())
            if not (summary.retirement_ready and not inventory_errors):
                result.structural.append(summary.stable_line())

    if surface.tab_table_header and surface.router:
        want = contracted_tabs(surface.doc, surface.tab_table_header)
        surfaces = list(dict.fromkeys((surface.router, *ROUTER_SURFACES)))
        have, catch_all = routed_tabs_across(surfaces, surface.router_object or "")
        missing = [t for t in want if t.lower().replace(" ", "-") not in have]
        if missing:
            result.structural.append(
                f"{len(missing)}/{len(want)} contracted tabs "
                f"{'swallowed by the catch-all' if catch_all else 'unrouted'}: "
                f"{', '.join(missing)}  "
                f"[{', '.join(p.relative_to(ROOT).as_posix() for p in surfaces)}]"
            )
        if catch_all:
            result.structural.append(
                f"`default:` branch in {surface.router.relative_to(ROOT).as_posix()} — "
                "an unknown tab renders another surface instead of failing visibly"
            )
    return result


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------


@dataclass
class Plumbing:
    """Findings that belong to no screen. Half the product lives here."""
    routes: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)
    french: list[str] = field(default_factory=list)
    styling: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    # Standing debt with a direction: reported as a count under a ratchet, not
    # as 151 individually accepted lines that would scroll past every turn.
    reimplemented: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(x) for x in (self.routes, self.modules, self.fallbacks,
                                    self.french, self.styling, self.duplicates,
                                    self.reimplemented))


def self_check() -> list[str]:
    """The audit's own configuration, audited.

    A surface pointing at a directory that does not exist owns nothing and
    reports a clean sheet forever. That is the same silent-success failure this
    tool exists to catch, so it is checked here rather than trusted.
    """
    findings = []
    for surface in SURFACES:
        if not surface.doc.exists():
            findings.append(f"audit config: {surface.key} names a missing document {surface.doc.name}")
        for directory in surface.owns:
            if not (UI / directory).is_dir():
                findings.append(
                    f"audit config: {surface.key} claims ui/admin/src/{directory}/, which does not exist"
                )
        if surface.router and not surface.router.exists():
            findings.append(f"audit config: {surface.key} names a missing router")
    # AN EXEMPTION MUST DIE WITH ITS CAUSE. `COLOUR_EXEMPTIONS` names literals
    # rather than paths so a new colour still fires; this is the other half —
    # a colour the file no longer spells is an allowance nobody is watching, and
    # it would silently cover the day someone writes that exact value back.
    for relative, colours, _reason in COLOUR_EXEMPTIONS:
        path = UI / relative
        if not path.exists():
            findings.append(f"audit config: colour exemption names a missing {relative}")
            continue
        present = {c.lower() for c in re.findall(
            r"#[0-9A-Fa-f]{6}\b",
            _code_only(path.read_text(encoding="utf-8", errors="replace"), path),
        )}
        stale = sorted(c for c in colours if c.lower() not in present)
        if stale:
            findings.append(
                f"audit config: {relative} no longer carries exempt colour(s) "
                f"{', '.join(stale)} — remove the exemption"
            )
    return findings


def sweep_plumbing() -> Plumbing:
    return Plumbing(
        routes=unreachable_routes(),
        modules=unexercised_modules(),
        fallbacks=silent_fallbacks(),
        # Both halves. The UI sweep alone reported six while the server emitted
        # a hundred more onto the same screens, so "6 non-English strings" read
        # as nearly done against two orders of magnitude.
        french=french_in_ui() + french_in_server(),
        styling=stylesheet_drift(),
        duplicates=duplicate_components() + self_check() + legacy_leaks(),
        reimplemented=reimplemented_components(),
    )


def print_plumbing(pipes: Plumbing, limit: int | None = 12) -> None:
    if not pipes.total:
        return
    print("\n  Plumbing — no screen shows any of this:")
    if pipes.fallbacks:
        print(f"    {len(pipes.fallbacks)} silent fallback(s) — wrong numbers, no error:")
        for finding in pipes.fallbacks:
            print(f"      {finding}")
    if pipes.routes:
        shown = pipes.routes if limit is None else pipes.routes[:limit]
        print(f"    {len(pipes.routes)} API route(s) the UI never calls:")
        for route in shown:
            print(f"      {route}")
        if limit is not None and len(pipes.routes) > limit:
            print(f"      … {len(pipes.routes) - limit} more")
    if pipes.modules:
        print(f"    {len(pipes.modules)} connector module(s) with no recorded response fixture:")
        print(f"      {', '.join(pipes.modules)}")
    if pipes.french:
        shown = pipes.french if limit is None else pipes.french[:limit]
        print(f"    {len(pipes.french)} non-English string(s) on screen:")
        for finding in shown:
            print(f"      {finding}")
        if limit is not None and len(pipes.french) > limit:
            print(f"      ... {len(pipes.french) - limit} more")
    for finding in pipes.styling:
        print(f"    visual vocabulary: {finding}")
    for finding in pipes.duplicates:
        print(f"    duplicate: {finding}")
    if pipes.reimplemented:
        print(f"    {len(pipes.reimplemented)} vocabulary concepts re-declared under a screen "
              f"prefix (must only ever fall):")
        for finding in pipes.reimplemented[: (8 if limit else len(pipes.reimplemented))]:
            print(f"      {finding}")
        if limit and len(pipes.reimplemented) > 8:
            print(f"      ... {len(pipes.reimplemented) - 8} more")


def report(results: list[Result], orphans: list[str], detailed: bool) -> int:
    width = max(len(r.surface) for r in results)
    open_total = sum(len(r.open_criteria) for r in results)
    structural_total = sum(len(r.structural) for r in results)
    criteria_total = sum(r.total_criteria for r in results)

    print()
    for result in results:
        print(
            f"  {result.surface.ljust(width)}  {result.verdict.ljust(12)}"
            f"  {len(result.open_criteria)}/{result.total_criteria} criteria open"
            + (f", {len(result.orphans)} unmounted" if result.orphans else "")
        )
        for problem in result.structural:
            print(f"  {' ' * width}  ! {problem}")
        for note in result.notes:
            print(f"  {' ' * width}  ~ {note}")

    unclaimed = [o for o in orphans if not any(o in r.orphans for r in results)]
    if unclaimed and len(results) > 1:
        print(f"\n  Unmounted outside any audited surface ({len(unclaimed)}):")
        for path in unclaimed:
            print(f"    {path}")

    if detailed:
        for result in results:
            for path in result.orphans:
                print(f"    unmounted: {path}")
            if result.open_criteria:
                print(f"\n  {result.surface} — open criteria:")
                for criterion in result.open_criteria:
                    print(f"    - {criterion}")

    pipes = sweep_plumbing()
    print_plumbing(pipes, limit=None if detailed else 12)

    debt = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    if debt.get("pending"):
        print(f"\n  Seen but NOT accepted ({len(debt['pending'])}) — these must be answered:")
        for entry in debt["pending"]:
            print(f"    [{entry.get('seen', '?')}] {entry['finding'][:96]}")
            print(f"      {entry.get('note', '')}")

    print(
        f"\n  {open_total}/{criteria_total} criteria open, {structural_total} structural "
        f"findings, {len(orphans)} components unmounted, {pipes.total} plumbing findings."
    )
    if open_total or structural_total or orphans or pipes.total:
        print("  Not finished. Name what is unfinished, and whether each surface is")
        print("  to wire, to repair or to rebuild — never that it works.\n")
        return 1
    print("  Nothing known is open. This is not proof of use.\n")
    return 0


def plan(results: list[Result], orphans: list[str]) -> int:
    """The delivery plan, generated from the repository so it cannot drift."""
    print("\n# Delivery plan — generated by scripts/finished_work_audit.py\n")
    print("Ranked by cost of the remedy, cheapest first. Every line is derived from")
    print("the ratified documents and the shipped code, not from a conversation.\n")

    print("## P0 — built and never mounted (wiring, not construction)\n")
    for path in orphans:
        print(f"- [ ] mount `{path}`, or delete it and record why")
    print()

    print("## P1 — contract structure broken\n")
    for result in results:
        for problem in result.structural:
            print(f"- [ ] {result.surface}: {problem}")
    print()

    print("## P2 — plumbing the UI cannot reveal\n")
    pipes = sweep_plumbing()
    for finding in pipes.fallbacks:
        print(f"- [ ] silent fallback: {finding}")
    for route in pipes.routes:
        print(f"- [ ] `{route}` — server capability with no door in the UI")
    if pipes.modules:
        print(f"- [ ] {len(pipes.modules)} module(s) never exercised against a recorded "
              f"response: {', '.join(pipes.modules)}")
    print()

    print("## P3 — criteria never evaluated with evidence\n")
    for result in sorted(results, key=lambda r: -len(r.open_criteria)):
        if result.open_criteria:
            print(f"- [ ] {result.surface}: {len(result.open_criteria)}"
                  f"/{result.total_criteria} criteria unproven")
    print()
    return 0


_COUNTED = re.compile(r"^(\d+) (.+)$")


def _phrase(tail: str) -> str:
    """The part of a counted finding that names WHAT is counted, without examples."""
    return tail.split(": ", 1)[0].strip()


def _members(finding: str) -> tuple[str, frozenset[str]]:
    """A list-shaped finding split into its stable phrase and its members.

    `historical-reader census has unclassified locators: a.py, b.py` names a
    SET. Nothing else about it moves; the set does, every time one member is
    classified. Comparing the sentence whole made each of those movements a
    brand-new finding, so the same census needed a new exemption after every
    step forward -- `known-debt.json` carried twenty-six of them.
    """
    head, separator, tail = finding.partition(": ")
    if not separator or "," not in tail:
        return finding, frozenset()
    return head.strip(), frozenset(part.strip() for part in tail.split(",") if part.strip())


def _has_new_member(finding: str, known: set[str]) -> bool:
    """True unless every member of *finding* is already covered under its phrase.

    This does NOT soften the gate: a locator nobody recorded still blocks, even
    when the list around it shrank. What stops blocking is the shrinkage
    itself, which is the debt going down.
    """
    head, members = _members(finding)
    if not members:
        return True
    covered: set[str] = set()
    for entry in known:
        entry_head, entry_members = _members(entry)
        if entry_head == head:
            covered |= entry_members
    return not covered or not members <= covered


def _is_regression(finding: str, known: set[str]) -> bool:
    """True when *finding* is genuinely new, counting a number as a RATCHET.

    A finding that embeds a live count can never match its own recorded line:
    `90 -> 70 -> 60` hardcoded colours is the debt going DOWN, and the gate
    reported every notch as a new finding, so someone re-notched the file by
    hand each time. The duplicated-class check had the same defect one shape
    removed -- five names ranked instead of a number -- and the comment above it
    already said what goes wrong. So the rule, for every counted finding at
    once: it blocks when the number RISES above what was recorded, never when it
    falls. An improvement is not a regression.
    """
    if finding in known:
        return False
    match = _COUNTED.match(finding)
    if not match:
        return _has_new_member(finding, known)
    current, tail = int(match.group(1)), match.group(2)
    # Match on the stable PHRASE, not on the sample that follows it. Several
    # counted findings name examples after a colon -- "…never filled: a.css,
    # b.css", "…worst: .table-scroll" -- and that sample rotates as the debt
    # shrinks. Comparing whole tails meant 16 -> 14 empty stylesheets read as a
    # brand-new finding because two names had dropped off the list, which is the
    # ratchet failing in exactly the direction this function exists to allow.
    head = _phrase(tail)
    ceilings = [
        int(entry.split(" ", 1)[0])
        for entry in known
        if entry.split(" ", 1)[0].isdigit()
        and _phrase(entry.split(" ", 1)[1] if " " in entry else "") == head
    ]
    # Never recorded under any count: it really is new.
    return True if not ceilings else current > max(ceilings)


# ROUTES ARE REPORTED, NOT BLOCKED -- deliberately, and with an end date in
# mind rather than forever.
#
# On 2026-08-01 `unreachable_routes` was repaired: it read only `admin_api.py`
# and saw 137 of the 393 mounted `/api/` routes, so 256 were invisible. That
# blindness is why two Epic 52 routes shipped with no caller under a green
# gate, and why four stories cited `--gate -> 0` as proof of a door.
#
# The repair takes the count of doorless routes from 22 to 59. Those 37 are
# NOT new debt: they existed and could not be seen. Blocking on them would
# turn one scanner fix into a red gate for every session at once, and the
# pressure would be to run `--baseline`, which accepts EVERYTHING as permanent
# -- the single worst outcome, and the reason that command has a warning of
# its own three screens below.
#
# So the route findings stay in the report, where `--plan` lists them one by
# one, and stop short of the gate until they are triaged by hand into
# `known-debt.json`. Flipping this to True is the last step of that triage,
# not a chore to postpone: see action item AI-124.
_ROUTE_FINDINGS_BLOCK = True


def gate(results: list[Result], orphans: list[str]) -> int:
    """Only regressions block. Standing debt is visible, not obstructive."""
    # Claude Code sets stop_hook_active when a Stop hook already blocked once.
    # Blocking again on the same finding would trap the session in a loop with
    # no exit, so the second pass reports and lets go.
    try:
        payload = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
    except (json.JSONDecodeError, OSError):
        payload = {}
    already_blocked = bool(payload.get("stop_hook_active"))

    debt = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    # `pending` holds findings that are seen and dated but NOT accepted —
    # typically another session's in-flight work. They stop blocking, because
    # blocking on someone else's unfinished edit is noise, but they stay loud in
    # the report so they cannot quietly become permanent.
    pending = {entry["finding"] for entry in debt.get("pending", [])}
    known_orphans = set(debt.get("orphans", [])) | pending
    known_structural = set(debt.get("structural", [])) | pending

    new_orphans = [o for o in orphans if o not in known_orphans]
    new_structural = [
        p for r in results for p in r.structural if _is_regression(p, known_structural)
    ]
    pipes = sweep_plumbing()
    known_pipes = set(debt.get("plumbing", [])) | pending
    _blocking = pipes.fallbacks + pipes.french + pipes.styling + pipes.duplicates
    if _ROUTE_FINDINGS_BLOCK:
        _blocking = _blocking + pipes.routes
    new_pipes = [
        f for f in _blocking
        if _is_regression(f, known_pipes) and not f.endswith(f"(target ~{CSS_TARGET_LINES})")
    ]

    # CSS volume is a ratchet: it may fall freely, never rise. Sheets a `pending`
    # entry attributes to another session are left out of the comparison, which
    # is answer 2 applied to this finding too -- it used to be the one finding
    # `pending` could not answer.
    recorded = debt.get("css_lines")
    exempt = css_pending_exemptions(debt)
    current = css_line_count(exempt=exempt)
    if recorded is not None and current > recorded:
        attributed = (
            f" ({css_line_count() - current} more lines sit in sheets a `pending`"
            f" entry attributes elsewhere, and are not counted here)"
            if exempt
            else ""
        )
        new_pipes.append(
            f"CSS grew from {recorded} to {current} lines{attributed}. Every change must "
            f"lower it towards ~{CSS_TARGET_LINES}; a screen that adds rules has not been "
            f"migrated."
        )
    new_structural += new_pipes
    if not new_orphans and not new_structural:
        return 0

    lines = ["Finished-work audit found NEW findings, not in known-debt.json:"]
    for path in new_orphans:
        lines.append(f"  - built and mounted nowhere: {path}")
    for problem in new_structural:
        lines.append(f"  - {problem}")
    lines.append("")
    # The advice used to be one line pointing at --baseline. In a shared checkout
    # the gate also sees other sessions' UNTRACKED files, so the finding in front
    # of you is often not yours -- and --baseline accepts EVERY current finding as
    # permanent debt, not just the one that blocked. Three sessions hit exactly
    # that on 2026-07-31. The third option is named first because it is the right
    # one whenever the work is simply not finished yet.
    lines.append("Three answers, and the first is usually the right one:")
    lines.append("")
    lines.append("  1. It is yours and unfinished -> wire it, or delete it with the")
    lines.append("     screen it belonged to.")
    lines.append("  2. It is ANOTHER session's in-flight work (check `git status`: is it")
    lines.append("     untracked?) -> add it to `pending` in known-debt.json by hand, with")
    lines.append("     a finding, a reason, an owner and today's date. It stops blocking")
    lines.append("     and stays loud in the report, so it cannot become permanent.")
    lines.append("  3. It is genuinely standing debt you accept -> run:")
    lines.append("       python scripts/finished_work_audit.py --baseline")
    lines.append("     This accepts EVERY current finding, not only the one above.")
    if already_blocked:
        # The docstring above always said the second pass "reports and lets go",
        # but the flag was computed and never read -- F841 sat on that line, and
        # the gate blocked EVERY stop until someone acted. On 2026-07-31 that
        # cost one session five consecutive turns on a finding it could not fix,
        # because the file belonged to another session still editing it.
        # Reporting without blocking is what the comment promised, and it is what
        # happens now: the finding is still printed in full, every single time.
        lines.append("")
        lines.append("(Already reported once for this stop -- not blocking again.)")
        print("\n".join(lines), file=sys.stderr)
        return 0
    print("\n".join(lines), file=sys.stderr)
    return 2


def baseline(results: list[Result], orphans: list[str]) -> int:
    """Accept the CURRENT findings as standing debt, without destroying the file.

    This used to build a fresh four-key dict and write it over `known-debt.json`.
    Two things went with it every time, and neither was anyone's intention:

    * `_README`, `_NOTES` and `_WHY_FRENCH_SERVER_COPY` -- the prose that tells
      the next reader what the buckets mean, including the one below;
    * the entire `pending` bucket -- other sessions' in-flight findings, recorded
      precisely so they would NOT be accepted as permanent.

    That made the command the gate message recommends a way to silently erase
    the record other sessions had deliberately left. Three sessions hit the gate
    on 2026-07-31 and each refused to run it; the reason they gave was that it
    accepts unfinished work as permanent, and they were right for a smaller
    reason than the real one.

    So: read, merge, write. Keys this function does not own are carried through
    verbatim, and `pending` entries whose finding is being accepted here are
    dropped from `pending` rather than duplicated across both buckets.
    """
    existing: dict = {}
    if BASELINE.exists():
        try:
            existing = json.loads(BASELINE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  {BASELINE.name} is not readable JSON; refusing to overwrite it.")
            return 2

    pipes = sweep_plumbing()
    accepted = {
        "orphans": sorted(orphans),
        "structural": [p for r in results for p in r.structural],
        "plumbing": pipes.fallbacks + pipes.routes + pipes.french
        + pipes.styling + pipes.duplicates,
        "reimplemented_count": len(pipes.reimplemented),
    }

    merged = dict(existing)
    merged.update(accepted)
    merged.setdefault(
        "_README",
        "Findings accepted as standing debt. The audit reports them but does not "
        "block on them. Anything NOT listed here blocks. Adding a line is a "
        "deliberate act, reviewable in git history. `pending` holds findings seen "
        "and dated but NOT accepted -- another session's in-flight work. They stop "
        "blocking and stay loud in the report, so they cannot quietly become "
        "permanent.",
    )

    # A finding cannot be both accepted and pending: accepting it here answers
    # the pending entry, so the entry goes rather than lingering as a stale
    # claim that someone still owes an answer.
    now_accepted = set(accepted["orphans"]) | set(accepted["structural"])
    kept_pending = [
        entry for entry in existing.get("pending", [])
        if entry.get("finding") not in now_accepted
    ]
    if kept_pending or "pending" in existing:
        merged["pending"] = kept_pending

    BASELINE.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dropped = len(existing.get("pending", [])) - len(kept_pending)
    try:
        where = BASELINE.relative_to(ROOT).as_posix()
    except ValueError:
        # A redirected BASELINE (a test fixture) lives outside the repository.
        # Reporting where it wrote must never be the thing that raises.
        where = str(BASELINE)
    print(f"  Recorded {len(orphans)} unmounted components as known debt in {where}")
    print(f"  Preserved {len(kept_pending)} pending finding(s)"
          + (f", answered {dropped}" if dropped else "")
          + f", and {len(set(existing) - set(accepted))} key(s) this command does not own.\n")
    return 0


def inline_criteria_report(surfaces: list[Surface]) -> int:
    """What each surface owes to a criterion written as prose rather than a bullet.

    The count is the point: it is the size of the blind spot the bullet-only
    parser had, and it must be readable without editing this file.
    """
    print()
    print("  Ratified criteria, by the form they are written in.")
    print()
    width = max(len(s.key) for s in surfaces)
    bullets_total = clauses_total = 0
    for surface in surfaces:
        listed, appended = criteria_split(surface.doc)
        bullets_total += len(listed)
        clauses_total += len(appended)
        marker = "  <-- read by nothing before 2026-08-21" if appended else ""
        print(
            f"  {surface.key.ljust(width)}  {len(listed):3d} bullet(s)  "
            f"{len(appended):3d} clause(s){marker}"
        )
    print()
    print(
        f"  {bullets_total} bullets + {clauses_total} inline clauses = "
        f"{bullets_total + clauses_total} ratified criteria."
    )
    print()
    print("  An inline clause is appended AFTER every bullet of its document, never")
    print("  interleaved: `completeness-ledger.json` keys a verdict by index, and an")
    print("  insertion would re-point every verdict below it.")
    print()
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = sys.argv[1:]
    mode = next((a for a in args if a.startswith("--")), None)
    wanted = [a for a in args if not a.startswith("--")]
    # The product surface is called Analyze; its ratified companion also owns
    # Test and historically used the longer audit key.  Keep both addresses so
    # the Story 65.11 verification command is exactly executable.
    wanted = ["analyze-and-test" if item == "analyze" else item for item in wanted]

    ledger = json.loads(LEDGER.read_text(encoding="utf-8")) if LEDGER.exists() else {}
    orphans = orphan_components()
    surfaces = [s for s in SURFACES if not wanted or s.key in wanted]
    if not surfaces:
        print(f"unknown surface; known: {', '.join(s.key for s in SURFACES)}")
        return 2
    results = [evaluate(s, ledger, orphans) for s in surfaces]

    if mode == "--inline-criteria":
        return inline_criteria_report(surfaces)
    if mode == "--gate":
        return gate(results, orphans)
    if mode == "--baseline":
        return baseline(results, orphans)
    if mode == "--plan":
        return plan(results, orphans)
    return report(results, orphans, detailed=len(results) == 1)


if __name__ == "__main__":
    raise SystemExit(main())
