#!/usr/bin/env python3
"""The four planes of every gesture in `user-bridge.md` §5, DERIVED.

    python scripts/user_bridge_census.py            # the four planes, per gesture
    python scripts/user_bridge_census.py --rows     # one line per gesture
    python scripts/user_bridge_census.py --json
    python scripts/user_bridge_census.py --gate     # the written matrix vs this derivation
    python scripts/user_bridge_census.py --no-tools # skip the ~15 s app boot

WHAT QUESTION THIS ANSWERS. `user-bridge.md` §1.3 states the rule the whole
product is judged by — *a capability exists on four planes, and green on three
it delivers nothing* — and §5 carries the matrix that applies it to eighteen
gestures. The matrix was derived from the code on 2026-09-01 and then FROZE.
`completeness-ledger.json`, `user-bridge[1]`, records exactly that gap in the
words of the session that measured it: *"the count still lives only in the
document ... and `ls scripts/ | grep -i capab\\|trace` returns NOTHING, so no
script in the repository computes it. `capability-trace` is a skill that traces
ONE capability at a time; it is not a standing measure."*

This is the standing measure. It parses the four matrices out of the document
and re-derives, for each of their rows, the four planes from the living
sources — never from a list written here:

  component  the page's own component subtree draws from the shared entry point
             (`ui/admin/src/ui/index.ts`, itself re-exporting
             `ui/admin/src/components/ui/*`). §1.4: *"les primitives vivent dans
             `components/ui/` ; une page qui dessine la sienne fait dériver le
             socle"* — so what is measured is whether the page draws from the
             library at all, and how much of its subtree does
  api        the composed Starlette router, `core.admin_api.router`, normalised
             the way `element_inventory.route_elements()` normalises it — one
             reader of the router, never a second
  ui         the navigation registry and `ContentRouter`, read through
             `scripts/screens.py resolve()` — the section is declared and
             mounted, the Level 3 object type is answered, the tab is declared
  mcp        the registered declarations and the wire projection, through
             `mcp_tool_surface_report._collect()` and `mcp_profiles`

WHY IT COMPARES RATHER THAN REGENERATES. The row's *state* is a judgement — which
verbs a set of tools covers is not something a parser produces, exactly as
`object_coverage_audit.DELIVERY` keeps the judgement of which address answers
which ratified object. So the document keeps the sentences and this script holds
them to the code: every address, every route, every module line and every tool
name the matrix cites must still resolve, and the state cell must still agree
with the planes. A derivation whose citation no longer resolves is not a
derivation — the document says so itself, in §5, about the seven citations that
had rotted before it: *"c'est une affirmation, et c'est exactement le défaut que
le statut `DERIVED` existe pour empêcher."*

WHAT IS GATED.

  * a page address the matrix cites that navigation no longer declares, or that
    `ContentRouter` no longer mounts;
  * a `+ N tabs` count, or an `explicit` / `blanket:x` annotation, that the
    registry contradicts;
  * a route fragment no mounted address carries, and a server module that
    contributes no route to the composed router;
  * a `<module>.py:<line>` citation whose line is out of the file, or which no
    longer lands in a route declaration when the module is a route module;
  * an MCP tool name that is no longer registered;
  * a `delivered` row that is not green on all four planes, a `partial` row that
    does not name its missing plane, an `absent` row that some surface carries;
  * a row whose four planes are all missing — a gesture written into the matrix
    that no surface carries;
  * the per-lot count lines and the total line against the parsed rows;
  * the floors, because a parse that finds nothing reports a clean sheet — the
    same reason `element_inventory.FLOORS` exists.

WHAT IS REPORTED AND NOT GATED: the `mounted admin_api.py:<line>` half of a
route citation. `admin_api.py` is one 3 000-line router that every neighbouring
story edits, so its line numbers move under work that touches nothing in this
matrix — three commits moved them the day after the matrix landed. AI-255 already
ruled on that shape for `screens.py`: *the anchor, not the line number*. So the
splice line the router really carries is derived and printed beside the cited
one, and repairing the cell is a documentation edit, not a gate.

WHAT THIS IS NOT. It does not compute a state from scratch, and must not: which
verbs the named tools cover, and therefore whether a row is `partial` for the
third verb or `delivered`, is the judgement the matrix records. It also says
nothing about the journey or the principal action — §5 names those as the four
things still to be collected from Jean, and no command in the repository knows
them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "product-architecture" / "user-bridge.md"
UI_SRC = ROOT / "ui" / "admin" / "src"
SERVER = ROOT / "server"

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import element_inventory  # noqa: E402

import screens  # noqa: E402

#: The shared visual vocabulary, in the two directories §1.4 and the entry point
#: of `ui/index.ts` name. A page file that imports from neither draws its own.
SHARED_VOCABULARY = ("ui/admin/src/ui/", "ui/admin/src/components/ui/")

#: A route parameter is a hole. The SAME normalisation `element_inventory._HOLE`
#: and `screens.py` apply, so every reader of the router agrees on an address.
_HOLE = re.compile(r"\{[^}]*\}")

#: `GET `, `GET/POST `, `POST ` in front of a path inside one backtick.
_VERB = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE)(?:/(?:GET|POST|PUT|PATCH|DELETE))*\s+")

#: `data/datastreams`, `governance/master-data`, `data/datastreams/o/datastream/runs`.
_ADDRESS = re.compile(r"^[a-z][a-z0-9-]*/[a-z0-9-]+(?:/o/[a-z0-9-]+/[a-z0-9-]+)?$")

#: `datastream_workbench_api.py:982`, `controls_quality_api.py:44-46`,
#: `governance_read_model.py:250,284,2039`.
_PY_CITE = re.compile(r"^([a-z_][a-z0-9_]*\.py):((?:\d+)(?:[-,]\d+)*)$")

#: `DatastreamRecoveryDialog.tsx:99`, `ProjectSettings.tsx:30,644`.
_TSX_CITE = re.compile(r"^([A-Za-z][A-Za-z0-9_.-]*\.tsx):((?:\d+)(?:[-,]\d+)*)$")

#: `(`explicit`)`, `(`mounted`)`, `(`blanket:governance`)`.
_ANNOTATION = re.compile(r"\(`(explicit|mounted|blanket:[a-z-]+)`")

#: `+ 9 tabs`, `+ 5 tabs per object type`.
_TABS = re.compile(r"\+\s*(\d+)\s*tabs(\s+per object type)?")

#: An MCP tool NAME, as opposed to the lens names, module names, field names and
#: reason strings the same cell also puts in backticks. A backticked identifier
#: is read as a tool citation when the LIVING catalog knows it, or when it
#: carries this shape and the catalog does not — that second half is the drift
#: case, a tool renamed or retired out from under the matrix.
#:
#: THE HOLE THIS LEAVES, named rather than left to be found: a ONE-SEGMENT tool
#: retired from the catalog reads as prose and is not reported. There is one
#: such tool today (`SINGLE_SEGMENT_TOOLS`), the guard pins the set, and closing
#: the hole by matching every bare word would make `datastreams` — a lens named
#: in the same cell — a phantom tool citation, which is worse.
_TOOL_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")

#: The registered tools `_TOOL_NAME` cannot see, measured on 2026-09-02 against
#: `mcp_profiles.registered_declarations()`. Pinned so the set's growth is a
#: finding rather than a silence.
SINGLE_SEGMENT_TOOLS: frozenset[str] = frozenset({"health"})

#: A row states its own missing plane in the state cell: `**MCP plane missing**`.
_PLANE_MISSING = re.compile(r"\*\*(COMPONENT|API|UI|MCP) plane missing", re.I)

#: The three states §5 defines, and no fourth.
_STATE = re.compile(r"^`(delivered|partial|absent)`")

#: A line of a module that declares routes. Checked in a small window around the
#: cited line, because a citation points at a block and blocks have a first line.
_ROUTE_MARKER = re.compile(r"Route\(|endpoint=|methods=|_ROOT\b|_BASE\b|ROUTES\s*=|\"/api/|f\"\{")
_ROUTE_WINDOW = 3

#: `**Matrix — 4 rows: 3 `delivered`, 1 `partial`, 0 `absent`.**`
_LOT_COUNTS = re.compile(
    r"\*\*Matrix\s*[—-]\s*(\d+) rows:\s*(\d+) `delivered`,\s*(\d+) `partial`,\s*(\d+) `absent`"
)

#: `**What the four matrices add up to — 18 gestures, 8 `delivered`, 10 `partial`, 0 `absent`.**`
_TOTAL_COUNTS = re.compile(
    r"add up to\s*[—-]\s*(\d+) gestures,\s*(\d+) `delivered`,\s*(\d+) `partial`,"
    r"\s*(\d+) `absent`"
)

#: The floors. Each answers one question: *if this parse read nothing, would
#: anything be false?* A parse that finds no row reports a perfect matrix, which
#: is criterion 13 of `module-boundaries.md` and the reason
#: `element_inventory.FLOORS` exists. Deliberately below the measured figures
#: (4 lots / 18 rows / 63 fragments / 47 tool citations on 2026-09-02) — a floor
#: is not a budget, it exists to fail on an empty scan.
FLOORS = {
    # 2026-09-05: lots 1 and 2 gained their matrices, so an empty scan now has
    # six lots and twenty-seven rows to miss rather than four and eighteen.
    "lots": 6,
    "rows": 18,
    "route_fragments": 45,
    "tool_citations": 35,
    "page_addresses": 16,
}

#: THE RATCHET. The rows of §5 that did not match the code on 2026-09-02, the day
#: this instrument was written. It turns ONE WAY: a row that comes loose from the
#: code is refused at the moment it comes loose, and a row repaired here must be
#: deleted from this set — a stale line is refused too, so the baseline cannot
#: quietly become a list of things nobody looks at.
#:
#: This document is not this session's to rewrite: §5's cells are Jean's
#: derivation and the one accepted line is a documentation repair, not a code
#: one. It is written here rather than left un-gated so that the OTHER
#: seventeen rows are held from today.
#:
#:   1  2026-09-02  `analyze/renders` is written `+ 2 tabs` and the registry has
#:                  declared THREE since 34fe2f91 (2026-08-12) --
#:                  `tabs: ["result", "evidence", "sharing"]` in
#:                  `ui/admin/src/shell/navigation/analyze.ts:133`. The matrix was
#:                  written on 2026-09-01, so this cell was wrong on the day it was
#:                  derived, never drifted into being wrong. The uncounted tab is
#:                  `sharing` -- the very verb that row calls console-only, which is
#:                  why it is worth a line rather than a silent correction.
#:                  `grep -n 'tabs:' ui/admin/src/shell/navigation/analyze.ts`
ACCEPTED_ON_2026_09_02: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- #
# What the document writes
# --------------------------------------------------------------------------- #


@dataclass
class Plane:
    """One of the four planes of §1.3, for one gesture."""

    name: str
    green: bool
    detail: str = ""


@dataclass
class Row:
    """One gesture of one matrix, as written and as derived."""

    lot: int
    line: int
    gesture: str
    written_state: str
    names_missing_plane: str  # "", or the plane the state cell names as missing
    addresses: list[str] = field(default_factory=list)
    annotation: str = ""
    tabs_written: int | None = None
    tabs_per_type: bool = False
    route_fragments: list[str] = field(default_factory=list)
    server_citations: list[tuple[str, list[int]]] = field(default_factory=list)
    mount_citations: list[int] = field(default_factory=list)
    component_citations: list[tuple[str, list[int]]] = field(default_factory=list)
    #: Every backticked identifier of the MCP cell, before the catalog says
    #: which of them are tools. Kept apart so the classification happens against
    #: the living registry and not against a shape alone.
    mcp_tokens: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    claims_no_tool: bool = False

    planes: dict[str, Plane] = field(default_factory=dict)
    derived_state: str = ""
    findings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def identity(self) -> str:
        return f"lot {self.lot}, user-bridge.md:{self.line}"

    @property
    def key(self) -> str:
        """The identity the ratchet is keyed by. Deliberately NOT the line number:
        a paragraph added above §5 moves every row and would stale the whole
        baseline on an edit that changed no matrix cell."""
        return f"lot {self.lot} / {self.gesture[:48]}"


@dataclass
class Lot:
    number: int
    line: int
    written_rows: int
    written_delivered: int
    written_partial: int
    written_absent: int
    rows: list[Row] = field(default_factory=list)


def improvement_row_two(path: Path = DOC) -> str:
    """§6's row about the four planes, verbatim.

    It is the row `user-bridge[1]` is keyed to — *tracer les quatre plans des
    capacités Datastream · 1 verte sur 11 · `capability-trace`* — and its figure
    is at a DIFFERENT grain from this census: the eleven are the owner's verbatim
    journey sentences, kept in `.claude/skills/capability-trace/SKILL.md`, and no
    table in this repository maps one of them to a surface. Printing the row
    beside the derived count is the honest way to relate them: this census is the
    standing measure that row asked for, at the grain the document does carry,
    and it does not re-derive the eleven.
    """
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("|") and "quatre plans" in line:
            return line.strip()
    return ""


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split(" | ")]


def _backticked(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def parse_document(path: Path = DOC) -> tuple[list[Lot], tuple[int, int, int, int] | None]:
    """The four matrices of §5, and the total line that closes them.

    The document is PARSED, never paraphrased: a census that carried its own copy
    of the eighteen gestures would measure its copy, and the copy would age the
    way the matrix aged.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        # SINCE 2026-09-05 THE PARSE STARTS AT SECTION 3, NOT 5. Lots 1 and 2 are
        # VERBATIM journeys with no matrix until that day: the gap audit measured
        # their nine gestures as neither green nor red -- not measured at all --
        # and each now carries a `### Lot N` matrix of its own, derived like the
        # others. Reading from `## 5.` would keep certifying eighteen gestures
        # while twenty-seven are written.
        start = next(i for i, line in enumerate(lines) if line.startswith("## 3."))
        end = next(i for i, line in enumerate(lines) if i > start and line.startswith("## 6."))
    except StopIteration:  # pragma: no cover - the document lost its section
        return [], None

    lots: list[Lot] = []
    current: Lot | None = None
    in_matrix = False

    for offset, raw in enumerate(lines[start:end]):
        lineno = start + offset + 1
        heading = re.match(r"### Lot (\d+)", raw)
        if heading:
            current = Lot(int(heading.group(1)), lineno, 0, 0, 0, 0)
            lots.append(current)
            in_matrix = False
            continue
        counts = _LOT_COUNTS.search(raw)
        if counts and current is not None:
            current.written_rows = int(counts.group(1))
            current.written_delivered = int(counts.group(2))
            current.written_partial = int(counts.group(3))
            current.written_absent = int(counts.group(4))
            continue
        if raw.startswith("| Gesture |"):
            in_matrix = True
            continue
        if in_matrix and raw.startswith("|---"):
            continue
        if in_matrix:
            if not raw.startswith("|"):
                in_matrix = False
                continue
            if current is not None:
                current.rows.append(_parse_row(current.number, lineno, _cells(raw)))

    body = "\n".join(lines[start:end])
    total = _TOTAL_COUNTS.search(body.replace("\n", " "))
    totals = tuple(int(g) for g in total.groups()) if total else None
    return lots, totals  # type: ignore[return-value]


