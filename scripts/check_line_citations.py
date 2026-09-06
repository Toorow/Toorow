"""Find `file:line` citations that a change has just made stale.

    python scripts/check_line_citations.py            # citations into changed files
    python scripts/check_line_citations.py --gate     # fail on a LIVE citation
                                                     # past EOF (dated records
                                                     # are reported, not blocking)
    python scripts/check_line_citations.py --all      # every citation in the repo
    python scripts/check_line_citations.py --ratchet  # fail on a NEW line citation
                                                     # into a DECOMPOSED file (AI-250)

WHY THIS EXISTS. Two consecutive stories of epic 58 were REJECTED at review for
the same defect, and neither introduced it on purpose:

  * 58.8 moved `_console_link` in `server/core/data_surface.py` and left four
    comments elsewhere pointing at `:351`, which by then was `return str(value)`;
  * 58.9 added a field to `ui/admin/src/shell/pages/ProjectSettings.tsx`, shifting
    the two change-set calls by seven lines, and shipped the old pair into a
    production comment AND into its own story file.

The class is mechanical: this repository cites `path:line` heavily and on
purpose -- a citation is how a comment proves it is talking about real code
rather than an intention. That is worth keeping. What it costs is that ANY diff
which moves lines in a cited file silently invalidates every citation pointing
past the change, in files the diff never opened. No test covers it, because a
stale citation breaks nothing at runtime. It only lies to the next reader, which
is the exact failure this repository pays the most for.

WHAT IT CAN AND CANNOT DECIDE. Whether `foo.py:120` still points at the right
line needs to know what the citation MEANT, which is not recoverable from the
text. So this script does not judge -- it narrows. It answers "which citations
could this change have broken", prints each with the line it currently lands on,
and lets a person confirm or repair in seconds instead of grepping. Only one
verdict is objective enough to fail a gate: a citation past the end of its file
is broken with no interpretation needed.

WHAT THE GATE IS ALLOWED TO BLOCK ON -- decided 2026-08-17, AI-250. The gate
guards the citations of LIVE CODE and RATIFIED DOCUMENTS, the two kinds of text
this repository rewrites when reality moves. It reports, and never blocks on, a
DATED RECORD: a story file, an applied migration, a `SESSIONS.md` entry, an
audit report under `reviews/`. The reason is one sentence and it is the ratified
rule of AI-250 -- a dated report said something true on the DAY IT WAS WRITTEN,
and is never rewritten afterwards. Blocking on one would ask a session to edit
another session's history to make an instrument green, which is the opposite of
what the instrument is for. The scope widening of the same day put `reviews/`
inside the WALK on purpose -- a directory a guard cannot see is not clean, it is
silent -- and that intent is kept whole: the citations are still resolved, still
printed, still counted. Only the rc stops answering for them. The rule outranks
the instrument, so it is the instrument's scope that moved.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: `path/to/file.ext:123`, optionally a range `:123-130`. The path must carry a
#: directory separator or a known extension, so prose like "story 58.9:2" and
#: timestamps like "10:30" are not mistaken for citations.
CITATION = re.compile(
    r"(?P<path>[A-Za-z0-9_./\\-]+\.(?:py|tsx?|sql|json|md|css|ya?ml))"
    r":(?P<line>\d+)(?:-(?P<end>\d+))?"
)

#: `path/to/file.py#symbol` or `path/to/file.tsx#route/key` -- the anchor form
#: `resolve_stale_citations.py` writes, and the form that does NOT rot when a
#: diff moves lines. It is checked here rather than left alone: an anchor nobody
#: verifies is worse than a number, because a number at least goes visibly past
#: the end of its file, while a symbol that was renamed just sits there reading
#: as correct. The check is exact-string presence, which is all this form
#: promises -- the symbol or the route key appears in the file, or it does not.
#:
#: NARROWED TWICE, and the narrowing is the interesting part: `path#name` is
#: already THREE different languages in this repository, and a first version of
#: this rule reported 767 findings that were nearly all one of the other two.
#:
#:   `epic-41-....md#Story-41`                    a markdown heading anchor
#:   `mediaplan_store.py#L44-66`                  a GitHub line fragment
#:   `notebook_mcp.py#symbol:module.foo.a1b2c3`   a finished-work-audit locator
#:
#: So: code targets only (a markdown anchor belongs to the document, not to a
#: symbol table), no `L<digits>`, and nothing followed by `:` -- the locator
#: form carries its own resolution after the colon and is checked by the audit
#: that writes it, not here.
SYMBOL_CITATION = re.compile(
    r"(?P<path>[A-Za-z0-9_./\\-]+\.(?:py|tsx?|sql))"
    r"#(?!L\d)(?P<symbol>[A-Za-z0-9_/-]+)(?![:\w])"
)

#: Where citations are written. Scanning the whole tree would read node_modules.
#:
#: `e2e` AND THE REPOSITORY ROOT WERE MISSING, and the omission is the exact
#: failure this file exists to catch. Measured 2026-08-17 while repairing AI-250:
#: `e2e/AUTONOMIE.md` carried two citations into `admin_api.py` (`:247`, `:7375`)
#: and `e2e/gates/g5_org.py` one (`:11646`) -- all three past the end of a file
#: that had shrunk from 21 727 lines to 2 886, and this gate reported NONE of
#: them, because it never opened the directory. A guard whose scope stops short
#: of a directory does not report "clean" for that directory; it says nothing at
#: all, and the silence reads as clean. `reviews/`, `SESSIONS.md`, `CLAUDE.md`
#: and `TOOLBOX.md` were invisible for the same reason.
SEARCH_ROOTS = (
    "server", "ui/admin/src", "scripts", "docs", "screens", "_bmad-output",
    "infra", "e2e", "reviews", "supabase", "dbt",
)

#: Files that live at the repository root, which `rglob` over SEARCH_ROOTS cannot
#: reach. Named rather than globbed: the root also holds lockfiles and generated
#: manifests, and reading those buys nothing.
ROOT_FILES = ("SESSIONS.md", "CLAUDE.md", "TOOLBOX.md", "AGENTS.md", "CONTRIBUTING.md", "README.md")

SKIP_PARTS = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", "coverage"}

READABLE = {".py", ".ts", ".tsx", ".sql", ".md", ".json", ".css", ".yaml", ".yml"}


_HUNK = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")


def _changed_files() -> dict[str, int]:
    """Changed path -> the FIRST line the change touched, in the OLD numbering.

    The first changed line is what makes this usable. A citation pointing ABOVE
    the earliest edit cannot have moved -- only lines at or below it shift. On
    this repository that is the difference between 316 citations to re-read and
    a handful, which is the difference between a tool someone runs and a tool
    someone ignores. A file that is new or unreadable gets line 1: everything in
    it is suspect.
    """
    out: dict[str, int] = {}
    # Decoded here rather than by `text=True`: a diff carrying one byte the
    # ambient codepage cannot read used to raise UnicodeDecodeError, leave
    # `stdout` as None, and take this function down -- while `main()` still
    # returned 0. A gate that cannot read its input must SAY so, never pass
    # quietly; three stories in a row shipped a stale citation under that
    # silent rc=0 (measured 2026-08-08, story 60.2).
    try:
        result = subprocess.run(
            ["git", "diff", "-U0", "HEAD"], cwd=ROOT, capture_output=True, check=False
        )
    except OSError as exc:
        raise SystemExit(f"check_line_citations: could not read the diff ({exc}).") from exc
    if result.stdout is None:
        raise SystemExit("check_line_citations: `git diff` produced no readable output.")
    diff_text = result.stdout.decode("utf-8", errors="replace")

    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:].strip()
            continue
        if current and (match := _HUNK.match(line)):
            first = int(match.group("old")) or 1
            out[current] = min(out.get(current, first), first)

    try:
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        for line in untracked.stdout.splitlines():
            if line.strip():
                out[line.strip()] = 1
    except OSError:
        pass
    return out


#: Sources whose citations are a DATED RECORD rather than a live claim. A story
#: file said something true on the day it was written and is never rewritten
#: afterwards; failing a gate on it would ask a session to edit another
#: session's history. They are still reported, never blocking.
#: An APPLIED migration is immutable -- CLAUDE.md §3, and the ledger stores its
#: checksum. A citation rotting inside one cannot be repaired at all: the only
#: legal edit is a NEW migration, and nobody writes one to fix a comment. So it
#: is reported and never blocking, for the same reason as a dated record.
#: `SESSIONS.md` JOINS THEM for the identical reason, now that the walk can see
#: it: every entry is another session's dated declaration, and this repository's
#: working rules forbid editing it outside one's own line. Blocking a gate on a
#: citation inside somebody else's dated entry would demand exactly the edit the
#: rules refuse. Its citations are reported, never blocking -- which is also why
#: the two `admin_api.py:<n>` citations AI-250 could not repair are still there.
_HISTORICAL_PREFIXES = ("_bmad-output/", "infra/nango/migrations/", "SESSIONS.md")

#: DATED AUDIT REPORTS. `reviews/audit-2026-08-17/13-connecteurs.md:140` cites a
#: connector file that has since lost twelve lines, and on 2026-08-17 that single
#: citation reddened the gate. There is no repair: the report carries the date of
#: its reading in its own path, it described what that file held THAT DAY, and
#: rewriting it would make the report lie about when it was true. That is the
#: ratified rule of AI-250 -- "un fichier de story disait vrai le jour ou il a
#: ete ecrit" -- and it holds for an audit report for the identical reason. So
#: the class joins the dated records: reported, never blocking.
#:
#: SEPARATE FROM `_HISTORICAL_PREFIXES` BECAUSE THE RATCHET STILL WANTS IT. Two
#: different questions are being asked. "Is this stale citation repairable?" --
#: no, for both lists, which is why neither blocks the gate. "Is someone writing
#: a NEW line citation into a decomposed file right now?" -- for a report being
#: drafted today that is a live question with a cheap answer (cite `path#symbol`
#: instead), and 35 of the ratchet's 46 baselined entries live under `reviews/`.
#: Folding this prefix into `_HISTORICAL_PREFIXES` would delete that coverage
#: silently, which is the failure the 2026-08-17 widening was written to end.
_DATED_REPORT_PREFIXES = ("reviews/",)

#: What the gate refuses to answer for. The ratchet reads the narrower list.
_NEVER_BLOCKING_PREFIXES = _HISTORICAL_PREFIXES + _DATED_REPORT_PREFIXES


def _is_historical(path: str, changed: dict[str, int] | None = None) -> bool:
    """A record, unless THIS change is writing it.

    The story file a session is drafting right now is a live claim -- the 58.9
    review found a stale citation inside one. The story file of epic 41 is a
    record of what was true in July. The diff is what tells them apart, and it
    tells an audit report apart the same way: the one being written in this diff
    answers for its citations, the ones already dated do not.
    """
    if not path.startswith(_NEVER_BLOCKING_PREFIXES):
        return False
    return not (changed and path in changed)


_WALK_CACHE: list[Path] | None = None
_BY_NAME: dict[str, list[Path]] = {}
_LINES_CACHE: dict[Path, list[str]] = {}


def _walk() -> list[Path]:
    """Every readable file once. Cached: the resolver asks for it per citation,
    and this repository carries thousands."""
    global _WALK_CACHE
    if _WALK_CACHE is not None:
        return _WALK_CACHE
    files: list[Path] = []
    for root in SEARCH_ROOTS:
        base = ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in READABLE:
                continue
            if SKIP_PARTS & set(path.parts):
                continue
            files.append(path)
    for name in ROOT_FILES:
        candidate = ROOT / name
        if candidate.is_file():
            files.append(candidate)
    _WALK_CACHE = files
    for path in files:
        _BY_NAME.setdefault(path.name, []).append(path)
    return files


def _lines(path: Path) -> list[str]:
    cached = _LINES_CACHE.get(path)
    if cached is None:
        try:
            cached = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cached = []
        _LINES_CACHE[path] = cached
    return cached


def _resolve(cited: str) -> Path | None:
    """A citation is written relative to the repo root, or as a bare basename."""
    cited = cited.replace("\\", "/")
    direct = ROOT / cited
    if direct.is_file():
        return direct
    _walk()
    # `data_surface.py:354` with no directory -- resolve only if unambiguous.
    if "/" not in cited:
        matches = _BY_NAME.get(cited, [])
        return matches[0] if len(matches) == 1 else None
    # `core/data_surface.py:354` -- a suffix of a real path. Only files sharing
    # the basename can match, so the candidate set is already tiny.
    basename = cited.rsplit("/", 1)[-1]
    matches = [
        p for p in _BY_NAME.get(basename, [])
        if str(p.relative_to(ROOT)).replace("\\", "/").endswith(cited)
    ]
    return matches[0] if len(matches) == 1 else None


#: FILES WHOSE LINE NUMBERS NAME NOTHING DURABLE, because their content was
#: moved out from under them. `peak -> today` measured 2026-08-17 by:
#:
#:     for rev in $(git log --format=%h -- <path>); do
#:         git show "$rev:<path>" | wc -l
#:     done
#:
#: The rule for being on this list: the file holds less than two thirds of the
#: lines it once held. That is not a style opinion, it is what makes a number
#: meaningless -- when a third of a file has left, a citation into it points at
#: whatever slid into the gap, and it does so SILENTLY. AI-250 measured the
#: silence: of the 29 live `admin_api.py:<n>` citations, 23 were past the end of
#: the file (visibly broken) and the remaining 6 were inside it and every single
#: one landed on unrelated code -- `:247` on an import block, `:1325` on blank
#: lines, `:2090` on a retired preview handler. Past-EOF is the lucky case.
DECOMPOSED: dict[str, tuple[int, int, str]] = {
    "server/core/admin_api.py": (21727, 2886, "AD-43 -- every handler left for its subject module"),
    "server/core/main.py": (5372, 924, "the `register(mcp)` split by surface"),
    "server/core/csv_excel_import.py": (4357, 218, "file-source ingestion split out"),
    "ui/admin/src/shell/ContentRouter.tsx": (742, 280, "routes declare themselves at home"),
    "ui/admin/src/shell/navigation.ts": (620, 172, "inverted registration -- AD-42"),
    "ui/admin/src/App.tsx": (404, 243, "shell primitives extracted"),
}

#: The line-number citations into those files that EXISTED on 2026-08-17, after
#: AI-250 repaired every one it could resolve. Form: `source -> target:line`, the
#: source FILE rather than its line, so that editing the source does not churn
#: this list.
#:
#: CLIQUET, PAS MUR -- the same shape as
#: `check_canonical_target_classification.py`. Refusing all 58 at once would stop
#: the repository without repairing anything, and 29 of them are today's audit
#: notes citing lines that are still correct. So they are listed, and the gate
#: reddens on what is NOT listed: a NEW line citation into a decomposed file is
#: refused at the moment someone writes it, which is the only moment repairing it
#: is cheap. Removing a line from this list without removing the citation reddens
#: too -- the ratchet turns one way.
#:
#: The repair for a refusal is never "add it here": it is to cite
#: `path#symbol` instead. That form does not rot when lines move, and this same
#: script already checks that the symbol exists.
LINE_CITATIONS_INTO_DECOMPOSED_AT_2026_08_17: frozenset[str] = frozenset(
    {
        "docs/product-architecture/known-debt.json -> ui/admin/src/shell/ContentRouter.tsx:19",
        "docs/product-architecture/known-debt.json -> ui/admin/src/shell/ContentRouter.tsx:416",
        "reviews/audit-2026-08-17/01-overview.md -> ui/admin/src/shell/ContentRouter.tsx:113",
        "reviews/audit-2026-08-17/01-overview.md -> ui/admin/src/shell/ContentRouter.tsx:158",
        "reviews/audit-2026-08-17/02-analyse.md -> server/core/admin_api.py:2817",
        "reviews/audit-2026-08-17/03-analytics.md -> ui/admin/src/shell/ContentRouter.tsx:40",
        "reviews/audit-2026-08-17/04-data.md -> server/core/admin_api.py:2567",
        "reviews/audit-2026-08-17/04-data.md -> server/core/admin_api.py:2757",
        "reviews/audit-2026-08-17/04-data.md -> server/core/admin_api.py:2884",
        "reviews/audit-2026-08-17/04-data.md -> server/core/csv_excel_import.py:74",
        "reviews/audit-2026-08-17/04-data.md -> server/core/main.py:142",
        "reviews/audit-2026-08-17/06-modules-cross-datastream.md -> server/core/admin_api.py:2810",
        "reviews/audit-2026-08-17/06-modules-cross-datastream.md -> server/core/admin_api.py:2822",
        "reviews/audit-2026-08-17/06-modules-cross-datastream.md -> server/core/main.py:198",
        "reviews/audit-2026-08-17/06-modules-cross-datastream.md -> server/core/main.py:212",
        "reviews/audit-2026-08-17/06-modules-cross-datastream.md -> server/core/main.py:251",
        "reviews/audit-2026-08-17/08-context-hub.md -> server/core/main.py:328",
        "reviews/audit-2026-08-17/09-ai-route.md -> server/core/admin_api.py:2576",
        "reviews/audit-2026-08-17/09-ai-route.md -> server/core/main.py:801",
        "reviews/audit-2026-08-17/10-mcp-app.md -> server/core/admin_api.py:218",
        "reviews/audit-2026-08-17/10-mcp-app.md -> server/core/admin_api.py:2613",
        "reviews/audit-2026-08-17/10-mcp-app.md -> server/core/main.py:117",
        "reviews/audit-2026-08-17/10-mcp-app.md -> server/core/main.py:282",
        "reviews/audit-2026-08-17/10-mcp-app.md -> server/core/main.py:307",
        "reviews/audit-2026-08-17/10-mcp-app.md -> server/core/main.py:436",
        "reviews/audit-2026-08-17/10-mcp-app.md -> server/core/main.py:749",
        "reviews/audit-2026-08-17/10-mcp-app.md -> server/core/main.py:834",
        "reviews/audit-2026-08-17/12-acces-utilisateurs.md -> server/core/admin_api.py:1092",
        "reviews/audit-2026-08-17/12-acces-utilisateurs.md -> server/core/admin_api.py:1124",
        "reviews/audit-2026-08-17/12-acces-utilisateurs.md -> server/core/admin_api.py:363",
        "reviews/audit-2026-08-17/13-connecteurs.md -> server/core/admin_api.py:68",
        "reviews/review-3-6.md -> server/core/main.py:401",
        "reviews/review-3-7.md -> server/core/main.py:345",
        "reviews/review-3-7.md -> server/core/main.py:373",
        "reviews/review-3-7.md -> server/core/main.py:376",
        "reviews/review-3-7.md -> server/core/main.py:401",
        "reviews/review-3-7.md -> server/core/main.py:621",
        "screens/architecture.md -> ui/admin/src/shell/navigation.ts:123",
        "scripts/finished_work_audit.py -> ui/admin/src/shell/ContentRouter.tsx:19",
        "server/tests/conformance/test_pull_contract.py -> server/core/main.py:243",
        "server/tests/conformance/test_pull_contract.py -> server/core/main.py:264",
        "server/tests/isolation/test_mcp_tool_scope_refusal.py -> server/core/main.py:3303",
        "server/tests/isolation/test_mcp_tool_scope_refusal.py -> server/core/main.py:3435",
        "ui/admin/src/governance/GovernanceCollection.tsx -> ui/admin/src/shell/ContentRouter.tsx:174",
        "ui/admin/src/governance/contracts.ts -> ui/admin/src/shell/ContentRouter.tsx:174",
    }
)


#: A LINE OF THE BASELINE LITERAL BELOW, which is not a citation but a record of
#: one. Without this the ratchet reads its own list, calls all 58 entries NEW
#: because their source file is now this script, and reports 55 refusals on its
#: first run -- measured. `resolve_stale_citations.py` warns about the same trap
#: in its own docstring ("prose about citations must not contain a live one").
#:
#: Matched by SHAPE rather than by skipping this whole file: an entry is exactly
#: `"<source> -> <target>:<line>",` on its own line. Everything else here is
#: still scanned, so a real citation written in this script is still caught.
_BASELINE_ENTRY = re.compile(r'^"[A-Za-z0-9_./-]+ -> [A-Za-z0-9_./-]+:\d+",$')


def decomposed_line_citations() -> dict[str, str]:
    """`{"source -> target:line": the text of the citing line}`, live sources only.

    WHAT THIS DOES NOT LOOK AT, stated because a guard that hides its blind spots
    is worse than none: dated records (`_bmad-output/`, applied migrations,
    `SESSIONS.md`) are skipped, because they are never rewritten and a refusal
    there asks a session to edit another session's history. Two `admin_api.py:<n>`
    citations live in `SESSIONS.md` for exactly that reason and this function
    will never see them.

    `reviews/` IS NOT SKIPPED HERE, and the asymmetry with the gate is deliberate
    (2026-08-17, AI-250). The gate asks whether a stale citation can be repaired;
    in a dated report it cannot, so it never blocks there. The ratchet asks
    whether a citation form that is known to rot is being written ANEW -- and the
    session drafting today's audit report can still choose `path#symbol`, at the
    only moment choosing it is free. 35 of the 46 baselined entries are review
    reports; dropping them would return this directory to the silence the walk
    was widened to break.
    """
    found: dict[str, str] = {}
    for source in _walk():
        source_rel = str(source.relative_to(ROOT)).replace("\\", "/")
        if source_rel.startswith(_HISTORICAL_PREFIXES):
            continue
        for lineno, line in enumerate(_lines(source), start=1):
            if _BASELINE_ENTRY.match(line.strip()):
                continue
            for match in CITATION.finditer(line):
                target = _resolve(match.group("path"))
                if target is None:
                    continue
                target_rel = str(target.relative_to(ROOT)).replace("\\", "/")
                if target_rel not in DECOMPOSED:
                    continue
                key = f"{source_rel} -> {target_rel}:{match.group('line')}"
                found.setdefault(key, f"{source_rel}:{lineno}  {line.strip()[:110]}")
    return found


def _ratchet() -> int:
    """Refuse a NEW `path:line` citation into a decomposed file. One way only."""
    found = decomposed_line_citations()
    baseline = LINE_CITATIONS_INTO_DECOMPOSED_AT_2026_08_17

    print("decomposed files (peak -> today), and the line citations still aimed at them:")
    for path, (peak, today, why) in sorted(DECOMPOSED.items()):
        live = sum(1 for k in found if f" -> {path}:" in k)
        print(f"  {path}: {peak} -> {today} lines ({why}) -- {live} cited by line")
    print(f"\nlisted on 2026-08-17: {len(baseline)}   present now: {len(found)}")

    new = sorted(set(found) - baseline)
    gone = sorted(baseline - set(found))

    if new:
        print(f"\nREFUSED: {len(new)} NEW line citation(s) into a decomposed file.")
        print("A line number in these files names whatever slid into the gap, and it")
        print("does it silently. Cite `path#symbol` instead -- the symbol survives a diff.")
        for key in new:
            print(f"  {key}\n      at {found[key]}")
    if gone:
        print(f"\nREFUSED: {len(gone)} listed citation(s) no longer exist.")
        print("The ratchet turns one way: delete these lines from")
        print("LINE_CITATIONS_INTO_DECOMPOSED_AT_2026_08_17 in this file.")
        for key in gone:
            print(f"  {key}")
    if new or gone:
        return 1
    print("\nOK: no new line citation into a decomposed file, no stale line in the list.")
    return 0


def main() -> int:
    # The OUTPUT side of the same guarantee the diff-reading code states at its
    # `git diff` call: a gate that cannot WRITE its report must not die halfway
    # through it. On a cp1252 console, one `↔` inside a reported line
    # killed the print after the verdict but before the listing -- measured
    # 2026-08-17 while repairing AI-250. Replacement is enough: the report is
    # for a reader, and a `?` in a quoted line loses nothing the gate decides.
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gate", action="store_true",
        help="exit 1 on a citation past end of file, in live code or a ratified doc",
    )
    parser.add_argument("--all", action="store_true", help="report every citation, not only changed targets")
    parser.add_argument(
        "--ratchet", action="store_true",
        help="exit 1 on a NEW line citation into a decomposed file (AI-250)",
    )
    args = parser.parse_args()

    if args.ratchet:
        return _ratchet()

    changed = {c.replace("\\", "/"): first for c, first in _changed_files().items()}

    past_eof: list[str] = []
    past_eof_blocking: list[str] = []
    suspect: list[str] = []
    seen: set[tuple[str, str, int]] = set()

    for source in _walk():
        try:
            text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source_rel = str(source.relative_to(ROOT)).replace("\\", "/")
        for lineno, line in enumerate(text.splitlines(), start=1):
            # The ratchet's baseline records citations; it does not make them.
            # Skipped here as well as in `decomposed_line_citations()`: without
            # it, --gate reported 15 findings that were this file's own list of
            # the citations it is tracking -- measured on the first run after the
            # list landed.
            if _BASELINE_ENTRY.match(line.strip()):
                continue
            for match in SYMBOL_CITATION.finditer(line):
                target = _resolve(match.group("path"))
                if target is None:
                    continue
                target_rel = str(target.relative_to(ROOT)).replace("\\", "/")
                symbol = match.group("symbol")
                body = "\n".join(_lines(target))
                if symbol in body:
                    continue
                entry = (
                    f"  {source_rel}:{lineno} cites {target_rel}#{symbol}"
                    " -- that name is not in the file"
                )
                past_eof.append(entry)
                if not _is_historical(source_rel, changed):
                    past_eof_blocking.append(entry)

            for match in CITATION.finditer(line):
                cited_raw = match.group("path")
                cited_line = int(match.group("line"))
                cited_end = int(match.group("end")) if match.group("end") else None
                target = _resolve(cited_raw)
                if target is None:
                    continue
                target_rel = str(target.relative_to(ROOT)).replace("\\", "/")

                # A RANGE WHOSE END PRECEDES ITS START NAMES NOTHING, and until
                # 2026-08-09 this script never looked: only the first number was
                # read, so `:538-446` passed with rc=0. Eleven such ranges lived
                # in the repository, all born the same way -- a renumbering pass
                # shifted the START of a range and left the END at its old value.
                # This needs no interpretation, so it blocks like a past-EOF.
                if cited_end is not None and cited_end < cited_line:
                    entry = (
                        f"  {source_rel}:{lineno} cites {target_rel}:{cited_line}-{cited_end}"
                        " -- the range ends before it starts, so it names nothing"
                    )
                    past_eof.append(entry)
                    if not _is_historical(source_rel, changed):
                        past_eof_blocking.append(entry)
                    continue
                # A file citing its own relative line is usually a self-reference
                # in prose; keep it, it rots the same way.
                key = (source_rel, target_rel, cited_line)
                if key in seen:
                    continue
                seen.add(key)

                target_lines = _lines(target)
                if not target_lines:
                    continue

                if cited_line > len(target_lines):
                    entry = (
                        f"  {source_rel}:{lineno} cites {target_rel}:{cited_line}"
                        f" -- that file has {len(target_lines)} lines"
                    )
                    past_eof.append(entry)
                    if not _is_historical(source_rel, changed):
                        past_eof_blocking.append(entry)
                    continue

                # Only citations at or below the earliest edit can have moved,
                # and only a LIVE source is asked to answer for one.
                first_changed = changed.get(target_rel)
                moved = first_changed is not None and cited_line >= first_changed
                live = not _is_historical(source_rel, changed)
                if args.all or (moved and live and source_rel != target_rel):
                    landed = target_lines[cited_line - 1].strip()
                    suspect.append(
                        f"  {source_rel}:{lineno}\n"
                        f"      cites {target_rel}:{cited_line}\n"
                        f"      lands on: {landed[:110]}"
                    )

    if past_eof_blocking:
        print(
            f"BROKEN citations in live code or ratified docs ({len(past_eof_blocking)})"
            " -- past end of file, no interpretation needed:"
        )
        print("\n".join(sorted(past_eof_blocking)))
        print()

    historical_eof = len(past_eof) - len(past_eof_blocking)
    if historical_eof:
        print(
            f"past end of file in dated records ({historical_eof}) -- reported, never blocking:"
            " a story file or a dated audit report said something true on the day"
            " it was written, and is never rewritten afterwards."
        )
        print()

    if suspect:
        scope = "every citation" if args.all else "citations into files this change touched"
        print(f"{scope} ({len(suspect)}) -- confirm each still means what it says:")
        print("\n".join(sorted(suspect)))
        print()
    elif not args.all:
        print("no citation points into a file this change touched")

    if args.gate and past_eof_blocking:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
