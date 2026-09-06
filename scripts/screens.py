#!/usr/bin/env python
"""screens.py -- the closed improvement loop over every Level 2 screen.

This tool writes NO documentation. Everything a screen needs already exists in
this repo; what was missing is the LINK between the three places and a way to
run it locally. So each screen resolves to four exact paths, and one command:

    SPEC   docs/product-architecture/<surface>.md  + its `Incomplete if` list
           + the recorded verdicts in completeness-ledger.json
    FRONT  the component ContentRouter actually mounts for that route
    TEST   the vitest files that name that component
    RUN    cd ui/admin && npx vitest run <those files>

The loop is circular because the RUN result is what selects the next screen to
work on, and every turn appends a row to history.jsonl. Improvement is the
delta between two rows -- never a claim.

    RESOLVE --> TEST --> BOARD --> PICK --> (fix the front) --+
       ^                                                      |
       +------------------------------------------------------+

Commands:
    python scripts/screens.py resolve          # rebuild the link table
    python scripts/screens.py work <id>        # the work order for one screen
    python scripts/screens.py test [--id X]    # run the local tests, record
    python scripts/screens.py board            # regenerate screens/board.md
    python scripts/screens.py next             # the one screen to fix now
    python scripts/screens.py cycle [--push]   # one full turn of the wheel
    python scripts/screens.py orphans          # doors the server opens, no screen pushes
    python scripts/screens.py sync             # mirror the board into GH issues
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "ui" / "admin"
NAV = UI / "src" / "shell" / "navigation.ts"

sys.path.insert(0, str(Path(__file__).resolve().parent))

from finished_work_audit import incomplete_if as _audit_incomplete_if  # noqa: E402

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

ROUTER = UI / "src" / "shell" / "router.tsx"
CONTENT = UI / "src" / "shell" / "ContentRouter.tsx"

#: LE ROUTAGE EST EN TROIS FICHIERS depuis AD-42 (2026-08-12) : `ContentRouter`
#: assemble, `objectSurfaces` tient le niveau 3 et `collectionSurfaces` le
#: niveau 2. Un garde epingle sur UN chemin devient faux EN SILENCE le jour ou
#: le code demenage -- mesure ce jour-la sur le script voisin : 0 -> 34 objets
#: << rendus par rien >> sans qu un seul ecran ne bouge.
CONTENT_SURFACES = (
    CONTENT,
    UI / "src" / "shell" / "objectSurfaces.tsx",
    UI / "src" / "shell" / "collectionSurfaces.tsx",
)


def read_router() -> str:
    """Les trois fichiers du routage, joints. Une seule source pour ce script."""
    return chr(10).join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in CONTENT_SURFACES
        if path.exists()
    )


def read_navigation() -> str:
    """Le registre entier : `navigation.ts` et les six espaces qu il assemble."""
    shell = CONTENT.parent
    parts = [shell / "navigation.ts", *sorted((shell / "navigation").glob("*.ts"))]
    return chr(10).join(p.read_text(encoding="utf-8") for p in parts if p.exists())

#: LE ROUTAGE EST EN TROIS FICHIERS depuis AD-42 (2026-08-12) : `ContentRouter`
#: assemble, `objectSurfaces` tient le niveau 3 et `collectionSurfaces` le
#: niveau 2. Ce script lisait UN CHEMIN, et un garde epingle sur un chemin
#: devient faux EN SILENCE le jour ou le code demenage -- mesure le meme jour
#: sur le script voisin : 0 -> 34 objets << rendus par rien >> sans qu un seul
#: ecran ne bouge. `read(CONTENT)` lit donc les trois ensemble.
CONTENT_SURFACES = (
    CONTENT,
    UI / "src" / "shell" / "objectSurfaces.tsx",
    UI / "src" / "shell" / "collectionSurfaces.tsx",
)


def read_router() -> str:
    """Les trois fichiers du routage, joints. Une seule source pour ce script."""
    return chr(10).join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in CONTENT_SURFACES
        if path.exists()
    )
ARCH = ROOT / "docs" / "product-architecture"
LEDGER = ARCH / "completeness-ledger.json"
COVERAGE = ROOT / "_bmad-output" / "planning-artifacts" / "desktop-design-story-coverage-2026-07-24.md"

OUT = ROOT / "screens"
BOARD_JSON = OUT / "board.json"
BOARD_MD = OUT / "board.md"
PAGES_MD = OUT / "pages.md"
WIRING_MD = OUT / "wiring.md"
ROUTES_JSON = OUT / "routes.json"
ORPHANS_MD = OUT / "orphans.md"
HISTORY = OUT / "history.jsonl"

# The ONE hand-written mapping in this tool: which ratified document owns which
# surface. It is transcribed from the table in CLAUDE.md section 1, which is the
# authority. A surface with no document says so -- inventing one would be the
# exact drift CLAUDE.md forbids.
SURFACE_DOC = {
    "overview": ["overview.md"],
    "analyze": ["analyze-and-test.md"],
    "test": ["analyze-and-test.md"],
    "data": ["data.md"],
    "governance": ["governance.md"],
    "context-hub": ["context-hub.md"],
    "project-settings": ["project-settings.md"],
    "project-access": ["project-settings.md"],
    "getting-started": ["project-settings.md"],
    "organization-settings": [],
    "account": [],
    "platform": [],
}
# Two screens are covered by a document of their own, finer than their surface.
SECTION_DOC = {
    "data/datastreams": ["datastream-workbench-and-wizard.md"],
    "data/imports": ["file-source-ingestion.md"],
    "context-hub/knowledge-graph": ["context-hub.md"],
}

# The ledger does not key on the document's file name. Reading it as if it did
# reported 14 open criteria on `data/datastreams` when the recorded verdicts were
# sitting under `datastream` all along -- an instrument that fabricates red.
# Measured with: python -c "import json;print(sorted(json.load(open(
#   'docs/product-architecture/completeness-ledger.json')).keys()))"
LEDGER_KEY = {
    "datastream-workbench-and-wizard": "datastream",
}

# How many vitest suites one screen's local run executes. Bounded because the
# four Governance screens match 37 suites each and a per-screen loop would take
# longer than the full suite. The cap is PRINTED wherever a verdict is, never
# implied: `pass` on six of thirty-seven is not `pass`.
RUN_CAP = 6


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _exe(name: str) -> str:
    """Resolve a launcher to a real path.

    On Windows `npx`, `pnpm` and `gh` are `.cmd` shims: `subprocess.run` with
    `shell=False` cannot find them by bare name and returns `not found`, which
    reads exactly like a broken tool. Thirty-six screens reported `fail` for
    that reason once — a measurement instrument that fabricates red is worse
    than none, so the resolution is explicit here."""
    found = shutil.which(name)
    if found:
        return found
    for ext in (".cmd", ".exe", ".bat"):
        found = shutil.which(name + ext)
        if found:
            return found
    return name


def run(cmd: list[str], cwd: Path, timeout: int = 1200) -> tuple[int, str]:
    cmd = [_exe(cmd[0]), *cmd[1:]]
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, shell=False)
        return p.returncode, p.stdout + p.stderr
    except FileNotFoundError:
        return 127, f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


# ---------------------------------------------------------------------------
# 1. The site structure, read from the router. Never typed.
# ---------------------------------------------------------------------------


@dataclass
class Screen:
    id: str
    title: str
    scope: str
    surface: str
    section: str
    route: str
    spec: list = field(default_factory=list)
    criteria: list = field(default_factory=list)
    front: list = field(default_factory=list)
    mounted_at: str = ""
    tests: list = field(default_factory=list)
    run_cmd: str = ""
    verdict: str = "untested"
    detail: str = ""
    # The backward direction: object types this section PROMISES, and whether
    # ContentRouter has a branch that answers each one.
    objects: list = field(default_factory=list)


def _balanced(text: str, start: int) -> int:
    depth, i = 0, start
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _workspace_regions() -> list[tuple[str, str]]:
    """(source, cle) pour chaque espace, dans l ordre du registre assemble.

    AD-42 : le registre est en sept fichiers. Le parcours d avant DECOUPAIT un
    seul texte sur la cle voisine -- et ne trouvait plus rien une fois les
    espaces separes, EN SILENCE : 31 types d objets declares etaient devenus 0
    pendant que l audit annoncait toujours << 0 rendus par rien >>. Un fichier
    absent est une erreur ; une tranche vide ne l est pas.
    """
    shell = CONTENT.parent
    assembly = (shell / "navigation.ts").read_text(encoding="utf-8")
    order = re.search(r"export const WORKSPACES = \[(.*?)\]", assembly, re.S)
    names = re.findall(r"^\s*(\w+),", order.group(1), re.M) if order else []
    regions: list[tuple[str, str]] = []
    for name in names:
        path = shell / "navigation" / f"{name}.ts"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        key = re.search(r'key: "([\w-]+)"', text)
        if key:
            regions.append((text, key.group(1)))
    return regions


def workspaces() -> list[Screen]:
    src = read_navigation()
    out: list[Screen] = []
    for region, ws in _workspace_regions():
        calls = [m for m in re.finditer(r'section\(\s*"([\w-]+)"\s*,\s*"([^"]+)"', region)]
        for j, m in enumerate(calls):
            stop = calls[j + 1].start() if j + 1 < len(calls) else len(region)
            call = region[m.start():stop]
            types = []
            # `type` NEED NOT BE THE FIRST THING IN THE BRACES. This pattern was
            # `\{\s*type:` and therefore read an object contract only while
            # nothing — not even a comment — sat between the brace and the key.
            # Measured 2026-09-01: adding a comment inside the `tracked-entity`
            # contract of `navigation/governance.ts` dropped all FIVE of its
            # pages out of the declared set, and the inventory parity test
            # reported them as pages the navigation no longer declares. A
            # registry reader that goes blind on a comment reports a removal
            # that never happened, which is worse than a stale count.
            for om in re.finditer(
                r'\{(?:\s*//[^\n]*)*\s*type:\s*"([\w-]+)"(.*?)\}', call, re.S
            ):
                tabs_m = re.search(r"tabs:\s*(?:\[([^\]]*)\]|([A-Z_]+))", om.group(2), re.S)
                tabs: list[str] = []
                if tabs_m and tabs_m.group(1) is not None:
                    tabs = re.findall(r'"([\w-]+)"', tabs_m.group(1))
                elif tabs_m:
                    cm = re.search(rf"const {tabs_m.group(2)}\s*=\s*\[([^\]]*)\]", src, re.S)
                    tabs = re.findall(r'"([\w-]+)"', cm.group(1)) if cm else []
                types.append({"type": om.group(1), "tabs": tabs, "answered": "?"})
            out.append(Screen(
                id=f"{ws}/{m.group(1)}", title=m.group(2), scope="project",
                surface=ws, section=m.group(1),
                route=f"/org/:orgId/project/:projectId/{ws}/{m.group(1)}",
                objects=types,
            ))
    return out

def answered_types() -> tuple[set[str], set[str]]:
    """What ContentRouter really answers.

    Two shapes, and conflating them hides a real difference: an EXPLICIT branch
    names the type (`route.objectType === "golden-question"`), a BLANKET branch
    catches every type of a workspace (`route.workspace === "governance"` hands
    the lot to one workbench). A blanket branch means the address resolves; it
    does not mean that workbench draws that type. So they are reported apart."""
    src = read_router()
    explicit = set(re.findall(r'route\.objectType === "([\w-]+)"', src))
    # the `contracts = { sources: { type: "source-account", ... } }` lookup table
    explicit |= set(re.findall(r'\btype:\s*"([\w-]+)"', src))
    blanket = set()
    for m in re.finditer(r'route\.workspace === "([\w-]+)"(?!\s*&&\s*route\.section)', src):
        window = src[m.start(): m.start() + 900]
        if re.search(r"return \(?\s*<[A-Z]", window) and "route.objectType ===" not in window[:200]:
            blanket.add(m.group(1))
    return explicit, blanket


GLOBALS = [
    ("PROJECT_SETTINGS", "project-settings", "project", "/org/:orgId/project/:projectId/settings/{s}"),
    ("PROJECT_ACCESS", "project-access", "project", "/org/:orgId/project/:projectId/access/{s}"),
    ("ORGANIZATION_SETTINGS", "organization-settings", "organization", "/org/:orgId/settings/{s}"),
    ("ACCOUNT", "account", "account", "/account/{s}"),
    ("PLATFORM", "platform", "platform", "/platform/{s}"),
]


def globals_() -> list[Screen]:
    src = read(ROUTER)
    out: list[Screen] = []
    for const, surface, scope, tpl in GLOBALS:
        m = re.search(rf"const {const}\s*=\s*new Set<[^>]*>\(\[([^\]]*)\]\)", src)
        if not m:
            continue
        for s in re.findall(r'"([\w-]+)"', m.group(1)):
            out.append(Screen(
                id=f"{surface}/{s}",
                title=f"{surface.replace('-', ' ').title()} / {s.replace('-', ' ')}",
                scope=scope, surface=surface, section=s, route=tpl.format(s=s),
            ))
    if '"getting-started": "journey"' in src:
        out.append(Screen(id="getting-started/journey", title="Getting Started / journey",
                          scope="project", surface="getting-started", section="journey",
                          route="/org/:orgId/project/:projectId/getting-started/journey"))
    return out


# ---------------------------------------------------------------------------
# 2. THE LINK TO THE FRONT.  ContentRouter is the only authority on what a
#    route actually mounts, so it is parsed rather than guessed.
# ---------------------------------------------------------------------------


def component_paths() -> dict[str, str]:
    """Component name -> source file, from both lazy() and plain imports."""
    src = read_router()
    out: dict[str, str] = {}
    for m in re.finditer(r'const (\w+)\s*=\s*lazy\(\s*\(\)\s*=>\s*\n?\s*import\(\s*"([^"]+)"', src):
        out[m.group(1)] = m.group(2)
    for m in re.finditer(r'^import\s+(\w+)\s+from\s+"([^"]+)"', src, re.M):
        out[m.group(1)] = m.group(2)
    for m in re.finditer(r'^import\s+\{([^}]+)\}\s+from\s+"([^"]+)"', src, re.M):
        for name in re.findall(r"\b([A-Z]\w+)\b", m.group(1)):
            out.setdefault(name, m.group(2))
    return out


def _resolve_import(spec: str) -> str | None:
    if not spec.startswith("."):
        return None
    base = (CONTENT.parent / spec).resolve()
    for cand in (base.with_suffix(".tsx"), base.with_suffix(".ts"),
                 base / "index.tsx", base / "index.ts"):
        if cand.exists():
            try:
                return cand.relative_to(ROOT).as_posix()
            except ValueError:
                return str(cand)
    return None


def mounts() -> dict[str, tuple[list[str], int]]:
    """screen id -> (component names, line in ContentRouter that mounts it)."""
    src = read_router()
    lines = src.splitlines()
    out: dict[str, tuple[list[str], int]] = {}

    # (a) the collection switch: `case "workspace/section":` ... `break;`
    pending: list[tuple[str, int]] = []
    for n, line in enumerate(lines, start=1):
        m = re.match(r'\s*case "([\w-]+/[\w-]+)":\s*$', line)
        if m:
            pending.append((m.group(1), n))
            continue
        if pending:
            if not line.strip() or line.strip().startswith("//"):
                continue
            # body of the case: collect components until `break;`
            body, j = [], n - 1
            while j < len(lines) and "break;" not in lines[j]:
                body.append(lines[j])
                j += 1
            comps = sorted(set(re.findall(r"<([A-Z]\w+)", "\n".join(body))))
            for sid, ln in pending:
                out[sid] = (comps, ln)
            pending = []

    # (b) the global surfaces, each an early `if (route.globalSurface === "x")`
    for m in re.finditer(r'route\.globalSurface === "([\w-]+)"', src):
        surface = m.group(1)
        start = src[:m.start()].count("\n") + 1
        window = "\n".join(lines[start - 1: start + 8])
        comps = sorted(set(re.findall(r"<([A-Z]\w+)", window)))
        for sid in list(out) + []:
            pass
        out[f"__surface__{surface}"] = (comps, start)
    return out


# ---------------------------------------------------------------------------
# 3. THE SPEC LINK.  Existing documents only, with their real criteria.
# ---------------------------------------------------------------------------


#: A THIRD PARSER OF ONE LIST IS A THIRD ANSWER TO "WHAT DOES INDEX 13 NAME?".
#: This file used to walk the documents itself — first heading containing
#: `Incomplete if`, `-`/`*` bullets only, stop at the next `#`. Measured
#: 2026-08-21 across the 24 ratified documents: **267 criteria against 480**,
#: and **0** for `visualization-and-rendering`, `execution-substrate`,
#: `mcp-tool-surface` and `user-bridge`, whose lists are numbered `1.`. It
#: disagreed on INDEX 0 for six surfaces out of 24 — and index 0 is exactly what
#: `completeness-ledger.json` keys a verdict by, so `work <id>` printed one
#: surface's verdict beside another surface's criterion. That work order is the
#: entry point of five agents and three skills (`.claude/agents/screen-fixer.md`,
#: `.claude/skills/screen-loop/SKILL.md`), so the divergence was not academic.
#:
#: `finished_work_audit.incomplete_if` is now the ONE reader, as it already is
#: for `check_ledger_anchors.py`. Nothing is re-derived here.
incomplete_if = _audit_incomplete_if


def ledger() -> dict:
    if not LEDGER.exists():
        return {}
    try:
        return json.loads(read(LEDGER))
    except json.JSONDecodeError:
        return {}


def coverage_rows() -> list[str]:
    if not COVERAGE.exists():
        return []
    return [l for l in read(COVERAGE).splitlines() if l.startswith("| `")]


# ---------------------------------------------------------------------------
# 4. THE TEST LINK.  Which vitest files actually exercise that component.
# ---------------------------------------------------------------------------


def test_index() -> dict[str, str]:
    """test file path -> its text, read once."""
    idx = {}
    for p in (UI / "src").rglob("*.test.ts*"):
        idx[p.relative_to(ROOT).as_posix()] = read(p)
    return idx


def local_imports(rel: str) -> set[str]:
    """The repo-relative files a source file imports from its own tree."""
    p = ROOT / rel
    if not p.exists():
        return set()
    out: set[str] = set()
    for m in re.finditer(r'(?:from|import)\s*\(?\s*"(\.[^"]+)"', read(p)):
        base = (p.parent / m.group(1)).resolve()
        for cand in (base.with_suffix(".tsx"), base.with_suffix(".ts"),
                     base / "index.tsx", base / "index.ts"):
            if cand.exists():
                try:
                    out.add(cand.relative_to(ROOT).as_posix())
                except ValueError:
                    pass
                break
    return out


def tests_for(components: list[str], front: list[str], idx: dict[str, str]) -> list[str]:
    """A test covers a screen when it imports the mounted component OR one of
    that component's own children. The child hop is not optional: a screen whose
    workbench is split into panels is tested through the panels, and a matcher
    that stops at the mounted name reports `no test` for a screen that has
    seven. That false negative sends work where there is none to do."""
    covered = {f for f in front if not f.endswith("(unresolved)")}
    # Follow the mounted component's own children -- but only the ones that live
    # in its own folder. Following it into `components/ui/`, `shell/` or the API
    # seam pulls in shared infrastructure, and then EVERY suite "covers" every
    # screen: the four Governance screens all ranked their suites alphabetically
    # and led with `AnalyzeRoutes.test.tsx`. A test that imports a shared
    # primitive tests the primitive, not this screen.
    own_dirs = {str(Path(f).parent) for f in covered}
    for f in list(covered):
        covered |= {c for c in local_imports(f) if str(Path(c).parent) in own_dirs}
    real_front = [f for f in front if not f.endswith("(unresolved)")]
    mounted = {Path(f).stem for f in real_front}
    stems = {Path(f).stem for f in covered} | set(components)

    def key(rel: str) -> str:
        """`ui/admin/src/governance/GovernanceCollection.tsx` -> `governance/GovernanceCollection`.

        A test imports `../governance/GovernanceCollection`, never the
        repo-relative path. Matching on the repo-relative path found nothing, so
        every Governance suite fell through to the weakest rank and the run
        picked `RenderSharingLegacy` and `AiPathPage` -- files that merely say
        the word. The suffix is what both sides actually share."""
        return re.sub(r"\.(tsx?|jsx?)$", "", rel).replace("ui/admin/src/", "")

    front_keys = [key(f) for f in real_front]
    child_keys = [key(f) for f in covered if f not in real_front]

    hits: list[tuple[int, str]] = []
    for path, text in idx.items():
        stem = Path(path).name.split(".")[0]
        if stem in mounted or stem in components:
            rank = 0                      # the suite named after the mounted screen
        elif any(k in text for k in front_keys):
            rank = 1                      # imports the mounted file itself
        elif any(k in text for k in child_keys):
            rank = 2                      # imports one of its children
        elif any(re.search(rf"\b{re.escape(s)}\b", text) for s in stems if s):
            rank = 3                      # merely names it
        else:
            continue
        hits.append((rank, path))
    # Ranked, not alphabetical. With an alphabetical order the six-suite cap gave
    # the four Governance screens the SAME six files -- four identical runs
    # reported as four per-screen verdicts (`77 passed` four times over). A cap
    # is defensible; a cap that makes siblings indistinguishable is not.
    # Keep only the strongest evidence available, never a mixture. Rank 3 is a
    # bare name mention, and `<RouteState>` or `<Suspense>` in a switch body
    # match half the suite -- which is how the four Governance screens each
    # claimed 34 tests. When something imports the screen, a file that merely
    # says its name adds noise, not coverage.
    if hits:
        best = min(r for r, _ in hits)
        hits = [h for h in hits if h[0] == best]
    hits.sort(key=lambda t: t[1])
    return [p for _, p in hits]


# ---------------------------------------------------------------------------
# RESOLVE -- build the link table.  This is the whole point of the tool.
# ---------------------------------------------------------------------------


def resolve() -> list[Screen]:
    screens = workspaces() + globals_()
    comp_paths = component_paths()
    mnt = mounts()
    led = ledger()
    idx = test_index()
    cov = coverage_rows()
    explicit, blanket = answered_types()

    for s in screens:
        # --- spec
        docs = SECTION_DOC.get(s.id) or SURFACE_DOC.get(s.surface, [])
        for d in docs:
            p = ARCH / d
            s.spec.append(f"docs/product-architecture/{d}" + ("" if p.exists() else "  (MISSING)"))
            stem = Path(d).stem
            key = LEDGER_KEY.get(stem, stem)
            crits = incomplete_if(p)
            recorded = led.get(key)
            for i, c in enumerate(crits):
                v = (recorded or {}).get(str(i))
                if (v or {}).get("verdict") == "false":
                    verdict = "cleared"
                elif recorded is None:
                    # The ledger's own rule makes an unrecorded criterion count as
                    # still incomplete -- but the WORK is different: OPEN means
                    # someone judged and it failed, UNRECORDED means the surface
                    # was never evaluated. Merging them hides which of the two.
                    verdict = "UNRECORDED"
                else:
                    verdict = "OPEN"
                s.criteria.append({
                    "doc": d, "index": i, "text": c[:220], "ledger_key": key,
                    "verdict": verdict,
                    "evidence": (v or {}).get("evidence", "")[:160],
                })
        if not docs:
            s.spec.append("NO RATIFIED DOCUMENT -- say so before writing code (CLAUDE.md §1)")

        row = [r for r in cov if f"/{s.section}" in r or f"`{s.section}" in r]
        if row:
            s.spec.append("_bmad-output/planning-artifacts/desktop-design-story-coverage-2026-07-24.md")

        # --- front
        comps, line = mnt.get(s.id, mnt.get(f"__surface__{s.surface}", ([], 0)))
        # AI-255 -- THE ANCHOR, NOT THE LINE NUMBER. This said
        # `ContentRouter.tsx:{line}`, and this text is REGENERATED: any insertion
        # above the mount moved every citation below it, and the file re-emitted
        # the new wrong numbers by itself. The router dispatches on the screen's
        # own address, so `case "<id>"` is what a reader greps for and what
        # survives an edit anywhere else in the file.
        s.mounted_at = (
            f'ui/admin/src/shell/ContentRouter.tsx (case "{s.id}")'
            if line
            else "NOT MOUNTED"
        )
        for c in comps:
            spec_path = comp_paths.get(c)
            resolved = _resolve_import(spec_path) if spec_path else None
            if resolved:
                s.front.append(resolved)
            elif spec_path:
                s.front.append(f"{spec_path}  (unresolved)")
        s.front = sorted(set(s.front))

        # --- backward: is every promised object type answered by a branch?
        for o in s.objects:
            if o["type"] in explicit:
                o["answered"] = "explicit"
            elif s.surface in blanket:
                o["answered"] = f"blanket:{s.surface}"
            else:
                o["answered"] = "UNANSWERED"

        # --- tests + run.  Ranked: the suite named after the mounted component
        # first, then the ones that import it, then its children, then mentions.
        s.tests = tests_for(comps, s.front, idx)
        if s.tests:
            rel = " ".join(t[len("ui/admin/"):] for t in s.tests[:RUN_CAP])
            s.run_cmd = f"cd ui/admin && npx vitest run {rel}"
            if len(s.tests) > RUN_CAP:
                s.run_cmd += f"   # {RUN_CAP} of {len(s.tests)} — see TEST for the rest"
        else:
            s.run_cmd = "NO TEST COVERS THIS SCREEN"
    return screens


def unmet(s: Screen) -> list[dict]:
    """Criteria that do not clear the screen: judged and failed (`OPEN`), or
    never judged at all (`UNRECORDED`). The ledger counts both as incomplete."""
    return [c for c in s.criteria if c["verdict"] in ("OPEN", "UNRECORDED")]


#: Everything `resolve()` reads. A cache is only as honest as the list of things
#: that may invalidate it, so this list is DERIVED from directories rather than
#: from filenames: the registry moved out of `navigation.ts` into seven files on
#: 2026-08-12 and a guard pinned to a path would have gone quiet that day (see
#: `NAV_SURFACES` above, and `routed_tabs_across` in `finished_work_audit.py`).
def _board_sources() -> list[Path]:
    return [
        *NAV_SURFACES,
        *CONTENT_SURFACES,
        ROUTER,
        LEDGER,
        COVERAGE,
        *ARCH.rglob("*.md"),
        *(UI / "src").rglob("*.ts"),
        *(UI / "src").rglob("*.tsx"),
    ]


def board_is_stale() -> str | None:
    """The first source newer than `screens/board.json`, or None when it is current.

    THE CACHE WAS NEVER CONFRONTED WITH ANYTHING. `load_board()` preferred
    `screens/board.json` the moment the file existed, no command rewrote it, and
    no test compared it to `resolve()`. `SESSIONS.md` reported the consequence on
    2026-08-03 -- `backward` still announcing `30/31 ... 1 are not` after the
    `skill` type had been removed from `navigation.ts`, where moving the file
    aside gave `30/30 ... 0 are not` -- and the note stayed open for eighteen
    days. It happened again on 2026-08-21: the board still held the 474 criteria
    of 2026-08-17 while the parser read 1095.

    An mtime comparison, like `_routes_cache_is_stale` below, for the same
    reason: reading a few thousand mtimes is far cheaper than the seven seconds
    `resolve()` costs, and being wrong costs a reader who trusts the file.
    """
    try:
        generated_at = BOARD_JSON.stat().st_mtime
    except OSError:
        return "screens/board.json (absent)"
    for path in _board_sources():
        try:
            if path.stat().st_mtime > generated_at:
                return path.relative_to(ROOT).as_posix()
        except OSError:
            continue
    return None


def load_board() -> list[Screen]:
    """The board, and never a stale copy of it.

    What a person sees when it IS stale: one line naming the file that moved and
    the command that refreshes the cache on disk -- then the answer, resolved
    from the sources. The reading is never refused and never wrong; only slower.
    """
    if not BOARD_JSON.exists():
        return resolve()
    stale = board_is_stale()
    if stale:
        print(
            f"screens/board.json was generated before {stale} changed, so it is not "
            "served.\nReading the sources instead; run `python scripts/screens.py "
            "resolve` to refresh the file.",
            file=sys.stderr,
        )
        return resolve()
    data = json.loads(read(BOARD_JSON))
    return [Screen(**s) for s in data["screens"]]


def save_board(screens: list[Screen]) -> None:
    OUT.mkdir(exist_ok=True)
    BOARD_JSON.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": ["ui/admin/src/shell/navigation.ts", "ui/admin/src/shell/router.tsx",
                   "ui/admin/src/shell/ContentRouter.tsx", "docs/product-architecture/"],
        "count": len(screens),
        "screens": [asdict(s) for s in screens],
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def cmd_resolve(_a) -> int:
    screens = resolve()
    save_board(screens)
    write_board_md(screens)
    no_front = [s.id for s in screens if not s.front]
    no_test = [s.id for s in screens if not s.tests]
    opened = sum(1 for s in screens for c in s.criteria if c["verdict"] == "OPEN")
    unrec = sum(1 for s in screens for c in s.criteria if c["verdict"] == "UNRECORDED")
    print(f"RESOLVE {len(screens)} screens")
    print(f"  front  {len(screens) - len(no_front)}/{len(screens)} mounted and resolved")
    print(f"  tests  {len(screens) - len(no_test)}/{len(screens)} covered by at least one vitest file")
    print(f"  spec   {opened} criteria judged and OPEN, {unrec} never judged (UNRECORDED)")
    for sid in no_front:
        print(f"  NO FRONT  {sid}")
    for sid in no_test:
        print(f"  NO TEST   {sid}")
    return 0


# ---------------------------------------------------------------------------
# WORK -- the exact order for one screen. Paths, criteria, command. No prose.
# ---------------------------------------------------------------------------


def cmd_backward(_a) -> int:
    """The direction that finds the broken promise rather than the missing feature.

    Forward asks `is what is declared present?`. Backward asks `is what is
    present declared, and is what is declared answered?`. Only the second one
    finds an object type a section advertises and no branch opens -- which is a
    404 at the click, not a gap in a document."""
    screens = load_board()
    total = un = 0
    print("declared object types, and the branch that answers each\n")
    for s in sorted(screens, key=lambda x: x.id):
        if not s.objects:
            continue
        print(f"{s.id}")
        for o in s.objects:
            total += 1
            flag = "  " if o["answered"] != "UNANSWERED" else "! "
            if o["answered"] == "UNANSWERED":
                un += 1
            tabs = f"{len(o['tabs'])} tabs" if o["tabs"] else "no tabs"
            print(f"  {flag}{o['type']:<22} {o['answered']:<22} {tabs}")
    print(f"\n{total - un}/{total} declared object types are answered by a branch; {un} are not")
    if un:
        print("An unanswered type is a promise the navigation makes and the click breaks.")
        print("The honest repair is either the workbench, or removing the type from navigation.ts.")
    return 0


def cmd_work(a) -> int:
    screens = {s.id: s for s in load_board()}
    s = screens.get(a.id)
    if not s:
        print(f"no such screen: {a.id}\ntry: {', '.join(sorted(screens)[:8])} ...")
        return 2
    print(f"SCREEN  {s.id}   ({s.title})")
    print(f"ROUTE   {s.route}")
    print(f"MOUNT   {s.mounted_at}")
    print("\nSPEC")
    for p in s.spec:
        print(f"  {p}")
    open_c = unmet(s)
    if open_c:
        print(f"\nOPEN CRITERIA ({len(open_c)} of {len(s.criteria)}) -- this is the acceptance test, not the unit tests")
        for c in open_c:
            print(f"  [{c['verdict']:<10}] {c['doc']}#{c['index']}  {c['text']}")
    print("\nFRONT")
    for p in s.front or ["  -- nothing mounted --"]:
        print(f"  {p}")
    print("\nTEST")
    for p in s.tests or ["  -- no vitest file names this component --"]:
        print(f"  {p}")
    print(f"\nRUN\n  {s.run_cmd}")
    if s.verdict != "untested":
        print(f"\nLAST   {s.verdict}  {s.detail[:300]}")
    return 0


# ---------------------------------------------------------------------------
# TEST -- run it locally.  Nothing here is assumed; the exit code is the truth.
# ---------------------------------------------------------------------------

SUMMARY = re.compile(r"Tests\s+(?:(\d+) failed \| )?(\d+) passed(?: \((\d+)\))?")


def run_screen(s: Screen) -> Screen:
    if not s.tests:
        s.verdict, s.detail = "uncovered", "no vitest file names this component"
        return s
    rel = [t[len("ui/admin/"):] for t in s.tests[:RUN_CAP]]
    code, out = run(["npx", "vitest", "run", *rel], UI)
    m = SUMMARY.search(out)
    # A cap that is not printed reads as full coverage. Governance screens match
    # 37 suites; running six of them and reporting `pass` would be a claim about
    # thirty-one runs that never happened.
    scope = "" if len(s.tests) <= RUN_CAP else f" [{RUN_CAP} of {len(s.tests)} suites]"
    if m:
        failed, passed = int(m.group(1) or 0), int(m.group(2))
        s.detail = f"{passed} passed, {failed} failed{scope}"
    else:
        s.detail = (out.strip().splitlines()[-1][:200] if out.strip() else f"exit {code}") + scope
    s.verdict = "pass" if code == 0 else "fail"
    if code != 0:
        fails = re.findall(r"(?:FAIL|×|AssertionError:)\s*(.+)", out)[:4]
        if fails:
            s.detail += " | " + " ; ".join(f.strip()[:110] for f in fails)
    return s


def cmd_test(a) -> int:
    screens = load_board()
    targets = [s for s in screens if not a.id or s.id == a.id]
    if a.id and not targets:
        print(f"no such screen: {a.id}")
        return 2
    for s in targets:
        run_screen(s)
        print(f"{s.verdict:<10} {s.id:<34} {s.detail[:110]}")
    save_board(screens)
    write_board_md(screens)
    bad = [s for s in targets if s.verdict == "fail"]
    return 1 if bad else 0


# ---------------------------------------------------------------------------
# BOARD -- one generated table.  The only tracker.  36 rows, no narrative.
# ---------------------------------------------------------------------------

MARK = {"pass": "ok", "fail": "FAIL", "uncovered": "no test", "untested": "-"}


def write_board_md(screens: list[Screen]) -> None:
    OUT.mkdir(exist_ok=True)
    rows = [
        "# Screen board — generated by `python scripts/screens.py`. Do not edit.",
        "",
        f"{len(screens)} Level 2 screens, read from `navigation.ts` + `router.tsx`. "
        "`spec` is the ratified document that owns the screen; `open` counts its "
        "`Incomplete if` criteria still open in `completeness-ledger.json`.",
        "",
        "| screen | test | open | front | vitest | spec |",
        "|---|---|---|---|---|---|",
    ]
    for s in sorted(screens, key=lambda x: x.id):
        open_c = len(unmet(s))
        front = f"`{Path(s.front[0]).name}`" + (f" +{len(s.front)-1}" if len(s.front) > 1 else "") if s.front else "**none**"
        tests = f"{len(s.tests)}" if s.tests else "**0**"
        spec = Path(s.spec[0]).name if s.spec else "-"
        rows.append(f"| `{s.id}` | {MARK.get(s.verdict, s.verdict)} | {open_c or '-'} | {front} | {tests} | {spec} |")
    rows += ["", "```", "python scripts/screens.py next          # the one screen to fix now",
             "python scripts/screens.py work <screen id>   # its spec paths, front paths, command",
             "python scripts/screens.py test --id <id>     # run it locally", "```", ""]
    BOARD_MD.write_text("\n".join(rows), encoding="utf-8")


def cmd_board(_a) -> int:
    screens = load_board()
    write_board_md(screens)
    print(f"BOARD screens/board.md  ({len(screens)} rows)")
    return 0


# ---------------------------------------------------------------------------
# WIRING -- the second plane, joined to the first.
#
# `board` and `pages` only ever look at the UI plane: is a component mounted,
# does a tab have a branch. A page can pass both and still do nothing, because
# what makes it work is an endpoint that exists and carries the fields. This
# joins the two planes MECHANICALLY: the endpoints a page's own component tree
# calls, checked against the routes the server really registers.
#
# It does NOT answer "is there a functionality hole" -- see `capability-trace`.
# A hole is a capability nobody built on any plane, and no page-by-page view can
# see one: the page it would live on looks complete because it is complete at
# what it does.
# ---------------------------------------------------------------------------

# ANY string literal holding an `/api/` path. The template hole becomes `{}` so
# it can be compared with a registered route's `{param}`.
#
# ENUMERATING CALLER NAMES DOES NOT WORK, and this is the third correction of the
# same regex. It started at `apiFetch`, which missed
# `apiGet`/`apiPost`/`apiJson`/`apiPut`/`apiDelete` -- the Data fleet screen read
# as calling nothing because `useDataSurface` loads through `apiGet`. The names
# were added. Then `orphans` reported `/api/projects/{}/settings` as a door no
# screen pushes, while `ProjectSettings.tsx:391` pushes it through a local helper
# named `request`. Six such wrappers exist (`request`, `scoped`, `buildUrl`,
# `requestJson`, `readJson`, `load`) hiding SEVENTY real call sites, and nothing
# stops the next module from inventing a seventh.
#
# So the rule stops asking who calls and asks what is written: an `/api/` path
# spelled out in a non-test source file IS a call. Comments are stripped first --
# a docstring quoting `/api/...` is prose, and reading prose as code has now cost
# this repository three separate false findings in one day.
CALL_RE = re.compile(r'[`"\']([^`"\'\n]*/api/[^`"\'\n]*)[`"\']')

COMMENT_RE = re.compile(r'/\*[\s\S]*?\*/|//.*')


def _routes_cache_is_stale() -> bool:
    """True when any route-declaring source is newer than the cache (AI-237).

    `server/core/*.py` is the scope, and it is deliberately coarse: the router is
    assembled from many modules, so watching `admin_api.py` alone would miss a
    route declared next door -- which is the precise shape of the silence this
    replaces. Reading ~200 mtimes is cheaper than the import it decides about.
    """
    try:
        cached_at = ROUTES_JSON.stat().st_mtime
    except OSError:
        return True
    core = ROOT / "server" / "core"
    return any(
        path.stat().st_mtime > cached_at
        for path in core.glob("*.py")
    )


def server_routes() -> set[str]:
    """Every path the server really registers, normalized to `{}` holes.

    Imported rather than grepped: half the route lists are built with f-strings
    over a `_BASE` constant (`server/core/datastream_workbench_api.py:321`), so a
    regex sees 111 of the 481 and every page would read as calling something that
    does not exist. A measurement instrument that fabricates red is worse than
    none (see `_exe` above, same lesson).

    THE CACHE EXPIRES (AI-237). It used to be returned whenever the file existed,
    full stop -- so a route added and never followed by a manual regeneration made
    `screens/wiring.md` false in silence, and nothing anywhere could notice. The
    import costs a few seconds; being wrong costs a reader who trusts the file.

    So the cache is kept for what it is good at -- not paying the import on every
    run -- and dropped as soon as any file that DECLARES a route is newer than it.
    The router is assembled from many modules, so the whole of `server/core` is
    the mtime to beat, not `admin_api.py` alone: a route declared in
    `datastream_workbench_api.py` would otherwise never expire the cache."""
    if ROUTES_JSON.exists() and not _routes_cache_is_stale():
        return set(json.loads(read(ROUTES_JSON)))
    code = (
        "import sys, json; sys.path.insert(0, '.'); "
        "from core.admin_api import router; "
        "print(json.dumps(sorted({getattr(r, 'path', '') for r in router.routes})))"
    )
    rc, out = run([sys.executable, "-c", code], ROOT / "server", timeout=180)
    if rc != 0:
        print(f"  ! could not import the router ({rc}); API plane NOT CHECKED")
        return set()
    paths = json.loads(out.strip().splitlines()[-1])
    norm = sorted({re.sub(r"\{[^}]*\}", "{}", p) for p in paths})
    OUT.mkdir(exist_ok=True)
    ROUTES_JSON.write_text(json.dumps(norm, indent=1) + "\n", encoding="utf-8")
    return set(norm)


# A door with a pusher that is not a screen. WRITTEN, never inferred: an
# unexplained exclusion is how a real gap gets filed as normal, and a heuristic
# that guesses "MCP probably owns this" was tried first and matched half the list
# on the word `health`. Anything not named here counts as unclaimed, which is the
# safe direction -- a false question costs a grep, a false exemption costs a
# surface.
BY_DESIGN = {
    "/api/health": "Cloud Run's liveness probe. No screen has any business here.",
    "/api/auth/oidc/callback": (
        "The browser is REDIRECTED here; the console pushes "
        "`/api/auth/oidc/login` (`BrowserAuthGate.tsx`) and the identity "
        "provider sends the person back. A redirect target is navigated to, "
        "never fetched."
    ),
    "/api/google/oauth/callback": (
        "Same shape, Google's consent return -- the one human gate in the "
        "Google-direct OAuth path (AD-21)."
    ),
    "/api/mcp-hosts/preflight/{}/bind": (
        "Pushed by the MCP host while pairing, which is the other client of "
        "this server."
    ),
    "/api/datastreams/{}/run": (
        "Deliberately not surfaced. `DatastreamReloadPanel.tsx:4` records the "
        "decision (AI-144): triggering a collection is not scheduling, and the "
        "ratified vocabulary is `synchronize / reload / reprocess / rollback`. "
        "The console offers `reload` over a bounded range instead."
    ),
    "/api/datastreams/{}/executions": (
        "Answered richer by `workbench/runs` (`datastream_workbench.py:1521`), "
        "which returns the same rows plus each run's recovery eligibility."
    ),
    "/api/datastreams/{}/executions/{}": (
        "Same: the Runs tab selects from the payload it already holds."
    ),
    "/api/datastreams/{}/publications": (
        "Answered by `workbench/outputs` (`datastream_workbench.py:1594`), which "
        "adds the candidate/current/last-known-good pointers and `used_by`."
    ),
}

# The question a row cannot answer about itself. `docs/product-architecture/` is
# the ratified target; if it names a route that nothing pushes, that is a gap
# between the wanted and the delivered. If it names it nowhere, the question is
# not "who forgot to wire this" but "does this belong at all" -- a different
# conversation, and not one a wiring pass should answer by wiring.
#
# It cut 64 rows to 10 on the first run. Twelve `/api/datastreams/{id}/*` doors
# read as a whole broken family until this column showed that not one of them is
# cited anywhere, while every Workbench route is -- they are the pre-Workbench
# generation, not a hole.
ARCH_GLOBS = ("*.md", "*.json")

# `docs/product-architecture/` holds two kinds of file, and CLAUDE.md names the
# split: Surfaces / Transverses / Capacités state what we want, Registres record
# how the delivery is doing against it. Only the first kind can cite a route as a
# requirement.
REGISTERS = {
    "known-debt.json",
    "completeness-ledger.json",
    "caveats-register.md",
    "alignment-register.md",
    "element-control-loop.md",
    "SURFACE-STATE.md",
    "page-structure.md",
    "research-findings.md",
}


def _enclosing_heading(body: str, offset: int) -> str | None:
    """The nearest markdown heading ABOVE *offset* -- the anchor of a document.

    AI-255. This used to be a line NUMBER, and a line number in a document that
    someone edits above the citation is wrong from the next edit onwards. Worse,
    the number is GENERATED: `screens/orphans.md` re-emits it at every run, so
    correcting one by hand never holds.

    A heading is what a document actually offers as an address. It survives an
    insertion above it, it is what a reader searches for, and it is what a link
    would point at. When no heading precedes the match -- a route named in a
    preamble -- the citation carries the file alone, which is honest: the
    document names the door, and nothing finer is true.
    """
    heading = None
    for line in body[:offset].splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
    return heading or None


def ratified_citations(routes: list[str]) -> dict[str, str]:
    """`route -> document § heading` for every route a ratified document names.

    NOT `document:line` (AI-255): this text is REGENERATED on every run, so a
    line number here is a stale citation the repository re-emits by itself.
    """
    # THE REGISTERS ARE EXCLUDED, and this is the whole point of the column. They
    # record verdicts ABOUT the delivery; they do not state what we want.
    # `known-debt.json` is the output of `finished_work_audit.unreachable_routes()`
    # -- a list of doors already found unreached -- so counting it made this
    # measure quote a sibling measure and call the quote a requirement: six of the
    # first ten rows read "the target asks for this" when the target says nothing.
    # `completeness-ledger.json` then supplied two more, both from inside an
    # `evidence` string where a route is pasted as PROOF, not as an ask.
    #
    # CLAUDE.md already draws this line: Surfaces, Transverses and Capacités carry
    # the target; Registres carry its assessment. The measure now respects it.
    corpus = {
        p: read(p)
        for glob in ARCH_GLOBS
        for p in ARCH.rglob(glob)
        if p.name not in REGISTERS
    }
    found: dict[str, str] = {}
    for route in routes:
        # A route ends where the path ends. Without the boundary, a mention of
        # `/api/cards/templates` cites `/api/cards`, and two doors that are not
        # the same door read as one -- the exact confusion that made
        # `/api/cards` look reached this morning.
        pattern = re.escape(route).replace(r"\{\}", r"\{[^}/]*\}") + r"(?![\w/{-])"
        for path, body in corpus.items():
            match = re.search(pattern, body)
            if match:
                heading = _enclosing_heading(body, match.start())
                found[route] = f"{path.name} § {heading}" if heading else path.name
                break
    return found


def route_modules() -> dict[str, str]:
    """Which server module mounts each route -- the first question asked of a
    row, and one that took a subprocess to answer by hand each time."""
    code = (
        "import sys, json, re; sys.path.insert(0, '.'); "
        "from core.admin_api import router; "
        "out = {}; "
        "[out.setdefault(re.sub(r'\\{[^}]*\\}', '{}', getattr(r, 'path', '')), "
        "getattr(getattr(r, 'endpoint', None), '__module__', '?').split('.')[-1]) "
        "for r in router.routes]; "
        "print(json.dumps(out))"
    )
    rc, out = run([sys.executable, "-c", code], ROOT / "server", timeout=180)
    return json.loads(out.strip().splitlines()[-1]) if rc == 0 else {}


def _computed(expr: str) -> bool:
    """Does this `${...}` hole carry a whole path SEGMENT chosen at runtime?

    `${encodeURIComponent(id)}` and `${projectId}` fill one id and the path can
    still be compared with a route. `${ENDPOINTS[lens]}` and `${suffix}` decide
    what the path IS, so no static comparison can succeed and calling the result
    a dead endpoint would be fabricated red -- the third time this file has had
    to make that distinction."""
    e = expr.strip()
    if "[" in e:
        return True
    return bool(re.fullmatch(r"[A-Za-z_]\w*", e)) and not re.search(r"[Ii][dD]$", e)


_DEF_RE = re.compile(
    r'const\s+(\w+)\s*(?::[^=]*)?=\s*(?:\([^)]*\)\s*(?::[^=]*)?=>\s*)?`([^`\n]*/api/[^`\n]*)`'
)
# `function setupDraftPath(cfg, draftId) { const root = `…`; return draftId ?
# `${root}/${id}` : root; }` -- the wizard's whole API surface hangs off this one
# helper, sixteen call sites. A `const`-only resolver saw none of them.
_FUNC_RE = re.compile(
    r'function\s+(\w+)\s*\([^)]*\)[^{]*\{([^\n]*?`[^`\n]*/api/[^`\n]*`[^\n]*?)\}'
)
_TABLE_RE = re.compile(r'const\s+(\w+)\s*(?::[^=]*)?=\s*\{([^{}]*)\}\s*;')
_ENTRY_RE = re.compile(r'[\w"\']+\s*:\s*["\']([^"\']+)["\']')


def _resolved(src: str) -> str:
    """Source with this file's own path constants substituted into the literals.

    THE THIRD BLIND SPOT, and the one that hid whole families. `client.ts:315`
    defines `const base = (projectId) => `/api/projects/${projectId}/analyze``
    and then writes ``${base(projectId)}/reports`` seventeen times. That literal
    holds no `/api/`, so nothing matched it, and every Analyze and Test route
    read as a door no screen pushes -- ten of them at once.

    Only definitions in the SAME file are resolved, because that is where the
    evidence is and a cross-file resolver would be an import graph, not a
    measurement."""
    defs = {name: lit for name, lit in _DEF_RE.findall(src)}
    for name, body in _FUNC_RE.findall(src):
        lit = re.search(r'`([^`\n]*/api/[^`\n]*)`', body)
        if not lit:
            continue
        # `return draftId ? `${root}/${id}` : root` -- the helper answers with
        # the collection OR the object, so both are real. Without the branch it
        # answers with one path and inventing a second `/{}` would fabricate a
        # call that suppresses a genuine orphan.
        branches = "?" in body and "${" in body.split("?", 1)[1]
        defs[name] = lit.group(1) + ("/{}" if branches else "")
    for _ in range(2):  # a base may be spelled in terms of another base
        for name, lit in defs.items():
            src = re.sub(r'\$\{' + name + r'\([^)]*\)\}', lit, src)
            src = re.sub(r'\$\{' + name + r'\}', lit, src)
    return src


def _table_expansions(src: str) -> dict[str, list[str]]:
    """`${ENDPOINTS[lens]}` is not unknowable -- the table is right there.

    `dataSurface.ts:90` picks one of four literal suffixes by lens. Treating the
    hole as runtime-composed threw away four real calls
    (`source-accounts`, `imports`, `event-configurations`, `connectors`), and all
    four then reported as orphans."""
    out: dict[str, list[str]] = {}
    for name, body in _TABLE_RE.findall(src):
        values = _ENTRY_RE.findall(body)
        if values and len(values) == body.count(":"):
            out[name] = values
    return out


def calls_in(files: set[str]) -> tuple[set[str], set[str]]:
    """(comparable paths, runtime-composed paths) a component tree calls."""
    fixed: set[str] = set()
    dynamic: set[str] = set()
    for f in files:
        src = COMMENT_RE.sub("", read(ROOT / f))
        tables = _table_expansions(src)
        src = _resolved(src)
        for m in CALL_RE.finditer(src):
            raws = [m.group(1)]
            for name, values in tables.items():
                if f"{{{name}[" in raws[0].replace("$", ""):
                    raws = [
                        re.sub(r'\$\{' + name + r'\[[^\]]*\]\}', v, r)
                        for r in raws for v in values
                    ]
            for raw in raws:
                _one_call(raw, fixed, dynamic)
    return fixed, dynamic


def _one_call(raw: str, fixed: set[str], dynamic: set[str]) -> None:
    """File one literal into the comparable set or the runtime-composed one."""
    # The query string goes FIRST, before the holes are judged. A hole after the
    # `?` never decides what the path is -- and judging it anyway made
    # `/api/mdm/conflicts?project_id=${scope}` read as runtime-composed, so
    # `/api/mdm/conflicts` reported as a door nobody pushes while
    # `ProjectMapping.tsx` pushes it.
    path = raw[raw.index("/api/"):].split("?")[0]
    holes = re.findall(r"\$\{([^}]*)\}", path)
    path = re.sub(r"\$\{[^}]*\}", "{}", path).rstrip("/")
    # An `/api/` inside an error message is prose, not a call: matching every
    # literal caught `` `/api/connections failed: HTTP ${s} — ${b}` `` and would
    # have taught the measure that a sentence is an endpoint. A path has no
    # spaces, and `...` is an ellipsis in a guard's text.
    if not path or "..." in path or " " in path:
        return
    # A hole GLUED to the previous segment is the `objectId ? "/"+id : ""` idiom,
    # written eight times in this console. It is not one unknowable path, it is
    # exactly two known ones -- the collection and the object -- and calling it
    # unknowable made both read as doors nobody pushes.
    if path.endswith("{}") and not path.endswith("/{}"):
        stem = path[:-2].rstrip("/")
        fixed.add(stem)
        fixed.add(stem + "/{}")
        return
    (dynamic if any(_computed(h) for h in holes) else fixed).add(path)


def route_answers(call: str, routes: set[str]) -> bool:
    """Segment-wise match, `{}` being a hole on EITHER side.

    A plain set lookup reported `/nodes/root/commands` as answered by nothing
    while the server registers `/nodes/{node_id}/commands`: the UI passes the
    literal `root` in a parameter position, which is exactly what a parameter is
    for. String equality cannot see that, and every such call read as a 404 that
    does not happen."""
    if call in routes:
        return True
    cs = call.split("/")
    for r in routes:
        rs = r.split("/")
        if len(rs) != len(cs):
            continue
        if all(a == b or a == "{}" or b == "{}" for a, b in zip(rs, cs)):
            return True
    return False


def console_calls() -> set[str]:
    """Every `/api/...` path the WHOLE console calls, normalized like a route.

    `wiring` walks the component tree of each page and answers "does this page
    reach the server". This answers the opposite question over the whole tree at
    once, and it is the one that kept going unasked.
    """
    called: set[str] = set()
    for path in (UI / "src").rglob("*.ts*"):
        # A test that names a route proves a test exists, not that a person can
        # reach it -- `FileSourceSamplePanel.test.tsx` spells out two paths its
        # component never spells out. Colocated tests are excluded too, not just
        # the `__tests__` directories.
        if "__tests__" in path.parts or ".test." in path.name or ".spec." in path.name:
            continue
        rel = path.relative_to(ROOT).as_posix()
        fixed, dynamic = calls_in({rel})
        # The runtime-composed ones COUNT here, unlike in `wiring`. The two
        # commands ask different questions: `wiring` asks whether a call reaches
        # a real route, and a path assembled at runtime cannot answer it. This
        # asks whether any screen pushes a door -- and
        # `/api/projects/{}/datastreams/{}/workbench/{}` pushes six of them, one
        # per tab. Dropping it reported all six as unreachable.
        called |= fixed | dynamic
    return called


def cmd_orphans(_a) -> int:
    """Mounted server routes that NO console file calls.

    Written on 2026-08-03 after finding the same shape on five separate pages in
    one day: the endpoint is built, mounted and tested, and nothing in the
    console reaches it. `createReport`, `createNotebook`, `create_render`, the
    Datastream sample reader, five idempotent creators -- each found one page at
    a time, by four reviewers, over hours.

    A page-by-page review cannot see this class: the page where the missing call
    belongs looks complete, because it is complete at what it does. Only the two
    inventories compared can name it.

    WHAT THIS IS NOT. An orphan is not automatically a defect. An MCP-only tool,
    a webhook, an internal scheduler door and a public bootstrap all legitimately
    have no console caller. The list is a QUESTION per row -- "who is meant to
    push this door?" -- and the answer is sometimes "nobody, by design".
    """
    routes = server_routes()
    if not routes:
        return 1
    called = console_calls()

    # A console call matches a route segment-wise, `{}` being a hole on either
    # side -- the same rule `wiring` uses, and for the same reason: a literal in
    # a parameter position is what a parameter is for.
    def reached(route: str) -> bool:
        return any(route_answers(call, {route}) for call in called)

    api = [r for r in routes if r.startswith("/api/")]
    orphans = sorted(r for r in api if not reached(r))

    # 293 of 400 is not a finding, it is a phone book. Most orphans are
    # legitimately MCP-only, webhook, scheduler or bootstrap doors, and a list
    # nobody can act on is the same as no list.
    #
    # The discriminating question is narrower: is this door closed inside a
    # family the console ALREADY reaches? `/analyze/reports` is called and
    # `/analyze/reports/{}/versions` is not -- that is the exact shape found on
    # five pages today. A family the console never touches at all is a different
    # conversation, and usually not a defect.
    reached_prefixes = {call.rsplit("/", 1)[0] for call in called if "/" in call}
    reached_prefixes |= {call for call in called}
    adjacent = sorted(
        r for r in orphans
        if r.rsplit("/", 1)[0] in reached_prefixes or r in reached_prefixes
    )
    rows = [
        "# Orphan routes — generated by `python scripts/screens.py orphans`. Do not edit.",
        "",
        f"**{len(adjacent)} doors are closed inside a family the console already "
        f"reaches.** Those are the actionable ones, and they are listed first.",
        "",
        f"({len(orphans)} of {len(api)} mounted `/api/` routes have no console caller at "
        "all. That larger number is not a finding — it is dominated by MCP-only, "
        "webhook, scheduler and bootstrap doors that no screen should ever push.)",
        "",
        "**An orphan is not automatically a defect.** An MCP-only tool, a webhook, an",
        "internal scheduler door and a public bootstrap all legitimately have no console",
        "caller. Each row is a QUESTION — *who is meant to push this door?* — and the",
        "answer is sometimes *nobody, by design*.",
        "",
        "**A sibling measure already existed, and it sees a subset.**",
        "`finished_work_audit.unreachable_routes()` tests the literal stem before a",
        "route's first `{` and asks whether that stem appears ANYWHERE in the console —",
        "so `/api/datastreams/{id}/run` reads as reached the moment any file mentions",
        "`/api/datastreams`. It reports 50 routes; this reports 160. The difference is",
        "the whole class of doors whose family is reached and whose own path is not,",
        "which is exactly the class this command was written for. Its findings land in",
        "`known-debt.json` — read that register as a subset, never as the whole answer.",
        "",
        "What it does catch is the shape found on five separate pages in one day: an",
        "endpoint built, mounted and tested, that the console was supposed to reach and",
        "never did. A page-by-page review cannot see it — the page where the missing call",
        "belongs looks complete, because it is complete at what it does.",
        "",
        "## Named by the target, pushed by no screen",
        "",
        "A ratified document asks for this door and nothing opens it. This is the",
        "work list — a gap between what we wrote down and what a person can reach.",
        "",
        "| route | mounted in | the document that asks for it |",
        "|---|---|---|",
    ]
    claimed = [r for r in adjacent if r in BY_DESIGN]
    unclaimed = [r for r in adjacent if r not in BY_DESIGN]
    cited = ratified_citations(unclaimed)
    wanted = [r for r in unclaimed if r in cited]
    silent = [r for r in unclaimed if r not in cited]
    rows[2] = (
        f"**{len(wanted)} door{'' if len(wanted) == 1 else 's'} named by a ratified "
        f"document {'is' if len(wanted) == 1 else 'are'} pushed by no "
        f"screen.** That is the work list. {len(silent)} more are pushed by nobody "
        f"AND named nowhere — a question of belonging, not of wiring. "
        f"{len(claimed)} are closed on purpose and carry their reason."
    )
    where = route_modules()
    rows += [
        f"| `{route}` | `{where.get(route, '?')}` | `{cited[route]}` |" for route in wanted
    ]
    rows += [
        "",
        "## Pushed by nobody, and named nowhere",
        "",
        "No ratified document mentions these. The question is not *who forgot to wire",
        "this* but *does this belong at all* — and a wiring pass must not answer it by",
        "wiring. Twelve `/api/datastreams/{id}/*` doors sit here: the pre-Workbench",
        "generation, superseded by routes the target does cite.",
        "",
        "| route | mounted in |",
        "|---|---|",
    ]
    rows += [f"| `{route}` | `{where.get(route, '?')}` |" for route in silent]
    rows += [
        "",
        "## Closed on purpose",
        "",
        "Each of these has a pusher that is not a screen. The reason is written here",
        "rather than inferred, because an unexplained exclusion is how a real gap gets",
        "filed as normal.",
        "",
        "| route | who pushes it |",
        "|---|---|",
    ]
    rows += [f"| `{route}` | {BY_DESIGN[route]} |" for route in claimed]
    rows += ["", "## Every route with no console caller", "", "| route |", "|---|"]
    rows += [f"| `{route}` |" for route in orphans]
    rows += [""]
    OUT.mkdir(exist_ok=True)
    ORPHANS_MD.write_text("\n".join(rows), encoding="utf-8")
    print(f"ORPHANS screens/orphans.md")
    print(f"  {len(adjacent)} closed doors inside a family the console reaches")
    print(f"  {len(orphans)} of {len(api)} routes with no console caller at all")
    for route in adjacent[:12]:
        print(f"    {route}")
    return 0


def cmd_wiring(_a) -> int:
    routes = server_routes()
    if not routes:
        return 1
    screens = resolve()
    paths = component_paths()
    obj_explicit, obj_blanket = object_mounts()
    rows = [
        "# Wiring — generated by `python scripts/screens.py wiring`. Do not edit.",
        "",
        f"Two planes joined, page by page: what the UI calls, against the "
        f"{len(routes)} routes the server really registers (read by importing "
        "`core.admin_api`, not by grepping — half the route lists are built with "
        "f-strings and a regex sees a quarter of them).",
        "",
        "`calls` — endpoints the page's own component tree calls. **`0` is the "
        "finding**: nothing on that page reaches the server, so whatever it shows "
        "is static or comes from its parent.",
        "",
        "`unknown` — a path the UI calls that NO registered route answers. Each one "
        "is a click that 404s.",
        "",
        "This view cannot see a **functionality hole** — a capability nobody built "
        "on any plane. Use `capability-trace` for that; a page missing a function "
        "looks complete here, because it is complete at what it does.",
        "",
        "| page | calls | unknown | endpoints |",
        "|---|---|---|---|",
    ]
    dead: dict[str, list[str]] = {}
    n_silent = n_unknown = 0
    for s in sorted(screens, key=lambda x: x.id):
        groups = [(s.id, closure(s.front))]
        for o in s.objects:
            comps = obj_explicit.get(o["type"]) or obj_blanket.get(s.surface) or []
            groups.append((f"{s.id}/o/{o['type']}", closure(files_for(comps, paths))))
        for pid, files in groups:
            # An empty tree is NOT a silent page: it is a page whose component
            # this tool could not resolve (`board` prints those as
            # `react  (unresolved)`). Counting the two together would report nine
            # screens as calling nothing when nobody has looked at them at all --
            # `NOT CHECKED` is a legal answer here and a guess is not.
            if not files:
                rows.append(f"| `{pid}` | NOT CHECKED | - | tree unresolved |")
                continue
            called, dyn = calls_in(files)
            missing = sorted(c for c in called if not route_answers(c, routes))
            if not called and not dyn:
                n_silent += 1
            if missing:
                n_unknown += len(missing)
                dead[pid] = missing
            shown = ", ".join(f"`{c}`" for c in sorted(called)[:3]) or "**none**"
            if len(called) > 3:
                shown += f" +{len(called) - 3}"
            if dyn:
                shown += f" · {len(dyn)} composed at runtime"
            total = len(called) + len(dyn)
            rows.append(
                f"| `{pid}` | {total or '**0**'} | "
                f"{len(missing) and f'**{len(missing)}**' or '-'} | {shown} |"
            )
    if dead:
        rows += ["", "## Paths the UI calls that no route answers", ""]
        for pid, miss in sorted(dead.items()):
            for m in miss:
                rows.append(f"- `{pid}` → `{m}`")
    distinct = sorted({m for miss in dead.values() for m in miss})
    rows += ["", f"{n_silent} pages call nothing. {n_unknown} calls on "
                 f"{len(dead)} pages hit no registered route — "
                 f"**{len(distinct)} distinct paths**.", ""]
    OUT.mkdir(exist_ok=True)
    WIRING_MD.write_text("\n".join(rows), encoding="utf-8")
    print(f"WIRING screens/wiring.md  ({len(routes)} server routes)")
    print(f"  {n_silent} pages reach the server with nothing")
    print(f"  {len(distinct)} distinct dead endpoints, on {len(dead)} pages")
    for d in distinct:
        print(f"    {d}")
    return 0


# ---------------------------------------------------------------------------
# PAGES -- the same inventory at the grain a person actually clicks.
#
# `board` stops at Level 2: 36 sections. But most of this product lives one
# level down, in the object tabs -- Datastream > Mapping, Semantic View >
# Source bindings. `navigation.ts` declares 121 of them and the board gives
# none of them a row, so a tab can be promised by the navigation, answered by a
# blanket branch, drawn by nothing, and still leave the board looking complete.
# ---------------------------------------------------------------------------


def object_mounts() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(explicit: object type -> components, blanket: workspace -> components).

    `mounts()` above only parses the COLLECTION switch, so a workbench mounted by
    an object branch is invisible to it. Rooting a tab's component tree at the
    collection component is what made every Datastream tab read as drawn by
    nothing: `DataWorkspace` does not import the workbench that answers
    `/o/datastream/mapping` -- ContentRouter mounts it on a separate branch.
    """
    src = read_router()
    explicit: dict[str, list[str]] = {}
    for m in re.finditer(r'route\.objectType === "([\w-]+)"', src):
        window = src[m.start(): m.start() + 900]
        comps = set(re.findall(r"<([A-Z]\w+)", window))
        explicit[m.group(1)] = sorted(set(explicit.get(m.group(1), [])) | comps)
    # The `const contracts = { sources: { type: "source-account", ... } }` table:
    # ONE branch (`route.objectType === contract.type`) answers four declared
    # types, and none of the four is ever named in a `=== "..."` comparison.
    tbl = re.search(r"const contracts = \{(.*?)\n\s*\};", src, re.S)
    if tbl:
        window = src[tbl.end(): tbl.end() + 900]
        comps = set(re.findall(r"<([A-Z]\w+)", window))
        for t in re.findall(r'\btype:\s*"([\w-]+)"', tbl.group(1)):
            explicit[t] = sorted(set(explicit.get(t, [])) | comps)
    blanket: dict[str, list[str]] = {}
    for m in re.finditer(r'route\.workspace === "([\w-]+)"(?!\s*&&\s*route\.section)', src):
        window = src[m.start(): m.start() + 900]
        if re.search(r"return \(?\s*<[A-Z]", window) and "route.objectType ===" not in window[:200]:
            blanket[m.group(1)] = sorted(set(re.findall(r"<([A-Z]\w+)", window)))
    return explicit, blanket


