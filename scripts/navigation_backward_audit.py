#!/usr/bin/env python3
"""The backward direction of the element control loop, for navigation contracts.

    python scripts/navigation_backward_audit.py           # the counts
    python scripts/navigation_backward_audit.py --list     # every unrendered promise
    python scripts/navigation_backward_audit.py --gate     # non-zero if any promise is unrendered

`docs/product-architecture/element-control-loop.md` §3 asks every check to run
both ways, and §5 records the backward direction as **not built**. This is that
direction for one contract: `ui/admin/src/shell/navigation.ts` declares object
types and tabs; `ContentRouter.tsx` and the workbenches it delegates to either
render them or do not.

WHY IT EXISTS. §3 states "31 object types declared in navigation, 14 rendered"
as a number written by hand. Written numbers rot: the four Epic 48-51 workbench
waves landed between that sentence and today, and nothing recomputed it. §6 is
explicit that a status is a computed state and never a claim — so the figure in
that document must come from here, not from prose.

WHAT IT MEASURES, AND AT WHICH GRAIN. This is state 4 ("reachable": mounted AND
in navigation) at the grain of the OBJECT TYPE, plus a coarse read of state 5 at
the grain of the TAB. It is deliberately NOT the element inventory §5 calls the
keystone: an element is one capability, and one tab can owe several. A type that
passes here can still owe six unbuilt capabilities behind a rendered tab. Said
here so this script is never mistaken for the inventory it does not replace.

WHAT IT CANNOT SEE. A tab whose panel renders an honest "not delivered" notice
counts as RENDERED — that is correct for state 5 (the screen draws something
true) and wrong for state 7. `GovernanceObjectWorkbench` does exactly this, on
purpose, and its `PENDING_TAB_OWNER` table is the real inventory for that
surface. This script reports that table's size rather than pretending to judge
it.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "admin" / "src"
NAV = UI / "shell" / "navigation.ts"

#: LE REGISTRE EST EN SEPT FICHIERS depuis AD-42 (2026-08-12) : `navigation.ts`
#: garde le vocabulaire, l'assemblage et les recherches ; chaque espace de
#: travail declare ses sections dans `shell/navigation/<espace>.ts`. Un garde
#: epingle sur UN chemin devient faux EN SILENCE le jour ou le code demenage --
#: mesure ce jour-la : << object types declared in navigation.ts : 31 >> etait
#: passe a **0**, et l'audit continuait d'annoncer << 0 rendus par rien >>.
NAV_SURFACES = tuple([NAV] + sorted((NAV.parent / "navigation").glob("*.ts")))


def read_navigation() -> str:
    """Les sept fichiers du registre, joints. Une seule source pour ce script."""
    return chr(10).join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in NAV_SURFACES
        if path.exists()
    )

ROUTER = UI / "shell" / "ContentRouter.tsx"

#: LES TROIS FICHIERS DU ROUTAGE. AD-42 (2026-08-12) a sorti les deux tables de
#: dispatch de `ContentRouter`, et ce script lisait UN CHEMIN : mesure le jour
#: meme, il est passe de 0 a 34 << declared and rendered by nothing >> sans
#: qu aucun ecran ne bouge. Un garde epingle sur un chemin devient faux EN
#: SILENCE le jour ou le code demenage ; celui-ci suit le code.
ROUTER_SURFACES = (
    ROUTER,
    UI / "shell" / "objectSurfaces.tsx",
    UI / "shell" / "collectionSurfaces.tsx",
)

#: Three dispatch shapes exist in `ContentRouter.tsx`, and only one of them is a
#: literal `objectType === "..."`. The first version of this script read that
#: literal alone and reported ELEVEN unrendered types; ten of them render
#: perfectly well. That draft number nearly went into a ratified document, which
#: is the whole reason this comment is long:
#:
#:   1. BY WORKSPACE — `route.workspace === "governance"` returns
#:      `GovernanceObjectWorkbench` for any object type the section declares.
#:      All twelve Governance types are dispatched by one branch that names none
#:      of them.
#:   2. BY TABLE — Data builds a `contracts` map keyed by section and compares
#:      `route.objectType === contract.type`. The type is a VALUE in that map,
#:      never a literal in a comparison.
#:   3. BY LITERAL — Analyze, Test and Context Hub compare the type directly.
#:
#: A checker that knows only shape 3 does not measure the product, it measures
#: one coding style.

#: Shape 1: workspace -> the branch in ContentRouter that catches all its types.
BY_WORKSPACE = re.compile(r'route\.workspace === "([a-z-]+)"\s*\)\s*\{\s*\n\s*return \(')

#: Shape 2: the `contracts` table Data keys by section.
BY_TABLE = re.compile(r'\btype:\s*"([a-z-]+)",\s*lens:')


def declared() -> list[tuple[str, str, str, tuple[str, ...]]]:
    """(workspace, section, object type, tabs) exactly as navigation.ts declares."""
    text = read_navigation()
    out: list[tuple[str, str, str, tuple[str, ...]]] = []
    # The registry is one nested literal; walking it by workspace keeps a type
    # attached to the section that owns it, which is what a promise is made of.
    for wsm in re.finditer(r'key:\s*"([a-z-]+)",\s*\n\s*slug:', text):
        workspace = wsm.group(1)
        start = wsm.end()
        nxt = re.search(r'key:\s*"[a-z-]+",\s*\n\s*slug:', text[start:])
        block = text[start:start + nxt.start()] if nxt else text[start:]
        for sm in re.finditer(r'section\("([a-z-]+)"', block):
            section = sm.group(1)
            sstart = sm.end()
            snxt = re.search(r'section\("[a-z-]+"', block[sstart:])
            sblock = block[sstart:sstart + snxt.start()] if snxt else block[sstart:]
            for om in re.finditer(r'\{\s*type:\s*"([a-z-]+)"(.*?)\}', sblock, re.S):
                tabs = re.findall(r'"([a-z-]+)"', om.group(2))
                out.append((workspace, section, om.group(1), tuple(tabs)))
    return out


def dispatched() -> tuple[set[str], set[str]]:
    """(workspaces caught wholesale, object types named individually)."""
    text = chr(10).join(
        path.read_text(encoding="utf-8")
        for path in ROUTER_SURFACES
        if path.exists()
    )
    wholesale = set(BY_WORKSPACE.findall(text))
    named = set(re.findall(r'objectType === "([a-z-]+)"', text)) | set(BY_TABLE.findall(text))
    return wholesale, named


def renders(workspace: str, object_type: str, wholesale: set[str], named: set[str]) -> bool:
    return workspace in wholesale or object_type in named


def main() -> int:
    args = sys.argv[1:]
    rows = declared()
    wholesale, named = dispatched()
    unrendered = [r for r in rows if not renders(r[0], r[2], wholesale, named)]

    pending = UI / "governance" / "contracts.ts"
    pending_tabs = 0
    if pending.exists():
        block = re.search(
            r"PENDING_TAB_OWNER[^=]*=\s*\{(.*?)\n\};", pending.read_text(encoding="utf-8"), re.S
        )
        pending_tabs = len(re.findall(r'"[a-z-]+:[a-z-]+"\s*:', block.group(1))) if block else 0

    print(f"  object types declared in navigation.ts : {len(rows)}")
    print(f"  dispatched by a workbench              : {len(rows) - len(unrendered)}")
    print(f"  declared and rendered by nothing       : {len(unrendered)}")
    print(f"  contracted tabs declared undelivered   : {pending_tabs} "
          f"(Governance's own PENDING_TAB_OWNER — its inventory, not this script's verdict)")

    if unrendered and ("--list" in args or "--gate" in args or not args):
        print("\n  Promised at every click, rendered by nothing:")
        for workspace, section, object_type, _ in unrendered:
            print(f"    /{workspace}/{section}/{object_type}/{{id}}")

    print("\n  This is state 4 at the grain of the OBJECT, not the element inventory")
    print("  element-control-loop.md §5 calls the keystone. A type counted here as")
    print("  rendered can still owe unbuilt capabilities behind a tab that draws.")

    if "--gate" in args:
        return 1 if unrendered else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