def _numbers(spec: str) -> list[int]:
    return [int(part) for part in re.split(r"[-,]", spec) if part.isdigit()]


def _parse_row(lot: int, lineno: int, cells: list[str]) -> Row:
    gesture, page, route, tool, state = (cells + [""] * 5)[:5]
    found = _STATE.match(state)
    missing = _PLANE_MISSING.search(state)
    row = Row(
        lot=lot,
        line=lineno,
        gesture=re.sub(r"\*\*|`", "", gesture).strip(),
        written_state=found.group(1) if found else "",
        names_missing_plane=missing.group(1).lower() if missing else "",
    )

    for token in _backticked(page):
        if _ADDRESS.match(token):
            row.addresses.append(token)
            continue
        tsx = _TSX_CITE.match(token)
        if tsx:
            row.component_citations.append((tsx.group(1), _numbers(tsx.group(2))))
    annotation = _ANNOTATION.search(page)
    row.annotation = annotation.group(1) if annotation else ""
    tabs = _TABS.search(page)
    if tabs:
        row.tabs_written = int(tabs.group(1))
        row.tabs_per_type = bool(tabs.group(2))
    for name, spec in re.findall(r"`([a-z_][a-z0-9_]*\.py):((?:\d+)(?:[-,]\d+)*)`", page):
        row.server_citations.append((name, _numbers(spec)))

    for token in _backticked(route):
        stripped = _VERB.sub("", token).strip()
        cite = _PY_CITE.match(stripped)
        if cite:
            if cite.group(1) == "admin_api.py":
                row.mount_citations.extend(_numbers(cite.group(2)))
            else:
                row.server_citations.append((cite.group(1), _numbers(cite.group(2))))
            continue
        if stripped.startswith("...") or stripped.startswith("/"):
            row.route_fragments.append(_HOLE.sub("{}", stripped.lstrip(".")))

    row.claims_no_tool = tool.lstrip().startswith("**none.**")
    row.mcp_tokens = [
        token
        for token in _backticked(tool)
        if re.fullmatch(r"[a-z][a-z0-9_]*", token) and not token.endswith("_mcp")
    ]
    return row