def files_for(comps: list[str], paths: dict[str, str]) -> list[str]:
    out = []
    for c in comps:
        spec = paths.get(c)
        resolved = _resolve_import(spec) if spec else None
        if resolved:
            out.append(resolved)
    return sorted(set(out))


def closure(front: list[str], depth: int = 3) -> set[str]:
    """The component files a screen can possibly render, following its own
    imports. Bounded: past three hops it stops being that screen's tree."""
    seen = {f for f in front if not f.endswith("(unresolved)")}
    frontier = set(seen)
    for _ in range(depth):
        nxt: set[str] = set()
        for f in frontier:
            nxt |= local_imports(f)
        nxt -= seen
        if not nxt:
            break
        seen |= nxt
        frontier = nxt
    return seen


def tab_mentions(tab: str, files: set[str]) -> int:
    """How many files of the screen's own tree name this tab.

    ZERO IS THE ONLY CONCLUSIVE READING: nothing in the tree that renders this
    section can branch on the tab, so the address resolves to a page that does
    not draw it. A non-zero count is NOT proof the tab is delivered -- a slug
    like `overview` appears for many reasons -- which is why the column below
    reports a count and never a verdict.
    """
    needle = f'"{tab}"'
    return sum(1 for f in files if needle in read(ROOT / f))


