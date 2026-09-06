#!/usr/bin/env python3
"""Do the sites a ratified amendment names still agree with each other?

WHY THIS INSTRUMENT EXISTS (2026-08-31). `docs/product-architecture/` ratifies
amendments that bind SEVERAL sites to one rule -- "the same threshold that opens
the `Cost` tab and shows the country split", "the `<select>` offers the seven
values of the constant". Nothing measured whether those sites still reach THE
rule. Two of them happened to be pinned by a shared constant; the class as a
whole was sampled, one hand-written test at a time, and a sample is silent about
the site nobody sampled.

WHAT IT MEASURES, EXACTLY. Two classes, declared separately below.

`AMENDMENTS` -- an amendment PINNED BY A SHARED CONSTANT. For each one:

  * the constant it is pinned by still EXISTS in its declaring module -- a rename
    must turn this red rather than make every site vacuously compliant;
  * every Python site the amendment names IMPORTS that constant from that module,
    read off the syntax tree and never off a substring, so a site that re-spells
    the rule's value is caught even when it spells it correctly today.

`ORDERED_AMENDMENTS` -- an amendment PINNED BY AN ORDER, added 2026-09-02. The
import class above is structurally blind to a rule that has to be spelled once
per language: a TypeScript array and a Python tuple cannot import one another, so
"which tabs, in which order" was written four times and nothing held the four
spellings together. Measured that day on « `Mapping` vient avant `Data` » (review
of 2026-08-05, amendment 3): the console and its navigation contract carried the
ratified order, while the tab table of the ratified document itself and `TABS` in
`server/core/datastream_workbench.py` still carried the order that amendment
reversed. Nothing was red, because the tuple is read as a SET. So each site
declares WHERE its spelling starts, its sequence is read from its own file, and
every site must spell the order the amendment ratified.

WHAT IT DOES NOT MEASURE, and says so on every run. An amendment whose rule lives
in PROSE alone -- a sentence about behaviour with no shared symbol and no ordered
list behind it -- is NOT covered, and cannot be: there is nothing for a site to
import and nothing to compare. The `--scope` line names how many of the declared
amendments are in each class, and `NOT_COVERED` below names, one by one, the
sites that carry a declared rule in another language (a TypeScript screen cannot
import a Python tuple) together with the test that pins each of them instead.
That entry is a debt written down, not a pass.

WHY A DECLARATION AND NOT A SWEEP. Which sites an amendment names is a fact about
the AMENDMENT, written in prose by the person who ratified it; no reading of the
code recovers it. The same reason `test_pg_gated_fixtures_declare_their_role`
holds a measured list: a rule that is not decidable by reading is answered by
declaring what was read, and re-reading the declaration when the amendment moves.

    python scripts/check_amendment_agreement.py            # the offenders
    python scripts/check_amendment_agreement.py --scope    # what is covered
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The ratified document these amendments live in. Held as a path so a rename of
#: the document turns the instrument red instead of leaving it pointing nowhere.
WORKBENCH_DOC = "docs/product-architecture/datastream-workbench-and-wizard.md"


@dataclass(frozen=True)
class Amendment:
    """One ratified amendment, and the sites it binds to one shared constant."""

    #: The amendment as the document titles it, so an offender report is findable.
    name: str
    #: The ratified document that carries it.
    document: str
    #: The module that DECLARES the constant, dotted as an import writes it.
    module: str
    #: The constant itself.
    constant: str
    #: Repo-relative Python files the amendment names. Each must import the above.
    sites: tuple[str, ...]
    #: The sentence of the amendment that binds them, quoted, so a reader of a
    #: failure can check the instrument against the document rather than trust it.
    quote: str
    #: Sites the amendment names that CANNOT import the constant, each with the
    #: test that pins it instead. A debt, printed on every run.
    not_covered: tuple[tuple[str, str], ...] = field(default=())


AMENDMENTS: tuple[Amendment, ...] = (
    Amendment(
        name=(
            "Une capacité activée AJOUTE UN ONGLET, OU COLORE LE CHAMP DANS "
            "L'APERÇU (relecture du 2026-08-11, point 11)"
        ),
        document=WORKBENCH_DOC,
        module="core.project_capability_states",
        constant="capability_is_active",
        quote=(
            "AND OFF IS ABSENT, NOT GREY. `capability_is_active` is the same "
            "threshold that opens the `Cost` tab (story 58.6) and shows the "
            "country split of the day grid (story 58.5)"
        ),
        sites=(
            # The tab the capability opens.
            "server/core/datastream_workbench_cost.py",
            # The country split of the day grid.
            "server/core/datastream_daily_breakdown_api.py",
            # And the three capabilities whose effect happens inside a reading.
            "server/core/datastream_reading_capabilities.py",
        ),
    ),
    Amendment(
        name="Le nom, le rôle et la catégorie se posent à la Source (ratifié 2026-08-05)",
        document=WORKBENCH_DOC,
        module="core.datastreams",
        constant="DATA_ROLES",
        quote=(
            "Le `<select>` n'offre par ailleurs que les sept valeurs de la "
            "contrainte (la constante `DATA_ROLES` de `server/core/datastreams.py`)"
        ),
        sites=(
            # The activation writer, the only one that ever wrote `data_role`.
            "server/core/datastream_activation.py",
        ),
        not_covered=(
            (
                "ui/admin/src/datastreams/preconfiguration/DatastreamSetupWizard.tsx",
                "server/tests/core/test_datastreams.py",
            ),
        ),
    ),
)


@dataclass(frozen=True)
class OrderedSite:
    """One file that spells a ratified ORDER, and where its spelling starts."""

    #: Repo-relative path of the file that spells it.
    path: str
    #: A literal that must occur EXACTLY ONCE in that file and opens the
    #: spelling. Deliberately NOT the sequence itself: an anchor that embedded
    #: the answer would stop matching the day the order drifted, and the report
    #: would say "anchor gone" where it has to say "this site spells it wrong".
    anchor: str
    #: How the sequence is read after the anchor.
    #:   `list`  -- the bracketed literal that follows (`[...]`, `(...)`);
    #:   `union` -- everything up to the next `;` (a TypeScript string union);
    #:   `table` -- the first cell of every row of the markdown table that
    #:              follows, up to the next heading.
    reader: str = "list"


@dataclass(frozen=True)
class OrderedAmendment:
    """One ratified amendment whose rule IS a sequence, and its spellings."""

    name: str
    document: str
    #: The order the amendment ratified, lowercased. Compared case-insensitively
    #: so a document that titles its rows `Mapping` and code that writes
    #: `mapping` are the same spelling, which they are.
    order: tuple[str, ...]
    sites: tuple[OrderedSite, ...]
    quote: str


ORDERED_AMENDMENTS: tuple[OrderedAmendment, ...] = (
    OrderedAmendment(
        name="« `Mapping` vient avant `Data` » (relecture du 2026-08-05, amendement 3)",
        document=WORKBENCH_DOC,
        quote=(
            "L'ordre des onglets devient `Overview`, `Mapping`, `Data`, "
            "[`Cost`], [`Placements`], `Processing`, `Runs`, `Outputs`"
        ),
        order=(
            "overview", "mapping", "data", "cost",
            "placements", "processing", "runs", "outputs",
        ),
        sites=(
            # The ratified document's own tab table -- the region a reader
            # opens to learn the order, and the one that carried the reversed
            # order for four weeks after the amendment that reversed it.
            OrderedSite(
                path=WORKBENCH_DOC,
                anchor="| Tab | Content and primary actions | Cross-workspace links |",
                reader="table",
            ),
            # The server's tab registry. Read as a set by the routes and by
            # `TAB_SCHEMAS`, which is exactly why its order could rot unseen.
            OrderedSite(path="server/core/datastream_workbench.py", anchor="\nTABS = "),
            # The console's band.
            OrderedSite(
                path="ui/admin/src/shell/pages/datastreamTabs.ts",
                anchor="export const DATASTREAM_TABS: readonly Tab[] =",
            ),
            # The union in the same file, which a reader reads first.
            OrderedSite(
                path="ui/admin/src/shell/pages/datastreamTabs.ts",
                anchor="export type Tab =",
                reader="union",
            ),
            # The navigation contract that decides which addresses may open.
            OrderedSite(
                path="ui/admin/src/shell/navigation/data.ts",
                anchor='type: "datastream",',
            ),
        ),
    ),
)

#: Where a comment starts, per file suffix, so the walk that looks for the
#: opening bracket of a literal is not fooled by one written in prose above it.
_LINE_COMMENT = {".py": "#", ".ts": "//", ".tsx": "//"}

_QUOTED = re.compile(r"""["']([^"'\n]+)["']""")


def _skip_comments(text: str, start: int, suffix: str) -> str:
    """*text* from *start*, with comments blanked so brackets in prose are gone.

    Blanked rather than deleted: every offset the caller then computes still
    addresses the same character of the real file, which keeps a future error
    message able to name a line.
    """
    marker = _LINE_COMMENT.get(suffix)
    chars = list(text[start:])
    i = 0
    while i < len(chars):
        rest = "".join(chars[i : i + 2])
        if suffix in (".ts", ".tsx") and rest == "/*":
            end = "".join(chars).find("*/", i)
            end = len(chars) if end == -1 else end + 2
            for j in range(i, end):
                if chars[j] != "\n":
                    chars[j] = " "
            i = end
            continue
        if marker and rest.startswith(marker) and "".join(chars[i : i + len(marker)]) == marker:
            while i < len(chars) and chars[i] != "\n":
                chars[i] = " "
                i += 1
            continue
        i += 1
    return "".join(chars)


def _sequence_at(path: Path, site: OrderedSite) -> tuple[str, ...] | str:
    """The sequence *site* spells, or one sentence saying why it cannot be read."""
    text = path.read_text(encoding="utf-8")
    occurrences = text.count(site.anchor)
    if occurrences != 1:
        return (
            f"its anchor {site.anchor.strip()!r} occurs {occurrences} times, so "
            "the instrument cannot say which declaration it is reading"
        )
    start = text.index(site.anchor) + len(site.anchor)

    if site.reader == "table":
        rows: list[str] = []
        for line in text[start:].splitlines():
            if line.startswith("#"):
                break
            row = re.match(r"^\|\s*\*\*(.+?)\*\*", line)
            if row:
                rows.append(row.group(1))
        return tuple(row.strip().lower() for row in rows)

    body = _skip_comments(text, start, path.suffix)
    if site.reader == "union":
        end = body.find(";")
        if end == -1:
            return "no `;` closes the union that follows its anchor"
        body = body[:end]
    else:
        opener = next((i for i, ch in enumerate(body) if ch in "[("), None)
        if opener is None:
            return "no `[` or `(` follows its anchor, so there is no list to read"
        closer = {"[": "]", "(": ")"}[body[opener]]
        depth = 0
        end = None
        for i in range(opener, len(body)):
            if body[i] == body[opener]:
                depth += 1
            elif body[i] == closer:
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            return "the list that follows its anchor is never closed"
        body = body[opener + 1 : end]

    return tuple(value.strip().lower() for value in _QUOTED.findall(body))


def _ordered_offenders() -> list[str]:
    """Every site that spells a ratified order differently, one line each."""
    found: list[str] = []
    for amendment in ORDERED_AMENDMENTS:
        document = REPO_ROOT / amendment.document
        if not document.exists():
            found.append(
                f"{amendment.name}: its document {amendment.document} is gone -- "
                "re-read the amendment before deciding what this instrument holds"
            )
            continue
        for site in amendment.sites:
            path = REPO_ROOT / site.path
            if not path.exists():
                found.append(
                    f"{amendment.name}: {site.path} is gone -- the amendment names "
                    "a site that no longer exists; re-read it and move this entry"
                )
                continue
            read = _sequence_at(path, site)
            if isinstance(read, str):
                found.append(f"{amendment.name}: {site.path} -- {read}")
                continue
            if read != amendment.order:
                found.append(
                    f"{amendment.name}: {site.path} spells "
                    f"{', '.join(read) or '(nothing)'} where the amendment "
                    f"ratified {', '.join(amendment.order)} -- one rule, two "
                    "orders, and neither file can import the other's list"
                )
    return found


def _module_declares(module: str, constant: str) -> bool:
    """Is `constant` a top-level name of `module`? Read, never imported.

    Importing `core.*` here would drag the application's own import graph into a
    script whose whole job is to read files, and a constant is a name in a syntax
    tree well before it is an object.
    """
    path = REPO_ROOT / "server" / Path(*module.split(".")).with_suffix(".py")
    if not path.exists():
        return False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == constant:
                return True
        elif isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == constant
                for target in node.targets
            ):
                return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == constant:
                return True
    return False


def _site_imports(path: Path, module: str, constant: str) -> bool:
    """Does this file reach `module.constant` by import, anywhere in it?

    BOTH SHAPES COUNT, because both are honest and this repository writes both: a
    top-level `from core.x import y`, and the deferred `from core.x import y`
    inside a function that `noqa: PLC0415` marks all over `core/`. What does NOT
    count is a re-spelling of the value, which is exactly what an AST read
    refuses to be fooled by.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == module and any(alias.name == constant for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            # `import core.x` then `core.x.y` -- the module is named, and the
            # attribute access is what the reader would then write.
            if any(alias.name == module for alias in node.names):
                return True
    return False


def _constant_offenders() -> list[str]:
    """Every disagreement of the CONSTANT class, one line each."""
    found: list[str] = []
    for amendment in AMENDMENTS:
        document = REPO_ROOT / amendment.document
        if not document.exists():
            found.append(
                f"{amendment.name}: its document {amendment.document} is gone -- "
                "re-read the amendment before deciding what this instrument holds"
            )
            continue
        if not _module_declares(amendment.module, amendment.constant):
            found.append(
                f"{amendment.name}: {amendment.module} no longer declares "
                f"`{amendment.constant}`, so every site below would agree with "
                "nothing. Point this entry at the constant that replaced it."
            )
            continue
        for site in amendment.sites:
            path = REPO_ROOT / site
            if not path.exists():
                found.append(
                    f"{amendment.name}: {site} is gone -- the amendment names a "
                    "site that no longer exists; re-read it and move this entry"
                )
                continue
            if not _site_imports(path, amendment.module, amendment.constant):
                found.append(
                    f"{amendment.name}: {site} does not import "
                    f"`{amendment.constant}` from `{amendment.module}`, so this "
                    "site holds its own copy of a rule the amendment declares once"
                )
    return found


def offenders() -> list[str]:
    """Every declared disagreement, both classes. Empty is agreement.

    The two classes are kept as separate functions so a test can make one of them
    fail on purpose against a throwaway tree without the other, pointed at the
    real repository, answering for a tree that does not contain it.
    """
    return _constant_offenders() + _ordered_offenders()


def scope_lines() -> list[str]:
    """What this instrument covers, and -- as loudly -- what it does not."""
    sites = sum(len(a.sites) for a in AMENDMENTS)
    uncovered = [pair for a in AMENDMENTS for pair in a.not_covered]
    ordered_sites = sum(len(a.sites) for a in ORDERED_AMENDMENTS)
    lines = [
        f"amendments pinned by a shared constant, declared here: {len(AMENDMENTS)}",
        f"sites verified by import: {sites}",
        f"declared sites that cannot import the constant: {len(uncovered)}",
    ]
    for site, pinned_by in uncovered:
        lines.append(f"  {site} -- another language; pinned instead by {pinned_by}")
    lines.append(
        f"amendments pinned by an ORDER, declared here: {len(ORDERED_AMENDMENTS)}"
    )
    lines.append(f"sites whose spelling of that order is read and compared: {ordered_sites}")
    for amendment in ORDERED_AMENDMENTS:
        for site in amendment.sites:
            lines.append(f"  {site.path} ({site.reader}) -- {site.anchor.strip()}")
    lines.append(
        "NOT COVERED AT ALL: every amendment whose rule lives in prose with no "
        "shared symbol and no ordered list behind it. There is nothing for a site "
        "to import and nothing to compare, so this instrument is silent about "
        "them and must not be read as their guard."
    )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", action="store_true", help="print what is covered")
    args = parser.parse_args()

    if args.scope:
        for line in scope_lines():
            print(line)
        return 0

    found = offenders()
    for line in found:
        print(line)
    declared = len(AMENDMENTS) + len(ORDERED_AMENDMENTS)
    print(
        f"{len(found)} disagreement(s) over {declared} declared amendment(s) "
        f"({len(AMENDMENTS)} pinned by a constant, "
        f"{len(ORDERED_AMENDMENTS)} pinned by an order)"
    )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