# --------------------------------------------------------------------------- #
# The living sources
# --------------------------------------------------------------------------- #


@dataclass
class Sources:
    """Everything the derivation reads, booted once."""

    screens: dict[str, object]
    mounted_files: set[str]
    reachable: set[str]
    object_components: dict[str, list[str]]
    routes: set[str]
    route_modules: set[str]
    declared_tools: set[str]
    wire_tools: set[str]
    app_only_tools: set[str]
    tools_measured: bool = True


def _closure(front: list[str]) -> set[str]:
    """Every local module a set of components pulls in, transitively.

    `screens.local_imports` owns the import parse; walking it here is a traversal
    of that reader, not a second copy of it.
    """
    seen: set[str] = set()
    stack = [f for f in front if f.endswith((".tsx", ".ts"))]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(screens.local_imports(current))
    return seen


def _draws_from_the_library(rel: str) -> bool:
    """Does this file import anything from the shared visual vocabulary?"""
    path = ROOT / rel
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for spec in re.findall(r'from\s+"([^"]+)"', text):
        if not spec.startswith("."):
            continue
        base = path.parent / spec
        for candidate in (
            base.with_suffix(".tsx"),
            base.with_suffix(".ts"),
            base / "index.ts",
            base / "index.tsx",
        ):
            if candidate.exists():
                resolved = candidate.resolve().relative_to(ROOT).as_posix()
                if resolved.startswith(SHARED_VOCABULARY):
                    return True
    return False