def cmd_pages(_a) -> int:
    screens = resolve()
    rows = [
        "# Page inventory — generated by `python scripts/screens.py pages`. Do not edit.",
        "",
        "Every address `navigation.ts` promises, at the grain a person clicks: the "
        "Level 2 sections AND the object tabs under them. `screens/board.md` is the "
        "same inventory stopped at Level 2.",
        "",
        "`branch` — what `ContentRouter` answers with. `explicit` names the object "
        "type; `blanket:<workspace>` catches every type of that workspace through one "
        "generic workbench, so the address works, which is not the same as that "
        "workbench drawing it; `UNANSWERED` means the click breaks.",
        "",
        "`draws` — how many files of the section's own component tree name the tab. "
        "**Only `0` concludes**: nothing there can branch on it. A count above zero is "
        "a place to look, not a verdict.",
        "",
        "| page | kind | branch | draws | spec |",
        "|---|---|---|---|---|",
    ]
    paths = component_paths()
    obj_explicit, obj_blanket = object_mounts()
    n_sections = n_tabs = n_undrawn = n_blanket = n_unanswered = 0
    for s in sorted(screens, key=lambda x: x.id):
        spec = Path(s.spec[0]).name if s.spec else "-"
        mounted = "mounted" if s.mounted_at != "NOT MOUNTED" else "**NOT MOUNTED**"
        rows.append(f"| `{s.id}` | section | {mounted} | - | {spec} |")
        n_sections += 1
        for o in s.objects:
            answered = o["answered"]
            if answered.startswith("blanket"):
                n_blanket += len(o["tabs"]) or 1
            elif answered == "UNANSWERED":
                n_unanswered += len(o["tabs"]) or 1
            mark = "**UNANSWERED**" if answered == "UNANSWERED" else answered
            # The tree rooted at the branch that ANSWERS this object type, not at
            # the section's collection component.
            comps = obj_explicit.get(o["type"]) or obj_blanket.get(s.surface) or []
            files = closure(files_for(comps, paths)) or closure(s.front)
            for tab in (o["tabs"] or ["(no tabs)"]):
                n_tabs += 1
                if not o["tabs"]:
                    # A type that declares no tab has nothing to look for. Printing
                    # `0` here would read as a finding; it is the absence of a
                    # question.
                    rows.append(f"| `{s.id}/o/{o['type']}` | tab | {mark} | - | {spec} |")
                    continue
                hits = tab_mentions(tab, files)
                if hits == 0:
                    n_undrawn += 1
                draws = f"{hits}" if hits else "**0**"
                rows.append(
                    f"| `{s.id}/o/{o['type']}/{tab}` | tab | {mark} | {draws} | {spec} |"
                )
    rows += [
        "",
        f"{n_sections} sections + {n_tabs} object tabs = **{n_sections + n_tabs} addressable pages**.",
        f"{n_undrawn} tabs are named by NO file of their section's own tree.",
        f"{n_blanket} tabs resolve through a blanket workbench; {n_unanswered} are unanswered.",
        "",
    ]
    PAGES_MD.parent.mkdir(exist_ok=True)
    PAGES_MD.write_text("\n".join(rows), encoding="utf-8")
    print(f"PAGES screens/pages.md  ({n_sections + n_tabs} pages: "
          f"{n_sections} sections + {n_tabs} tabs)")
    print(f"  {n_undrawn} tabs drawn by nothing in their section's tree")
    print(f"  {n_blanket} through a blanket workbench, {n_unanswered} unanswered")
    return 0


