#!/usr/bin/env python3
"""Wanted vs delivered, at the grain of the ratified OBJECT.

    python scripts/object_coverage_audit.py          # the two directions
    python scripts/object_coverage_audit.py --gate   # non-zero if either rots

WHAT QUESTION THIS ANSWERS. `README.md` ratifies 41 objects, each with one
authoritative owner. `navigation.ts` delivers 31 object types, some tabs and
some lenses. Nothing compared the two, so neither of these could be seen: a
ratified object with no surface at all, and a delivered surface answering no
ratified object.

`element-control-loop.md` §3 says every check must run both ways and that only
the forward one has ever run here. This runs both.

WHY THERE IS A HAND-WRITTEN TABLE, AND WHY IT CANNOT ROT. §5 of that document
warns that "a hand-written inventory drifts from the target exactly like the
code did", and it is right. But the two vocabularies genuinely differ — `Product`
is ratified as an object and delivered as a lens over a generic
`master-data-object` type; `Run` is ratified as an object and delivered as a tab
of the Datastream. No parser derives that; it is a design decision, and it has to
be written somewhere.

So it is written ONCE, here, and three checks stop it rotting:

  1. a ratified object absent from `DELIVERY` is an ERROR, not a silent gap —
     a new object added to README cannot slip through unmapped;
  2. a delivered object type that no mapping points at is an ERROR — that is
     the backward direction, and it is what finds the surface answering nothing;
  3. every `type:` / `tab:` / `lens:` reference is resolved against
     `navigation.ts` — a mapping that names something no longer declared fails
     loudly instead of quietly certifying a screen that is gone.

The table below records a DECISION each time, never a guess. `None` means the
ratified object reaches no surface, and it is the finding this script exists to
produce.

WHAT THIS IS NOT. Not the element inventory §5 calls the keystone: an element is
one capability, and one object can owe many. This is one grain coarser, and it
is the coarsest grain at which "wanted vs delivered" can be computed at all.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "docs" / "product-architecture" / "README.md"
NAV = ROOT / "ui" / "admin" / "src" / "shell" / "navigation.ts"

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


#: How each ratified object of README's ownership table reaches a person.
#:
#: A LIST, not one value, and that is a correction rather than a convenience: an
#: AI Path is opened from Context Hub as `ai-path` AND from Test as
#: `trace-observation` -- the same immutable object under two lenses, which
#: `ContentRouter` proves by passing `aiPathId` to the Trace Observation
#: workbench. Modelling one opening point per object would have made the second
#: look like an unratified surface, and the first draft of this script reported
#: exactly that.
#:
#:   type:<t>              its own object contract in navigation.ts
#:   tab:<t>/<tab>         a tab of another object -- the immutable children of
#:                         a Datastream are all of this shape, deliberately:
#:                         they are versions of one thing, not destinations
#:   lens:<ws>/<sec>/<l>   a lens of a collection
#:   global:<surface>      owned by a surface outside project navigation
#:   None                  NOT DELIVERED -- the finding
DELIVERY: dict[str, list[str]] = {
    # Global scope: outside project navigation by design, README's own table.
    "Organization": ["global:organization-settings"],
    "Project": ["global:project-settings"],
    "Project Capability": ["global:project-settings"],
    "Source Authorization": ["global:organization-settings"],
    # Owned by Organization Settings, projected into Data > Sources, which is
    # where a person actually opens one.
    "Source Account": ["type:source-account"],

    # Governance > Master Data. One generic type carries the governed objects
    # and a lens selects the population; `tracked-entity` is its own contract
    # because a Competitor has representations and bindings the others have not.
    "Business Domain": ["type:business-domain"],
    "Product": ["lens:governance/master-data/products", "type:master-data-object"],
    "Activity": ["lens:governance/master-data/activities", "type:master-data-object"],
    "Competitor": ["lens:governance/master-data/competitor-registry", "type:tracked-entity"],
    # Country, Market and Region are governed vocabularies reached through the
    # Registries lens rather than each having a contract of its own.
    "Country": ["lens:governance/master-data/registries", "type:registry"],
    "Market": ["lens:governance/master-data/registries", "type:registry"],
    "Region": ["lens:governance/master-data/registries", "type:registry"],

    "Connector": ["type:connector"],

    # Data. The Datastream's children are immutable versions of one object and
    # are opened as its tabs -- README: "Stable identity; immutable child
    # versions". They are versions of one thing, not destinations.
    #
    # The Plan was mapped to NOTHING in the first draft and that was wrong.
    # The SECOND reading was wrong too, in the opposite direction, and the
    # correction is worth more than either: I read the page header at
    # `WorkbenchProcessingPage.tsx:19` -- "table of instructions, not a control
    # plane" -- and concluded the tab could not edit a Plan. That sentence
    # describes the ordered CHAIN above it. Thirty lines further down, `:166`
    # mounts `SchedulePanel`, under a comment naming AI-119 and stating that the
    # cadence, the retrieval window and the next run are edited there. It is
    # exactly what `datastream-workbench-and-wizard.md:77` requires.
    #
    # Generalising a whole page from one comment is the same failure as the
    # name-matching orphan detector two commits earlier: reading a token instead
    # of the code it labels.
    "Datastream": ["type:datastream"],
    "Plan": ["tab:datastream/processing"],
    "Mapping": ["tab:datastream/mapping"],
    "Import": ["type:import"],
    "Run": ["tab:datastream/runs"],
    "Publication": ["tab:datastream/outputs"],
    "Output": ["tab:datastream/outputs"],
    "Event": ["type:event-configuration"],

    # Governance > Semantic Model / Controls & Quality / Evidence.
    "Canonical Concept": ["type:semantic-concept"],
    "Semantic View": ["type:semantic-view"],
    "DQ Rule": ["type:rule-set", "type:dq-monitor"],
    "Control Case": ["type:control-case"],
    "Evidence Record": ["type:evidence-trace", "type:object-version", "type:audit-event"],

    # Analyze.
    "Query Spec": ["type:query-spec"],
    "Result": ["type:result"],
    "Report": ["type:report"],
    "Visualization": ["type:visualization"],
    "Render": ["type:render"],
    "Share": ["tab:render/sharing"],
    "Notebook": ["type:notebook"],
    "Notebook Run": ["tab:notebook/runs"],
    # README ratifies it as an owned object with immutable seed/project
    # versions. Nothing in navigation declares it, under this name or another.
    #  RENAMED 2026-08-31 (Jean, decision D1 of epic 72): the object was carrying
    #  three spellings -- `Visualization Template` here and in README, `Widget
    #  templates` in `analyze-and-test.md`, nothing in the glossary. The word is
    #  **Chart Template**, and this key is README's own cell, so it moves with it.
    #  DELIVERED 2026-09-01 (story 72.5). The empty list above was the LAST
    #  finding this script produced, and it stood for thirteen months. Two
    #  surfaces answer the object now, and both are named because they are two
    #  different ways in: the `templates` lens of Reports is where a template is
    #  BROWSED (a starting point nobody can browse is not a starting point), and
    #  the `chart-template` type is its Level 3 workbench.
    "Chart Template": ["type:chart-template", "lens:analyze/reports/templates"],

    # Context Hub. `context-topic` / `context-procedure` are the delivered
    # tokens for Knowledge and Skill -- glossary.md records the rename, which is
    # a migration with a backfill.
    "Knowledge": ["type:context-topic"],
    "Skill": ["type:context-procedure", "type:skill"],
    "AI Path": ["type:ai-path", "type:trace-observation"],

    # Test.
    "Golden Question": ["type:golden-question"],
    "Evaluation Run": ["type:evaluation-run"],
    "Feedback": ["type:feedback-review"],
}


def ratified() -> dict[str, str]:
    """README's ownership table: object -> owner workspace."""
    section = README.read_text(encoding="utf-8").split("## Unique object ownership")[1]
    section = section.split("\n## ")[0]
    out = {}
    for obj, _scope, owner in re.findall(
        r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", section, re.M
    ):
        if obj in ("Object",) or obj.startswith("--"):
            continue
        out[obj] = owner
    return out