def collect(with_tools: bool = True) -> Sources:
    resolved = {screen.id: screen for screen in screens.resolve()}
    paths = screens.component_paths()
    explicit_mounts, blanket_mounts = screens.object_mounts()

    object_components: dict[str, list[str]] = {}
    mounted_files: set[str] = set()
    for screen in resolved.values():
        if screen.mounted_at != "NOT MOUNTED":
            mounted_files.update(f for f in screen.front if f.endswith((".tsx", ".ts")))
    for owner, names in list(explicit_mounts.items()) + list(blanket_mounts.items()):
        files = []
        for name in names:
            spec = paths.get(name)
            found = screens._resolve_import(spec) if spec else None
            if found:
                files.append(found)
        object_components[owner] = files
        mounted_files.update(files)

    route_elements = element_inventory.route_elements()
    routes = {element.address for element in route_elements}
    route_modules = {element.surface for element in route_elements}

    declared: set[str] = set()
    wire: set[str] = set()
    app_only: set[str] = set()
    if with_tools:
        if str(SERVER) not in sys.path:
            sys.path.insert(0, str(SERVER))
        import mcp_tool_surface_report as tools  # noqa: PLC0415

        report = tools._collect()
        wire = set(report["wire_tool_names"])
        app_only = set(report["app_only_tools"])
        from core import mcp_profiles  # noqa: PLC0415

        declared = {d.name for d in mcp_profiles.registered_declarations()}

    return Sources(
        screens=resolved,
        mounted_files=mounted_files,
        reachable=_closure(sorted(mounted_files)),
        object_components=object_components,
        routes=routes,
        route_modules=route_modules,
        declared_tools=declared,
        wire_tools=wire,
        app_only_tools=app_only,
        tools_measured=with_tools,
    )


# --------------------------------------------------------------------------- #
# The four planes, per gesture
# --------------------------------------------------------------------------- #


def _resolves(fragment: str, routes: set[str]) -> bool:
    """A fragment resolves when a mounted address ends with it, or carries it as
    a whole path segment — the matrix cites leaf endpoints AND family roots
    (`/rule-sets`), and a root is a real citation of a mounted family."""
    return any(path.endswith(fragment) or f"{fragment}/" in path for path in routes)


def _find_ui_file(basename: str) -> str | None:
    for path in UI_SRC.rglob(basename):
        return path.resolve().relative_to(ROOT).as_posix()
    return None