# ---------------------------------------------------------------------------
# NEXT -- the loop's selector.  One screen, chosen by a written rule.
# ---------------------------------------------------------------------------


def score(s: Screen) -> tuple[int, int, str]:
    """Lower sorts first. The rule, in order:
       1. a red local test  -- something is broken and measurable now
       2. mounted but no test at all -- unmeasurable, so unimprovable
       3. open acceptance criteria, most first
    """
    if s.verdict == "fail":
        return (0, 0, s.id)
    if s.front and not s.tests:
        return (1, 0, s.id)
    open_c = len(unmet(s))
    if open_c:
        return (2, -open_c, s.id)
    if not s.front:
        return (3, 0, s.id)
    return (4, 0, s.id)


def cmd_next(_a) -> int:
    screens = sorted(load_board(), key=score)
    s = screens[0]
    band = score(s)[0]
    why = ["a local test is red", "it is mounted and no test covers it",
           "its ratified acceptance criteria are still open",
           "it is not mounted at all", "nothing left"][band]
    print(f"NEXT  {s.id}   — {why}\n")
    return cmd_work(argparse.Namespace(id=s.id))


# ---------------------------------------------------------------------------
# CYCLE -- one turn of the wheel, with the delta against the previous turn.
# ---------------------------------------------------------------------------