def delivered(text: str | None = None) -> tuple[set[str], set[tuple[str, str]], set[str]]:
    """navigation.ts: object types, (type, tab) pairs, and lens addresses.

    *text* overrides what is read, so a caller may parse a SUBSET of the eight
    registry files with this same parser instead of writing a second one.
    `scripts/element_inventory.py` is the caller; it needs the same three sets
    at a finer grain and a second regex over the same files is exactly how two
    instruments start disagreeing about what the console declares.
    """
    text = read_navigation() if text is None else text
    types: set[str] = set()
    tabs: set[tuple[str, str]] = set()
    lenses: set[str] = set()

    master_data = re.search(r"MASTER_DATA_TABS\s*=\s*\[([^\]]*)\]", text)
    shared_tabs = re.findall(r'"([a-z-]+)"', master_data.group(1)) if master_data else []

    # Non-greedy to the FIRST closing brace. An earlier version anchored on
    # `\n\s*\}` and silently read 20 of the 31 types: every single-line
    # declaration -- `{ type: "import", tabs: [...] }` -- closes on its own line
    # and was skipped. A parser that under-reads the delivered side turns every
    # missed type into a phantom "not delivered", which is the most expensive
    # kind of false finding an alignment check can produce.
    # A comment between the brace and `type:` (the Competitor contract opens on
    # one) must not hide the declaration either: measured 2026-09-01, one type
    # and its five tabs were invisible to every instrument built on this read.
    for om in re.finditer(r'\{\s*(?://[^\n]*\n\s*)*type:\s*"([a-z-]+)"(.*?)\}', text, re.S):
        name, body = om.group(1), om.group(2)
        types.add(name)
        block = re.search(r"tabs:\s*(?:\[([^\]]*)\]|(MASTER_DATA_TABS))", body)
        if block:
            found = shared_tabs if block.group(2) else re.findall(r'"([a-z-]+)"', block.group(1))
            for tab in found:
                tabs.add((name, tab))

    for wsm in re.finditer(r'key:\s*"([a-z-]+)",\s*\n\s*slug:', text):
        workspace, start = wsm.group(1), wsm.end()
        nxt = re.search(r'key:\s*"[a-z-]+",\s*\n\s*slug:', text[start:])
        block = text[start:start + nxt.start()] if nxt else text[start:]
        for sm in re.finditer(r'section\("([a-z-]+)"', block):
            section, sstart = sm.group(1), sm.end()
            snxt = re.search(r'section\("[a-z-]+"', block[sstart:])
            sblock = block[sstart:sstart + snxt.start()] if snxt else block[sstart:]
            for slug in re.findall(r'\{\s*slug:\s*"([a-z-]+)",\s*label:', sblock):
                lenses.add(f"{workspace}/{section}/{slug}")
    return types, tabs, lenses