def _ui_plane(row: Row, sources: Sources) -> tuple[Plane, list[str], list[str]]:
    """Mounted, reachable, and — for a Level 3 address — answered and declared."""
    findings: list[str] = []
    notes: list[str] = []
    green = False

    for address in row.addresses:
        section, _, deeper = address.partition("/o/")
        screen = sources.screens.get(section)
        if screen is None:
            findings.append(f"page `{address}`: navigation declares no section `{section}`")
            continue
        if screen.mounted_at == "NOT MOUNTED":
            findings.append(f"page `{address}`: `{section}` is declared and NOT MOUNTED")
            continue
        if deeper:
            object_type, _, tab = deeper.partition("/")
            declared = {o["type"]: o for o in screen.objects}
            entry = declared.get(object_type)
            if entry is None:
                findings.append(
                    f"page `{address}`: `{section}` no longer declares the object "
                    f"type `{object_type}`"
                )
                continue
            if entry["answered"] == "UNANSWERED":
                findings.append(
                    f"page `{address}`: `{object_type}` is declared and no branch answers it"
                )
                continue
            if tab and tab not in entry["tabs"]:
                findings.append(
                    f"page `{address}`: `{object_type}` no longer declares the tab `{tab}` "
                    f"(it declares {sorted(entry['tabs'])})"
                )
                continue
        green = True

        if row.annotation and row.annotation != "mounted":
            answered = sorted({o["answered"] for o in screen.objects}) or ["(no object type)"]
            if [row.annotation] != answered:
                findings.append(
                    f"page `{address}`: the matrix writes `{row.annotation}`, the registry "
                    f"answers {answered}"
                )
        if row.tabs_written is not None:
            per_type = sorted({len(o["tabs"]) for o in screen.objects})
            total = sum(len(o["tabs"]) for o in screen.objects)
            if row.tabs_per_type:
                if per_type != [row.tabs_written]:
                    findings.append(
                        f"page `{address}`: the matrix writes `+ {row.tabs_written} tabs per "
                        f"object type`, the registry declares {per_type} per type"
                    )
            elif total != row.tabs_written:
                findings.append(
                    f"page `{address}`: the matrix writes `+ {row.tabs_written} tabs`, the "
                    f"registry declares {total}"
                )

    for basename, numbers in row.component_citations:
        found = _find_ui_file(basename)
        if found is None:
            findings.append(f"component `{basename}` cited by the matrix does not exist")
            continue
        length = len((ROOT / found).read_text(encoding="utf-8", errors="replace").splitlines())
        for number in numbers:
            if number > length:
                findings.append(
                    f"component `{basename}:{number}`: the file holds {length} lines"
                )
        if not row.addresses:
            if found in sources.reachable:
                green = True
                notes.append(f"reached from a mounted screen: {found}")
            else:
                findings.append(
                    f"component `{basename}` is cited as the surface of this gesture and no "
                    f"mounted screen reaches it"
                )

    if not row.addresses and not row.component_citations:
        findings.append("no page address and no component: this gesture names no surface")

    return Plane("ui", green), findings, notes


def _component_plane(row: Row, sources: Sources) -> tuple[Plane, list[str]]:
    """Does the surface draw from the shared library, and how much of it does?"""
    front: list[str] = []
    for address in row.addresses:
        section, _, deeper = address.partition("/o/")
        screen = sources.screens.get(section)
        if screen is None:
            continue
        front.extend(f for f in screen.front if f.endswith((".tsx", ".ts")))
        if deeper:
            front.extend(sources.object_components.get(deeper.partition("/")[0], []))
    for basename, _ in row.component_citations:
        found = _find_ui_file(basename)
        if found:
            front.append(found)
    if not front:
        return Plane("component", False, "no component resolves for this gesture"), []

    own = {f for f in _closure(front) if not f.startswith(SHARED_VOCABULARY)}
    drawing = sorted(f for f in own if _draws_from_the_library(f))
    detail = f"{len(drawing)} of {len(own)} own files import the shared vocabulary"
    if not drawing:
        return (
            Plane("component", False, detail),
            [
                "component plane: not one file of this surface imports "
                "`ui/admin/src/ui/` or `ui/admin/src/components/ui/` — the page draws "
                "its own vocabulary (§1.4)"
            ],
        )
    return Plane("component", True, detail), []


def _api_plane(row: Row, sources: Sources) -> tuple[Plane, list[str], list[str]]:
    findings: list[str] = []
    notes: list[str] = []

    unresolved = [f for f in row.route_fragments if not _resolves(f, sources.routes)]
    for fragment in unresolved:
        findings.append(f"route `{fragment}`: no address the composed router mounts carries it")

    for name, numbers in row.server_citations:
        path = next(SERVER.rglob(name), None)
        if path is None:
            findings.append(f"server module `{name}` cited by the matrix does not exist")
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        module = name[: -len(".py")]
        for number in numbers:
            if number > len(lines):
                findings.append(f"`{name}:{number}`: the file holds {len(lines)} lines")
                continue
            if module not in sources.route_modules:
                continue
            low = max(0, number - 1 - _ROUTE_WINDOW)
            window = "\n".join(lines[low: number + _ROUTE_WINDOW])
            if not _ROUTE_MARKER.search(window):
                findings.append(
                    f"`{name}:{number}` is cited as a route declaration and no longer "
                    f"lands in one"
                )
        if module not in sources.route_modules and row.route_fragments:
            notes.append(f"`{name}` contributes no address to the composed router")

    green = bool(row.route_fragments) and not unresolved
    return Plane("api", green, f"{len(row.route_fragments)} fragment(s) cited"), findings, notes