def snapshot(screens: list[Screen]) -> dict:
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "screens": len(screens),
        "mounted": sum(1 for s in screens if s.front),
        "covered": sum(1 for s in screens if s.tests),
        "pass": sum(1 for s in screens if s.verdict == "pass"),
        "fail": sum(1 for s in screens if s.verdict == "fail"),
        "criteria_open": sum(1 for s in screens for c in s.criteria if c["verdict"] == "OPEN"),
        "criteria_unrecorded": sum(1 for s in screens for c in s.criteria if c["verdict"] == "UNRECORDED"),
    }


def cmd_cycle(a) -> int:
    before = None
    if HISTORY.exists():
        rows = [l for l in read(HISTORY).splitlines() if l.strip()]
        before = json.loads(rows[-1]) if rows else None

    screens = resolve()
    for s in screens:
        run_screen(s)
        print(f"{s.verdict:<10} {s.id:<34} {s.detail[:100]}")
    save_board(screens)
    write_board_md(screens)

    now = snapshot(screens)
    OUT.mkdir(exist_ok=True)
    with HISTORY.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(now, ensure_ascii=False) + "\n")

    print("\n" + "-" * 62)
    print("CYCLE — improvement is this column, measured against the last turn")
    for k in ("mounted", "covered", "pass", "fail", "criteria_open", "criteria_unrecorded"):
        d = (now[k] - before.get(k, 0)) if before else None
        print(f"  {k:<20} {now[k]:>4}" + (f"   ({d:+d})" if d is not None else ""))
    if before and all(now[k] == before.get(k) for k in ("pass", "covered", "criteria_open")):
        print("  ! this turn moved nothing — fix a screen before turning again")
    if a.push:
        cmd_sync(a)
    return 0