def main() -> int:
    args = sys.argv[1:]
    objects = ratified()
    types, tabs, lenses = delivered()
    errors: list[str] = []

    # 1. Every ratified object is mapped.
    for obj in objects:
        if obj not in DELIVERY:
            errors.append(
                f"ratified object with no entry in DELIVERY: {obj!r} "
                f"(owner: {objects[obj]}). Decide how it reaches a person, or record None."
            )
    for obj in DELIVERY:
        if obj not in objects:
            errors.append(f"DELIVERY maps {obj!r}, which README no longer ratifies.")

    # 2. Every mapping resolves against what navigation actually declares.
    pointed_at: set[str] = set()
    for obj, points in DELIVERY.items():
        for how in points:
            if how.startswith("global:"):
                continue
            kind, _, rest = how.partition(":")
            if kind == "type":
                pointed_at.add(rest)
                if rest not in types:
                    errors.append(f"{obj!r} maps to type {rest!r}, which navigation.ts does not declare.")
            elif kind == "tab":
                owner, _, tab = rest.partition("/")
                pointed_at.add(owner)
                if (owner, tab) not in tabs:
                    errors.append(f"{obj!r} maps to tab {rest!r}, which navigation.ts does not declare.")
            elif kind == "lens":
                if rest not in lenses:
                    errors.append(f"{obj!r} maps to lens {rest!r}, which navigation.ts does not declare.")

    # 3. Backward: a delivered type that answers no ratified object.
    for name in sorted(types - pointed_at):
        errors.append(
            f"navigation.ts declares object type {name!r} and no ratified object "
            f"is delivered by it — either it answers a need README does not state, "
            f"or it is a second name for one that is already delivered."
        )

    missing = sorted(o for o, points in DELIVERY.items() if not points and o in objects)
    print(f"  ratified objects (README ownership table) : {len(objects)}")
    print(f"  object types declared in navigation.ts    : {len(types)}")
    print(f"  ratified objects reaching no surface      : {len(missing)}")
    for obj in missing:
        print(f"    {obj} — owner: {objects[obj]}")

    if errors:
        print(f"\n  Mapping or coverage findings ({len(errors)}):")
        for problem in errors:
            print(f"    - {problem}")
    else:
        print("\n  Both directions clean: every ratified object is accounted for, and every")
        print("  delivered object type answers one.")

    print("\n  Grain: the ratified OBJECT. Not the element inventory")
    print("  element-control-loop.md §5 calls the keystone — one object owes many elements.")

    if "--gate" in args:
        return 1 if errors else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