def _mcp_plane(row: Row, sources: Sources) -> tuple[Plane, list[str]]:
    if not sources.tools_measured:
        row.tool_names = [t for t in row.mcp_tokens if _TOOL_NAME.fullmatch(t)]
        return Plane("mcp", False, "not measured (--no-tools)"), []

    row.tool_names = [
        token
        for token in row.mcp_tokens
        if token in sources.declared_tools or _TOOL_NAME.fullmatch(token)
    ]
    findings: list[str] = []
    for name in row.tool_names:
        if name not in sources.declared_tools:
            findings.append(f"MCP tool `{name}` is cited by the matrix and is not registered")

    on_the_wire = [
        name
        for name in row.tool_names
        if name in sources.declared_tools and name not in sources.app_only_tools
    ]
    green = bool(on_the_wire) and not row.claims_no_tool and row.names_missing_plane != "mcp"
    detail = f"{len(on_the_wire)} of {len(row.tool_names)} cited tool(s) reach a model"
    return Plane("mcp", green, detail), findings


def _state_from(planes: dict[str, Plane]) -> str:
    """§5's own rule, applied to the two planes its three states are defined on.

    `delivered` — a person reaches the database from a mounted screen AND a model
    does the same through a declared tool; `partial` — one of the two; `absent` —
    neither. The component and api planes are carried beside them because §1.3
    counts four, and a row cannot be `delivered` while one of them is missing.
    """
    person = planes["ui"].green and planes["api"].green
    model = planes["mcp"].green
    if person and model:
        return "delivered"
    if person or model:
        return "partial"
    return "absent"


def derive(row: Row, sources: Sources) -> Row:
    ui, ui_findings, ui_notes = _ui_plane(row, sources)
    component, component_findings = _component_plane(row, sources)
    api, api_findings, api_notes = _api_plane(row, sources)
    mcp, mcp_findings = _mcp_plane(row, sources)

    row.planes = {p.name: p for p in (component, api, ui, mcp)}
    row.derived_state = _state_from(row.planes) if sources.tools_measured else ""
    row.findings = ui_findings + component_findings + api_findings + mcp_findings
    row.notes = ui_notes + api_notes

    if not sources.tools_measured:
        # The MCP plane is half of every state §5 defines. Comparing states with
        # that half unmeasured would report eight false disagreements — the shape
        # `element_inventory` already refuses when `--no-tools` skips its tool
        # floor: an unmeasured plane must never read as a collapsed one.
        return row

    if row.written_state and row.written_state != row.derived_state:
        row.findings.append(
            f"the matrix writes `{row.written_state}`; the four planes derive "
            f"`{row.derived_state}` "
            f"(component={component.green}, api={api.green}, ui={ui.green}, mcp={mcp.green})"
        )
    if row.written_state == "delivered" and not component.green:
        row.findings.append(
            "the matrix writes `delivered` and the component plane is missing — §1.3 "
            "counts four planes, and green on three delivers nothing"
        )
    if row.written_state == "partial" and not row.names_missing_plane:
        row.findings.append(
            "the matrix writes `partial` and the row names no missing plane — §5 requires "
            "the missing plane to be named in the row"
        )
    if row.written_state == "absent" and any(p.green for p in row.planes.values()):
        green = sorted(name for name, plane in row.planes.items() if plane.green)
        row.findings.append(
            f"the matrix writes `absent` and {green} derive green — an `absent` row's "
            f"command is the search that found nothing"
        )
    if not any(p.green for p in row.planes.values()):
        row.findings.append(
            "no plane of this gesture derives green: the matrix names a gesture no "
            "surface carries"
        )
    return row


# --------------------------------------------------------------------------- #
# The mount citations, reported and not gated
# --------------------------------------------------------------------------- #