# ---------------------------------------------------------------------------
# SYNC -- the board, mirrored into GitHub issues. One issue per broken screen.
# ---------------------------------------------------------------------------


def gh(*args: str) -> tuple[int, str]:
    return run(["gh", *args], ROOT, timeout=120)


def cmd_sync(_a) -> int:
    for name, colour, desc in [("screen", "1d76db", "One Level 2 screen"),
                               ("red", "b60205", "Its local test is failing"),
                               ("uncovered", "fbca04", "No test covers it")]:
        gh("label", "create", name, "--color", colour, "--description", desc)

    code, out = gh("issue", "list", "--state", "all", "--label", "screen",
                   "--limit", "200", "--json", "number,title,state")
    have = {}
    if code == 0:
        for it in json.loads(out or "[]"):
            m = re.match(r"\[screen\]\s+(\S+)", it["title"])
            if m:
                have[m.group(1)] = it

    opened = closed = updated = 0
    for s in load_board():
        healthy = s.verdict == "pass" and not unmet(s)
        title = f"[screen] {s.id} — {s.title}"
        body = "\n".join([
            f"**Route** `{s.route}`  •  **Mount** `{s.mounted_at}`", "",
            "| | |", "|---|---|",
            f"| spec | {', '.join(f'`{p}`' for p in s.spec) or '—'} |",
            f"| front | {', '.join(f'`{p}`' for p in s.front) or '**not mounted**'} |",
            f"| test | {', '.join(f'`{p}`' for p in s.tests) or '**none**'} |",
            f"| last run | `{s.verdict}` — {s.detail} |", "",
            "```sh", s.run_cmd, "```", "",
            "### Acceptance criteria not cleared",
            *([f"- [ ] **{c['verdict']}** `{c['doc']}#{c['index']}` — {c['text']}" for c in unmet(s)]
              or ["_all cleared in completeness-ledger.json_"]),
            "", "<sub>Generated by `python scripts/screens.py sync`. Fix the front, not this issue.</sub>",
        ])
        labels = "screen" + (",red" if s.verdict == "fail" else "") + (",uncovered" if not s.tests else "")
        if s.id in have:
            num = str(have[s.id]["number"])
            gh("issue", "edit", num, "--title", title, "--body", body)
            if healthy and have[s.id]["state"] == "OPEN":
                gh("issue", "close", num, "--comment", "Green locally and no open criterion.")
                closed += 1
            else:
                updated += 1
        elif not healthy:
            if gh("issue", "create", "--title", title, "--body", body, "--label", labels)[0] == 0:
                opened += 1
    print(f"SYNC  +{opened} opened  ~{updated} updated  -{closed} closed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("resolve", cmd_resolve), ("work", cmd_work), ("test", cmd_test),
                     ("board", cmd_board), ("next", cmd_next), ("cycle", cmd_cycle),
                     ("backward", cmd_backward), ("pages", cmd_pages),
                     ("wiring", cmd_wiring), ("orphans", cmd_orphans), ("sync", cmd_sync)]:
        p = sub.add_parser(name)
        p.set_defaults(fn=fn)
        if name == "work":
            p.add_argument("id")
        if name == "test":
            p.add_argument("--id")
        if name == "cycle":
            p.add_argument("--push", action="store_true")
    a = ap.parse_args()
    for flag, default in (("id", None), ("push", False)):
        if not hasattr(a, flag):
            setattr(a, flag, default)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
