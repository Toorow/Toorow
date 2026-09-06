#!/usr/bin/env python3
"""The element inventory, DERIVED — `element-control-loop.md` §5's keystone.

    python scripts/element_inventory.py             # the four planes, per surface
    python scripts/element_inventory.py --unclaimed # only what no ratified object claims
    python scripts/element_inventory.py --json
    python scripts/element_inventory.py --gate      # the written table vs this list
    python scripts/element_inventory.py --no-tools  # skip the 13 s app boot

WHAT QUESTION THIS ANSWERS. `element-control-loop.md` §5 lists the checks that
exist and the ones that do not, and against *the element inventory itself* it
says **no — and it is the keystone**. The inventory that does exist,
`object_coverage_audit.DELIVERY`, is 88 hand-written lines at the OBJECT grain,
and that script's own header says why it may not be derived: `Product` is
ratified as an object and delivered as a lens over a generic type, `Run` as a
tab of the Datastream. Those are design decisions. No parser produces them.

So this script does not replace that table. It derives the OTHER half — what the
code actually delivers — from the four sources this repository already owns, at
the grain of one capability:

  screen   the eight navigation registry files, read through
           `object_coverage_audit.delivered()` (the same parser, never a second
           one): every object type, every (type, tab) pair, every lens address
  route    the mounted Starlette router, `core.admin_api.router`, normalised the
           way `screens/routes.json` is normalised — a parameter is a hole
  tool     the assembled and wire MCP catalogs, through
           `mcp_tool_surface_report._collect()`
  object   README's ownership table and the `DELIVERY` mapping, through
           `object_coverage_audit`

AND THE ONE THING IT COMPUTES THAT NOTHING ELSE DOES: the backward direction at
the ELEMENT grain. `object_coverage_audit` runs it at the object grain — a
declared object TYPE that no ratified object is delivered by. It cannot see a
tab or a lens, because a ratified object maps to at most a handful of them. A
tab is a capability the console promises at a click; 127 of them were claimed by
no ratified object on the day this was written, and no instrument had ever
counted them.

AND IT NOW RUNS THAT DIRECTION ON THE ROUTES AND THE TOOLS TOO (2026-09-02).
Until today the two planes were ENUMERATED and reconciled with nothing: the
report printed every address by declaring module and every tool by profile, and
`element-control-loop.md` §5 said so in its own table — *"routes and tools are
enumerated per declaring module and per profile, and mapped to no element yet"*.
An enumeration answers "how many", never "which capability does this one serve",
which is the whole of §3's backward direction.

  HOW AN ADDRESS IS MAPPED, AND WHAT THE MAPPING CLAIMS. One vocabulary, derived
  from the two ratified sources this script already reads — the navigation
  registry (`type`, `tab`, `lens`) and `DELIVERY` — never a second hand-written
  table, because a hand-written route table is the very defect §5 forbids. An
  address is cut into its words (`/` and `-` for a route, `_` for a tool) and
  every contiguous window of at most three of them is matched against that
  vocabulary. `/api/projects/{}/datastreams/{}/workbench/mapping` names
  `type:datastream` and its tab `mapping`; `get_datastream_report` names
  `type:datastream` and `type:report`; `/api/context/procedures/{}/versions`
  names `type:context-procedure` — the two-segment join is what stops that one
  reading as drift.

  AND THE RATIFIED OBJECT'S OWN NAME IS NOT A WORD OF THAT VOCABULARY, WHICH IS
  A REFUSAL, NOT AN OVERSIGHT. README ratifies `Project`, `Run`, `Plan`,
  `Mapping`, `Output` — five words that sit in hundreds of addresses meaning
  something else entirely, `/api/projects/{}/…` first among them. Matching them
  would claim nearly every route for an object nobody checked, and a false claim
  is worse here than a missing one: it reports that an address answers a
  capability when no one established that it does. Only the tokens the console
  DECLARES are matched — they are compound and specific by construction. The
  cost is visible and kept: `get_procedure` and `get_knowledge` are claimed by
  nothing, because the delivered token is `context-procedure` / `context-topic`.
  That gap is not this script's noise; it is the `procedure` vs `skill`
  divergence `element-control-loop.md` §2 already records, arriving on a second
  plane.

  IT CLAIMS THAT THE ADDRESS NAMES THE ELEMENT, IN THE RATIFIED VOCABULARY, AND
  NOTHING MORE. It is not proof that the route serves that capability correctly,
  and it never becomes a state: §4's rule is that a status is computed, and this
  is computed on every run, from the booted router and the live registry. The
  finding is the complement — an address that names no delivered element at all,
  or one whose element no ratified object claims. Both are printed, neither is
  gated, for the reason the header already gives below.

WHAT IS GATED, AND WHAT IS ONLY REPORTED.

  gated   a delivery point written in `DELIVERY` that this derivation does not
          carry — the written list against the derived list, the shape of
          `tests/conformance/test_route_inventory_is_current.py`;
  gated   a plane that collapses. Each carries a floor, because an inventory
          that scans nothing reports a clean sheet — criterion 13 of
          `module-boundaries.md`, and the reason `quiet_guard_census.py` exists;
  printed the unclaimed elements. Blocking on a pre-existing 127 would hold the
          gate shut for every session at once, which is the treatment
          `finished_work_audit.py:1904` already reasoned through for the 37
          doorless routes it uncovered in one repair. The number is the finding;
          it belongs in the report, next to the command that produces it.

WHAT THIS IS NOT. It is not a state computation. Every element it lists is at
state 3 or 4 of the seven — served, and reachable when navigation declares it.
Nothing here says a screen draws (5), matches its mockup (6) or has ever been
traversed (7). `scripts/ledger_verdict_census.py` measures what the recorded
verdicts CLAIM about that; neither script may promote a claim into a state.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAV_DIR = ROOT / "ui" / "admin" / "src" / "shell"
SERVER = ROOT / "server"

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import object_coverage_audit as coverage  # noqa: E402

#: A route parameter is a hole. The SAME normalisation `scripts/screens.py:793`
#: and `test_route_inventory_is_current.py:36` apply, so the three readers of the
#: router agree on what an address is.
_HOLE = re.compile(r"\{[^}]*\}")

#: The floors. Each is the answer to one question: *if this plane scanned
#: nothing, would anything be false?* They are deliberately far below the
#: measured figures (35 / 133 / 23 / 612 / 130 on 2026-08-31) — a floor is not a
#: budget, it exists to fail on an empty scan, and a floor set at today's exact
#: count would redden on every ordinary addition instead.
FLOORS = {
    "screen.type": 25,
    "screen.tab": 90,
    "screen.lens": 15,
    "route": 300,
    "object": 30,
    "tool.assembled": 60,
    #: The two floors of the route/tool reconciliation (2026-09-02). They guard
    #: the OPPOSITE failure from the ones above: a vocabulary that collapses —
    #: `coverage.delivered()` returning nothing, a registry file renamed — makes
    #: every address unclaimed at once, and a bigger finding reads like more
    #: drift rather than like a broken instrument. Measured 256 / 19 the day
    #: they were written; set far below, because a floor is not a budget.
    "route.claimed": 120,
    "tool.claimed": 8,
}


@dataclass
class Element:
    """One capability, at the address the code delivers it."""

    plane: str  # screen | route | tool | object
    kind: str  # type | tab | lens | route | wire-tool | app-tool | object
    address: str
    surface: str  # the workspace, the declaring module, the profile, the owner
    claimed_by: list[str] = field(default_factory=list)  # ratified objects, if any
    #: For a route or a tool: the delivered screen elements its address NAMES,
    #: as `type:x` / `tab:x#y` / `lens:w/s/l`. The evidence behind `claimed_by`,
    #: and the finding when it is non-empty while `claimed_by` is not: the
    #: address answers a capability the console delivers and README ratifies
    #: under no object.
    answers: list[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        return f"{self.plane}::{self.kind}:{self.address}"


# --------------------------------------------------------------------------- #
# screen — the eight navigation registry files
# --------------------------------------------------------------------------- #


def _navigation_files() -> list[Path]:
    """The registry, in the seven-plus-one shape AD-42 left it in."""
    return [p for p in coverage.NAV_SURFACES if p.exists()]


#: `key: "governance",` — the workspace a registry file declares itself as. Read
#: from the file rather than guessed from its name, so a workspace renamed in
#: `vocabulary.ts` moves this report with it instead of leaving a stale label.
_WORKSPACE_KEY = re.compile(r'key:\s*"([a-z-]+)"\s*,\s*\n\s*slug:')


def _workspace_of_file(text: str, fallback: str) -> str:
    found = _WORKSPACE_KEY.search(text)
    return found.group(1) if found else fallback


def _declaring_workspace(token: str, texts: dict[str, str]) -> str:
    """Which workspace's registry file writes *token*.

    A lookup, not a parser: the parse is `coverage.delivered()`'s and stays
    there. A type declared in two files answers with both names joined rather
    than silently picking one — two files declaring one contract is itself a
    finding, and hiding it here would be the instrument choosing what to see.
    """
    owners = sorted(
        _workspace_of_file(text, name)
        for name, text in texts.items()
        if token in text and name != "navigation.ts"
    )
    return "+".join(dict.fromkeys(owners)) if owners else "?"


def screen_elements() -> list[Element]:
    types, tabs, lenses = coverage.delivered()
    texts = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in _navigation_files()}

    out: list[Element] = []
    for name in sorted(types):
        workspace = _declaring_workspace(f'type: "{name}"', texts)
        out.append(Element("screen", "type", name, workspace))
    for owner, tab in sorted(tabs):
        workspace = _declaring_workspace(f'type: "{owner}"', texts)
        out.append(Element("screen", "tab", f"{owner}#{tab}", workspace))
    for address in sorted(lenses):
        out.append(Element("screen", "lens", address, address.split("/")[0]))
    return out


# --------------------------------------------------------------------------- #
# route — the mounted router, never a cached file
# --------------------------------------------------------------------------- #


def route_elements() -> list[Element]:
    """Every address the composed Starlette router mounts, by declaring module.

    `screens/routes.json` holds the same set and is a CACHE — `scripts/screens.py`
    reads it once and never re-derives it, which is why
    `test_route_inventory_is_current.py` exists at all. An inventory that read
    the cache would inherit its drift, so this boots the router.
    """
    if str(SERVER) not in sys.path:
        sys.path.insert(0, str(SERVER))
    from core.admin_api import router  # noqa: PLC0415

    seen: dict[str, Element] = {}
    for route in router.routes:
        path = _HOLE.sub("{}", getattr(route, "path", ""))
        if not path:
            continue
        endpoint = getattr(route, "endpoint", None)
        module = getattr(endpoint, "__module__", "") or "?"
        module = module.rsplit(".", 1)[-1]
        seen.setdefault(path, Element("route", "route", path, module))
    return sorted(seen.values(), key=lambda e: e.address)


# --------------------------------------------------------------------------- #
# tool — the assembled and wire MCP catalogs
# --------------------------------------------------------------------------- #


def mcp_report() -> dict:
    """`mcp_tool_surface_report._collect()`, booted once.

    It boots the real app — about thirteen seconds. Calling it twice for the
    same run would double that for nothing, so every caller takes the dict.
    """
    import mcp_tool_surface_report as tools  # noqa: PLC0415

    return tools._collect()


def tool_elements(report: dict) -> list[Element]:
    """The MCP catalog, through the report that already owns its measurement.

    Two populations, not one, and the distinction is the whole point of
    `mcp_tool_surface_report`: a tool on the WIRE is reachable by a model in the
    default persona; a tool that is only assembled is reachable through a host
    that already knows its name. Collapsing them would report a capability as
    delivered to a reader who cannot see it.
    """
    wire = set(report["wire_tool_names"])
    out = [Element("tool", "wire-tool", name, "mcp/wire") for name in sorted(wire)]
    out += [
        Element("tool", "app-tool", name, "mcp/app-only")
        for name in sorted(set(report["app_only_tools"]) - wire)
    ]
    return out


# --------------------------------------------------------------------------- #
# object — README's ownership table, and how each object is delivered
# --------------------------------------------------------------------------- #


def object_elements() -> list[Element]:
    objects = coverage.ratified()
    out: list[Element] = []
    for name, owner in sorted(objects.items()):
        points = coverage.DELIVERY.get(name, [])
        element = Element("object", "object", name, owner.strip())
        element.claimed_by = list(points)
        out.append(element)
    return out


# --------------------------------------------------------------------------- #
# The comparison: the written table against the derived list
# --------------------------------------------------------------------------- #


def _screen_addresses(screens: list[Element]) -> set[str]:
    return {f"{e.kind}:{e.address}" for e in screens}


def delivery_points_not_derived(screens: list[Element]) -> list[str]:
    """Delivery points `DELIVERY` writes down that this derivation cannot find.

    `global:` points are owned by a surface outside project navigation and have
    no navigation address by design — `object_coverage_audit` already skips them
    for the same reason, and inventing one here would manufacture a finding.
    """
    known = _screen_addresses(screens)
    missing: list[str] = []
    for name, points in sorted(coverage.DELIVERY.items()):
        for point in points:
            kind, _, rest = point.partition(":")
            if kind == "global":
                continue
            if kind == "type":
                address = f"type:{rest}"
            elif kind == "tab":
                owner, _, tab = rest.partition("/")
                address = f"tab:{owner}#{tab}"
            elif kind == "lens":
                address = f"lens:{rest}"
            else:
                missing.append(f"{name}: {point!r} uses an unknown delivery kind {kind!r}")
                continue
            if address not in known:
                missing.append(
                    f"{name}: DELIVERY names {point!r}, which the navigation registry "
                    f"does not deliver."
                )
    return missing


def claim_screens(screens: list[Element]) -> None:
    """Mark each screen element with the ratified objects that claim it.

    Claiming a TYPE claims nothing about its tabs: that is the grain the object
    audit cannot reach, and leaving the tabs unclaimed is the finding, not a gap
    in this function.
    """
    by_address: dict[str, list[Element]] = {}
    for element in screens:
        by_address.setdefault(f"{element.kind}:{element.address}", []).append(element)

    for name, points in coverage.DELIVERY.items():
        for point in points:
            kind, _, rest = point.partition(":")
            if kind == "type":
                address = f"type:{rest}"
            elif kind == "tab":
                owner, _, tab = rest.partition("/")
                address = f"tab:{owner}#{tab}"
            elif kind == "lens":
                address = f"lens:{rest}"
            else:
                continue
            for element in by_address.get(address, []):
                element.claimed_by.append(name)


# --------------------------------------------------------------------------- #
# The vocabulary — which delivered element a route or a tool NAMES
# --------------------------------------------------------------------------- #

#: How far a window of words may reach. THREE, because that is the longest
#: delivered token the registry declares — `value-mapping-table`,
#: `master-data-object`, `event-configuration` (two), `metric-definition` (two).
#: A wider window buys nothing and starts joining two neighbouring nouns into a
#: token no registry declares.
_WINDOW = 3


def _singular(word: str) -> str:
    """`datastreams` -> `datastream`. A route says the plural, a registry the singular.

    Deliberately three rules and no library: the vocabulary being matched is the
    repository's own, all of it regular, and a stemmer would start folding
    `analysis`/`analyse` — two different words here — onto one token.
    """
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _windows(words: list[str]) -> dict[str, tuple[int, int]]:
    """Every contiguous window of at most `_WINDOW` words, with its position.

    The position is what keeps a tab claim honest: a tab is claimed only when its
    word comes AFTER the type that owns it, so `/api/.../datastreams/{}/workbench
    /mapping` names `tab:datastream#mapping` and a stray `mapping` earlier in an
    unrelated address does not.
    """
    found: dict[str, tuple[int, int]] = {}
    for start in range(len(words)):
        for end in range(start + 1, min(start + _WINDOW, len(words)) + 1):
            part = list(words[start:end])
            found.setdefault("-".join(part), (start, end))
            part[-1] = _singular(part[-1])
            found.setdefault("-".join(part), (start, end))
    return found


@dataclass
class Vocabulary:
    """The delivered vocabulary, and which ratified object claims each word of it."""

    types: set[str]
    tabs: dict[str, set[str]]  # type -> its tabs
    lenses: set[str]  # workspace/section/slug
    claimants: dict[str, list[str]]  # element address -> ratified objects

    def objects_claiming(self, addresses: list[str]) -> list[str]:
        out: list[str] = []
        for address in addresses:
            for name in self.claimants.get(address, ()):
                if name not in out:
                    out.append(name)
        return sorted(out)


def vocabulary(screens: list[Element]) -> Vocabulary:
    """Built from the SAME two ratified sources, never from a third list."""
    types = {e.address for e in screens if e.kind == "type"}
    tabs: dict[str, set[str]] = {}
    for element in screens:
        if element.kind == "tab":
            owner, _, tab = element.address.partition("#")
            tabs.setdefault(owner, set()).add(tab)
    lenses = {e.address for e in screens if e.kind == "lens"}
    claimants = {
        f"{e.kind}:{e.address}": list(e.claimed_by) for e in screens if e.claimed_by
    }
    return Vocabulary(types=types, tabs=tabs, lenses=lenses, claimants=claimants)


def elements_named(words: list[str], vocab: Vocabulary) -> list[str]:
    """The delivered elements the word sequence *words* names, as addresses."""
    seen = _windows(words)
    found: set[str] = set()
    for name in vocab.types:
        position = seen.get(name)
        if position is None:
            continue
        found.add(f"type:{name}")
        own_words = set(name.split("-"))
        for tab in vocab.tabs.get(name, ()):
            # A tab spelled inside its owner's own name (`query` of `query-spec`)
            # is the owner being read twice, not a tab being named.
            if tab in own_words or _singular(tab) in own_words:
                continue
            at = seen.get(tab) or seen.get(_singular(tab))
            if at and at[0] >= position[1]:
                found.add(f"tab:{name}#{tab}")
    for lens in vocab.lenses:
        workspace, section, slug = lens.split("/")
        # The slug ALONE is not enough: `templates` is a lens of Analyze > Reports
        # and also the word a connector uses for its delivery templates. The
        # section or the workspace has to be named too, or the lens is not the
        # one this address opens.
        if slug in seen and (section in seen or workspace in seen):
            found.add(f"lens:{lens}")
    return sorted(found)


def _route_words(address: str) -> list[str]:
    """`/api/projects/{}/datastreams/{}/workbench/runs` -> the words a reader sees."""
    words: list[str] = []
    for segment in address.strip("/").split("/"):
        if segment in ("api", "{}"):
            continue
        words.extend(part for part in segment.split("-") if part)
    return words


def claim_addresses(elements: list[Element], vocab: Vocabulary, words) -> None:
    """Attach to each route or tool the elements it names, and their claimants."""
    for element in elements:
        element.answers = elements_named(words(element.address), vocab)
        element.claimed_by = vocab.objects_claiming(element.answers)


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #


@dataclass
class Inventory:
    screens: list[Element]
    routes: list[Element]
    tools: list[Element]
    objects: list[Element]
    assembled_tools: int = 0
    #: False when `--no-tools` skipped the app boot. The tool floor is then not
    #: applied — an unmeasured plane must not be reported as a collapsed one.
    tools_measured: bool = True

    @property
    def all(self) -> list[Element]:
        return self.screens + self.routes + self.tools + self.objects

    def counts(self) -> dict[str, int]:
        return {
            "screen.type": sum(1 for e in self.screens if e.kind == "type"),
            "screen.tab": sum(1 for e in self.screens if e.kind == "tab"),
            "screen.lens": sum(1 for e in self.screens if e.kind == "lens"),
            "route": len(self.routes),
            "route.claimed": sum(1 for e in self.routes if e.claimed_by),
            "object": len(self.objects),
            "tool.assembled": self.assembled_tools,
            "tool.wire": sum(1 for e in self.tools if e.kind == "wire-tool"),
            "tool.claimed": sum(1 for e in self.tools if e.claimed_by),
        }

    def unclaimed(self) -> list[Element]:
        """Every delivered element no ratified object claims, on the three planes.

        The object plane is not here on purpose: a ratified object claimed by
        nothing is `object_coverage_audit.py`'s finding, at its own grain, and
        counting it twice would make one gap read as two.
        """
        return [
            element
            for element in self.screens + self.routes + self.tools
            if not element.claimed_by
        ]

    def collapsed(self) -> list[str]:
        """Planes below their floor — the inventory saying it scanned nothing."""
        counts = self.counts()
        out: list[str] = []
        for plane, floor in FLOORS.items():
            if not (self.tools_measured or not plane.startswith("tool.")):
                continue
            if counts.get(plane, 0) >= floor:
                continue
            why = (
                "the delivered vocabulary collapsed, so every address now reads "
                "as drift."
                if plane.endswith(".claimed")
                else "the source moved and this inventory scanned (almost) nothing."
            )
            out.append(f"plane {plane!r} holds {counts.get(plane, 0)}, floor is {floor}: {why}")
        return out


def build(with_tools: bool = True) -> Inventory:
    screens = screen_elements()
    claim_screens(screens)
    vocab = vocabulary(screens)
    routes = route_elements()
    claim_addresses(routes, vocab, _route_words)
    tools: list[Element] = []
    assembled = 0
    if with_tools:
        report = mcp_report()
        tools = tool_elements(report)
        claim_addresses(tools, vocab, lambda name: name.split("_"))
        assembled = report["assembled_tools"]
    return Inventory(
        screens=screens,
        routes=routes,
        tools=tools,
        objects=object_elements(),
        assembled_tools=assembled,
        tools_measured=with_tools,
    )


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def _report(inventory: Inventory, with_tools: bool) -> None:
    counts = inventory.counts()
    print()
    print("  The delivered element inventory, derived. Grain: one capability.")
    print()
    print(f"    screen  object types                 : {counts['screen.type']}")
    print(f"    screen  tabs of an object            : {counts['screen.tab']}")
    print(f"    screen  collection lenses            : {counts['screen.lens']}")
    print(f"    route   addresses the router mounts  : {counts['route']}")
    if with_tools:
        print(f"    tool    assembled MCP catalog        : {counts['tool.assembled']}")
        print(f"    tool    reaching the default wire    : {counts['tool.wire']}")
    else:
        print("    tool    (skipped: --no-tools)")
    print(f"    object  ratified in README           : {counts['object']}")

    print("\n  Screen elements per workspace:")
    per: dict[str, list[Element]] = {}
    for element in inventory.screens:
        per.setdefault(element.surface, []).append(element)
    width = max(len(k) for k in per) if per else 1
    for workspace in sorted(per):
        group = per[workspace]
        unclaimed = sum(1 for e in group if not e.claimed_by)
        print(
            f"    {workspace.ljust(width)}  {len(group):3d} element(s), "
            f"{unclaimed:3d} claimed by no ratified object"
        )

    print("\n  Route addresses per declaring module (top 12), and how many name")
    print("  no element a ratified object claims:")
    modules: dict[str, list[int]] = {}
    for element in inventory.routes:
        row = modules.setdefault(element.surface, [0, 0])
        row[0] += 1
        row[1] += 0 if element.claimed_by else 1
    for module, (total, orphan) in sorted(
        modules.items(), key=lambda kv: (-kv[1][0], kv[0])
    )[:12]:
        print(f"    {module:38s} {total:4d}  {orphan:4d} unclaimed")

    print("\n  BACKWARD, AT THE ELEMENT GRAIN — what no ratified object claims:")
    planes = [("screen element", inventory.screens), ("route address", inventory.routes)]
    if with_tools:
        planes.append(("MCP tool", inventory.tools))
    for label, group in planes:
        orphan = [e for e in group if not e.claimed_by]
        named = [e for e in orphan if e.answers]
        tail = f", {len(named)} of them naming a delivered element" if named else ""
        print(f"    {label:15s} : {len(orphan):4d} of {len(group):4d}{tail}")
    print(
        f"    {'total':15s} : {len(inventory.unclaimed()):4d} elements, "
        "the population `--unclaimed` lists"
    )
    print("  `object_coverage_audit.py` runs this direction at the OBJECT grain and")
    print("  cannot see a tab, a lens, a route or a tool. Reported, never gated: see")
    print("  the header.")

    missing = delivery_points_not_derived(inventory.screens)
    if missing:
        print(f"\n  WRITTEN BUT NOT DELIVERED ({len(missing)}):")
        for line in missing:
            print(f"    - {line}")
    else:
        print("\n  Every delivery point the hand-written table names is delivered.")

    for line in inventory.collapsed():
        print(f"\n  COLLAPSED PLANE: {line}")
    print()


def _unclaimed_report(inventory: Inventory) -> None:
    """Every element no ratified object claims, with what it does name.

    The trailing `-> names ...` is the difference between two very different
    findings that a bare list flattens into one: an address that answers a
    capability the console delivers and README ratifies under no object, and an
    address that answers nothing anyone declared.
    """
    print()
    for element in inventory.unclaimed():
        line = f"  {element.surface:26s} {element.kind:9s} {element.address}"
        if element.answers:
            line += f"   -> names {', '.join(element.answers)}"
        print(line)
    print()


def _gate(inventory: Inventory) -> int:
    problems = delivery_points_not_derived(inventory.screens) + inventory.collapsed()
    counts = inventory.counts()
    print(
        "element inventory: "
        + ", ".join(
            f"{plane}={total}"
            for plane, total in sorted(counts.items())
            if inventory.tools_measured or not plane.startswith("tool.")
        )
        + ("" if inventory.tools_measured else "  (tool planes not measured: --no-tools)")
    )
    if not problems:
        print("OK: the written table names nothing the code does not deliver, and no")
        print("plane collapsed.")
        return 0
    print(f"\nREFUSED ({len(problems)}):")
    for line in problems:
        print(f"  {line}")
    print()
    print("A delivery point in `object_coverage_audit.DELIVERY` must resolve to an")
    print("address the navigation registry declares. Repair the mapping, or the")
    print("registry — never this script, which only compares them.")
    return 1


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable")
    parser.add_argument("--unclaimed", action="store_true", help="only the unclaimed elements")
    parser.add_argument("--gate", action="store_true", help="written table vs derived list")
    parser.add_argument("--no-tools", action="store_true", help="skip the MCP app boot")
    args = parser.parse_args(argv)

    inventory = build(with_tools=not args.no_tools)
    if args.gate:
        return _gate(inventory)
    if args.json:
        print(
            json.dumps(
                {
                    "counts": inventory.counts(),
                    "unclaimed": len(inventory.unclaimed()),
                    "elements": [asdict(e) for e in inventory.all],
                    "written_but_not_delivered": delivery_points_not_derived(inventory.screens),
                    "collapsed_planes": inventory.collapsed(),
                },
                indent=2,
            )
        )
        return 0
    if args.unclaimed:
        _unclaimed_report(inventory)
        return 0
    _report(inventory, with_tools=not args.no_tools)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