def mount_anchors() -> dict[str, list[int]]:
    """Where `admin_api.py` really splices each module's routes, today.

    Derived from the file rather than pinned: the import line names the symbols a
    module exports, and the lines that spread those symbols into the router are
    the splice. The cited line is printed against these; see the header for why
    this half is reported and not gated.
    """
    path = SERVER / "core" / "admin_api.py"
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    text = "\n".join(lines)
    anchors: dict[str, list[int]] = {}
    for match in re.finditer(
        r"^from core\.([a-z_]+) import (?:\(([^)]*)\)|(.+))$", text, re.M
    ):
        module = match.group(1)
        body = match.group(2) or match.group(3) or ""
        symbols = {
            (part.split(" as ")[-1] if " as " in part else part).strip().rstrip(",")
            for part in re.split(r"[,\n]", body)
            if part.strip()
        }
        symbols = {s for s in symbols if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", s)}
        if not symbols:
            continue
        # Past the END of the import statement: a parenthesised import spans
        # several lines and every one of them names a symbol, so measuring from
        # its first line files the import block itself as a splice.
        after = text[: match.end()].count("\n") + 1
        found = [
            number
            for number, line in enumerate(lines, start=1)
            if number > after and any(re.search(rf"\*?\b{s}\b", line) for s in symbols)
        ]
        if found:
            anchors[module] = found
    return anchors


def mount_drift(lots: list[Lot]) -> list[str]:
    anchors = mount_anchors()
    out: list[str] = []
    for lot in lots:
        for row in lot.rows:
            if not row.mount_citations:
                continue
            modules = [name[: -len(".py")] for name, _ in row.server_citations]
            derived = sorted({n for m in modules for n in anchors.get(m, [])})
            stale = [n for n in row.mount_citations if n not in derived]
            if stale and derived:
                out.append(
                    f"{row.identity}: cites `admin_api.py:"
                    f"{','.join(str(n) for n in stale)}` as the mount; the router splices "
                    f"{modules} at {derived}"
                )
    return out


# --------------------------------------------------------------------------- #
# The census
# --------------------------------------------------------------------------- #


@dataclass
class Census:
    lots: list[Lot]
    totals: tuple[int, int, int, int] | None
    sources: Sources

    @property
    def rows(self) -> list[Row]:
        return [row for lot in self.lots for row in lot.rows]

    def counts(self) -> dict[str, int]:
        rows = self.rows
        return {
            "lots": len(self.lots),
            "rows": len(rows),
            "route_fragments": sum(len(r.route_fragments) for r in rows),
            "tool_citations": sum(len(r.tool_names) for r in rows),
            "page_addresses": sum(len(r.addresses) for r in rows),
        }

    def plane_counts(self) -> dict[str, int]:
        out = {"component": 0, "api": 0, "ui": 0, "mcp": 0}
        for row in self.rows:
            for name, plane in row.planes.items():
                if plane.green:
                    out[name] += 1
        return out

    def state_counts(self, derived: bool = True) -> dict[str, int]:
        out = {"delivered": 0, "partial": 0, "absent": 0}
        for row in self.rows:
            key = row.derived_state if derived else row.written_state
            if key in out:
                out[key] += 1
        return out

    def four_plane_green(self) -> list[Row]:
        return [row for row in self.rows if all(p.green for p in row.planes.values())]

    def collapsed(self) -> list[str]:
        counts = self.counts()
        return [
            f"the parse found {counts.get(name, 0)} {name}, floor is {floor}: §5 moved and "
            f"this census read (almost) nothing."
            for name, floor in FLOORS.items()
            if counts.get(name, 0) < floor
        ]

    def count_disagreements(self) -> list[str]:
        out: list[str] = []
        for lot in self.lots:
            written = (
                lot.written_rows,
                lot.written_delivered,
                lot.written_partial,
                lot.written_absent,
            )
            states = {"delivered": 0, "partial": 0, "absent": 0}
            for row in lot.rows:
                if row.written_state in states:
                    states[row.written_state] += 1
            actual = (len(lot.rows), states["delivered"], states["partial"], states["absent"])
            if written != actual:
                out.append(
                    f"lot {lot.number} (user-bridge.md:{lot.line}): the count line writes "
                    f"{written[0]} rows / {written[1]} delivered / {written[2]} partial / "
                    f"{written[3]} absent; the table holds {actual[0]} / {actual[1]} / "
                    f"{actual[2]} / {actual[3]}"
                )
        if self.totals is not None:
            states = self.state_counts(derived=False)
            actual = (
                len(self.rows),
                states["delivered"],
                states["partial"],
                states["absent"],
            )
            if tuple(self.totals) != actual:
                out.append(
                    f"the closing total writes {self.totals}; the four tables hold {actual}"
                )
        elif self.lots:
            out.append("the closing total line of §5 is gone: nothing sums the four matrices")
        return out

    def findings(self) -> list[str]:
        out: list[str] = []
        for row in self.rows:
            for finding in row.findings:
                out.append(f"{row.identity}: {finding}")
        return out

    def finding_keys(self) -> set[str]:
        """The same findings, keyed the way the ratchet keys them."""
        return {f"{row.key} :: {finding}" for row in self.rows for finding in row.findings}


def build(with_tools: bool = True) -> Census:
    lots, totals = parse_document()
    sources = collect(with_tools=with_tools)
    for lot in lots:
        for row in lot.rows:
            derive(row, sources)
    return Census(lots=lots, totals=totals, sources=sources)


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def _mark(plane: Plane) -> str:
    return "green" if plane.green else "  -  "


def _rows_report(census: Census) -> None:
    print()
    print("  comp   api    ui     mcp    written    derived    gesture")
    for lot in census.lots:
        print(f"  -- lot {lot.number} " + "-" * 60)
        for row in lot.rows:
            planes = " ".join(_mark(row.planes[n]) for n in ("component", "api", "ui", "mcp"))
            flag = " " if row.written_state == row.derived_state else "!"
            print(
                f"  {planes}  {row.written_state:9s}{flag} {row.derived_state:9s}  "
                f"{row.gesture[:46]}"
            )
    print()


def _report(census: Census) -> None:
    counts = census.counts()
    planes = census.plane_counts()
    rows = len(census.rows)
    print()
    print("  The four planes of `user-bridge.md` §5, derived. Grain: one gesture.")
    print()
    print(f"    gestures the four matrices write   : {rows}  (in {counts['lots']} lots)")
    print(f"    page addresses cited               : {counts['page_addresses']}")
    print(f"    route fragments cited              : {counts['route_fragments']}")
    print(f"    MCP tool names cited               : {counts['tool_citations']}")
    print()
    print("  Green, per plane of §1.3:")
    print(f"    COMPONENT  the surface draws from the shared library : "
          f"{planes['component']}/{rows}")
    print(f"    API        the router mounts every cited address     : {planes['api']}/{rows}")
    print(f"    UI         declared, mounted, the tab exists         : {planes['ui']}/{rows}")
    if census.sources.tools_measured:
        print(f"    MCP        a registered tool reaches a model         : {planes['mcp']}/{rows}")
    else:
        print("    MCP        (skipped: --no-tools)")
    print()
    if census.sources.tools_measured:
        green = census.four_plane_green()
        print(f"  GREEN ON ALL FOUR: {len(green)} of {rows}.")
        for row in green:
            print(f"    lot {row.lot}  {row.gesture[:64]}")
        print("  §1.3: a capability green on three planes delivers nothing. This count is")
        print("  the standing measure §6 row 2 asks for, at the grain of §5's gestures.")
        row_two = improvement_row_two()
        if row_two:
            print()
            print(f"  §6 row 2, verbatim: {row_two}")
            print("  Its `1 verte sur 11` is a DIFFERENT grain — the owner's eleven verbatim")
            print("  journey sentences, kept in `.claude/skills/capability-trace/SKILL.md`,")
            print("  which no table in this repository maps to a surface. This census does")
            print("  not re-derive them, and saying so is cheaper than implying it did.")

        written = census.state_counts(derived=False)
        derived = census.state_counts(derived=True)
        print()
        print("  States, as written and as derived:")
        for state in ("delivered", "partial", "absent"):
            print(f"    {state:10s} written {written[state]:3d}   derived {derived[state]:3d}")
    else:
        print("  GREEN ON ALL FOUR: not measurable with --no-tools — the MCP plane is one")
        print("  of the four, and reporting a count without it would report ten gaps that")
        print("  were never looked for. Drop the flag for the standing count.")

    disagreements = census.count_disagreements()
    findings = census.findings()
    collapsed = census.collapsed()
    if findings:
        print(f"\n  THE DOCUMENT AND THE CODE DISAGREE ({len(findings)}):")
        for line in findings:
            print(f"    - {line}")
    else:
        print("\n  Every citation of the four matrices still resolves.")
    for line in disagreements:
        print(f"\n  COUNT LINE: {line}")
    for line in collapsed:
        print(f"\n  COLLAPSED: {line}")

    drift = mount_drift(census.lots)
    if drift:
        print(f"\n  REPORTED, NOT GATED — mount citations that moved ({len(drift)}):")
        for line in drift:
            print(f"    - {line}")
        print("  `admin_api.py` is one router every neighbouring story edits; the anchor is")
        print("  the routes symbol, never the line. Repair the cells, not the code.")

    notes = [f"{r.identity}: {n}" for r in census.rows for n in r.notes]
    if notes:
        print(f"\n  Notes ({len(notes)}):")
        for line in notes:
            print(f"    - {line}")
    print()


def _gate(census: Census) -> int:
    keys = census.finding_keys()
    new = sorted(keys - ACCEPTED_ON_2026_09_02)
    gone = sorted(ACCEPTED_ON_2026_09_02 - keys)
    structural = census.count_disagreements() + census.collapsed()

    counts = census.counts()
    planes = census.plane_counts()
    print(
        "user-bridge census: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + "; green "
        + ", ".join(f"{k}={v}" for k, v in sorted(planes.items()))
        + (
            ""
            if census.sources.tools_measured
            else "  (mcp plane not measured: --no-tools)"
        )
    )
    print(
        f"  citations that no longer resolve: {len(keys)} "
        f"(accepted on 2026-09-02: {len(ACCEPTED_ON_2026_09_02)})"
    )

    if new:
        print(f"\nREFUSED: {len(new)} NEW row(s) of §5 no longer match the code.")
        print("A row of §5 is a DERIVATION. When its citation stops resolving it becomes")
        print("an assertion — the defect the `DERIVED` status of that section exists to")
        print("prevent. Repair the row against the code, or the code against the row;")
        print("never this script, which only compares them.")
        for line in new:
            print(f"  {line}")
    if gone:
        print(f"\nREFUSED: {len(gone)} accepted disagreement(s) no longer hold.")
        print("The ratchet turns one way: delete these from ACCEPTED_ON_2026_09_02 in")
        print("scripts/user_bridge_census.py.")
        for line in gone:
            print(f"  {line}")
    if structural:
        print(f"\nREFUSED: {len(structural)} structural problem(s).")
        for line in structural:
            print(f"  {line}")
    if new or gone or structural:
        return 1
    print("\nOK: no new row of §5 has come loose from the code, no accepted disagreement")
    print("has been repaired without being retired, and no plane of the parse collapsed.")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable")
    parser.add_argument("--rows", action="store_true", help="one line per gesture")
    parser.add_argument("--gate", action="store_true", help="the written matrix vs this derivation")
    parser.add_argument("--no-tools", action="store_true", help="skip the MCP app boot")
    args = parser.parse_args(argv)

    census = build(with_tools=not args.no_tools)
    if args.gate:
        return _gate(census)
    if args.json:
        print(
            json.dumps(
                {
                    "counts": census.counts(),
                    "planes_green": census.plane_counts(),
                    "states_written": census.state_counts(derived=False),
                    "states_derived": census.state_counts(derived=True),
                    "green_on_all_four": [r.gesture for r in census.four_plane_green()],
                    "rows": [asdict(row) for lot in census.lots for row in lot.rows],
                    "findings": census.findings(),
                    "count_disagreements": census.count_disagreements(),
                    "collapsed": census.collapsed(),
                    "mount_citations_that_moved": mount_drift(census.lots),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    if args.rows:
        _rows_report(census)
        return 0
    _report(census)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
