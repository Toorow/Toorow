"""Say what a broken `file:line` citation MEANT, and where that code lives now.

    python scripts/resolve_stale_citations.py            # every broken citation
    python scripts/resolve_stale_citations.py --target server/core/admin_api.py
    python scripts/resolve_stale_citations.py --apply    # rewrite the resolvable ones

WHY THIS EXISTS, AND WHY IT IS NOT `check_line_citations.py`. That script answers
"which citations are broken" and refuses to guess further, on purpose: whether
`foo.py:120` still points at the right line needs to know what the citation
MEANT, which is not recoverable from the text.

It is recoverable from the HISTORY. A citation past the end of its file was
written when the file was longer, and git still holds that file. So:

  1. walk the target file's revisions from newest to oldest, and stop at the
     first one long enough to contain the cited line -- the newest version the
     citation could have been describing;
  2. read the line there, and the `def` / `class` / `Route(` that encloses it;
  3. find where that symbol lives in the tree TODAY.

Step 3 is what makes the answer useful rather than archaeological: a citation
into a line number of `admin_api.py` resolves to the handler AD-43 moved into a
per-subject module, and the repair is to cite that module.

(This paragraph once carried a real `path:line` example. The tool rewrote it, in
its own docstring, exactly as designed -- and the sentence stopped making sense.
Prose about citations must not contain a live one.)

THE OUTPUT IS A SYMBOL, NOT A NEW NUMBER. `path#symbol` does not go stale when
lines move, which is the whole failure this class keeps re-running (AI-250: 106
broken citations, all of them produced by four refactors that shrank four files).
A number is right until the next diff; a symbol is right until someone renames
it, and a rename is a thing a reader can follow.

WHAT IT REFUSES TO DO. When the enclosing symbol cannot be read, or when it no
longer exists anywhere in the tree, this prints UNRESOLVED and changes nothing.
A citation rewritten to a symbol that does not exist would be worse than a
broken one: it would look repaired.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Same shape `check_line_citations.py` recognises, so the two agree on what a
#: citation IS. Duplicated rather than imported: that script is a gate, and a
#: gate that imports a repair tool can be broken by a change to the repair tool.
CITATION = re.compile(
    r"(?P<path>[A-Za-z0-9_./\\-]+\.(?:py|tsx?|sql|json|md|css|ya?ml))"
    r":(?P<line>\d+)(?:-(?P<end>\d+))?"
)

SEARCH_ROOTS = ("server", "ui/admin/src", "scripts", "docs", "screens", "_bmad-output", "infra")
SKIP_PARTS = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", "coverage"}
READABLE = {".py", ".ts", ".tsx", ".sql", ".md", ".json", ".css", ".yaml", ".yml"}

#: DATED RECORDS ARE NOT REPAIRED. The same prefixes `check_line_citations.py`
#: refuses to block on, and for the identical reason: a story file said
#: something true on the day it was written, and an applied migration header is
#: a fact with a checksum. Rewriting either would edit a record to match the
#: present, which is the one thing a record must not do. They are still SCANNED,
#: so `--target` reports what they cite, but never rewritten.
#:
#: `SESSIONS.md` AND `reviews/` ADDED 2026-08-17 to keep that first sentence
#: TRUE. Both joined the other script's never-blocking set that day -- an audit
#: report under `reviews/` carries the date of its reading in its own path and
#: described what a file held THAT DAY (AI-250). Neither is inside this script's
#: `SEARCH_ROOTS` today, so this changes nothing that runs; it means that the
#: day someone widens the roots the way `check_line_citations.py` was widened,
#: the repairer does not start rewriting records. A guarantee that holds only
#: because a directory is out of scope is not a guarantee, it is a coincidence.
HISTORICAL_PREFIXES = (
    "_bmad-output/", "infra/nango/migrations/", "SESSIONS.md", "reviews/",
)

#: A symbol definition, in the two languages this repository cites.
_PY_SYMBOL = re.compile(r"^(?:async\s+)?def\s+(\w+)|^class\s+(\w+)")
_TS_SYMBOL = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+(\w+)"
    r"|^(?:export\s+)?(?:const|let)\s+(\w+)\s*[:=]"
    r"|^(?:export\s+)?(?:interface|type|class)\s+(\w+)"
)


def _run(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout


def _revisions(rel: str) -> list[str]:
    return [r for r in _run("log", "--format=%h", "--", rel).splitlines() if r]


def _written_at(source_rel: str, lineno: int) -> str | None:
    """When the CITING line itself was written, as an ISO date.

    THE REVISION HAS TO BE CONTEMPORANEOUS WITH THE CITATION, and getting this
    wrong is how the tool produced confident nonsense. `admin_api.py` shrank in
    stages -- 21 727, then 20 465, 16 776, 8 774, 3 104, 2 886 -- so line 7 596
    names a DIFFERENT place in each of them. Picking "the newest revision long
    enough" answered `_revoke_source_delegation` for a citation whose own prose
    says « GET /api/organizations has always selected it »: correct for the
    8 774-line version, and meaningless for the one the author was reading.

    So the author's own commit date decides which version to open. A line whose
    blame cannot be read yields None, and the caller then refuses rather than
    falls back -- a fallback here is precisely what produced the wrong answers.
    """
    out = _run("blame", "-L", f"{lineno},{lineno}", "--porcelain", "--", source_rel)
    for line in out.splitlines():
        if line.startswith("author-time "):
            try:
                stamp = int(line.split()[1])
            except (IndexError, ValueError):
                return None
            from datetime import datetime, timezone  # noqa: PLC0415

            return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()
    return None


def _revision_at(rel: str, when: str) -> str | None:
    """The last revision of *rel* at or before *when*."""
    out = _run("rev-list", "-1", f"--before={when}", "HEAD", "--", rel).strip()
    return out or None


def _blob(rev: str, rel: str) -> list[str] | None:
    out = subprocess.run(
        ["git", "show", f"{rev}:{rel}"], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if out.returncode != 0:
        return None
    return out.stdout.splitlines()


def _enclosing_symbol(lines: list[str], index: int) -> str | None:
    """The `def`/`class`/`function` whose body contains ``lines[index]``.

    Walks UP and takes the first definition at column 0 or one indent level in.
    A nested helper is skipped in favour of its parent when the parent is what a
    reader would search for -- but a top-level definition always wins, which is
    what the indentation bound expresses.
    """
    pattern = _PY_SYMBOL if any(line.startswith(("def ", "class ", "import ")) for line in lines[:60]) else _TS_SYMBOL
    for i in range(min(index, len(lines) - 1), -1, -1):
        raw = lines[i]
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent > 4:
            continue
        m = pattern.match(raw.lstrip())
        if m:
            return next((g for g in m.groups() if g), None)
        # THE WALK IS BOUNDED, and this line is the whole reason.
        #
        # Without it the walk climbs PAST the end of a function into whatever
        # definition happens to sit further up. Measured on the citation
        # `admin_api.py` line 6 294 of that revision: `*_MEDIAPLAN_ROUTES,` -- an entry in
        # the module-level ROUTE TABLE, inside no function at all -- and the
        # unbounded walk answered `_rollback_publication_review_console`, a
        # handler 368 lines earlier with nothing to do with it. Four different
        # citations resolved to that same symbol, which is what gave it away.
        #
        # A citation rewritten to the WRONG symbol is worse than a broken one:
        # a broken one is visibly broken, and this one reads as repaired. So any
        # other statement at column 0 -- a module-level assignment, an import, a
        # decorator, a comment -- ENDS the search: the cited line was not inside
        # a definition, and no name may be invented for it.
        if indent == 0:
            return None
    return None


#: A route key on the cited line itself -- `case "analyze/explore":`, or a bare
#: quoted id in a route table. The SECOND strategy, and the one that matters for
#: the shell: `ContentRouter.tsx` and `navigation.ts` are dispatch tables, so a
#: cited line there sits inside no function at all and the enclosing-symbol walk
#: returns nothing. What the citation meant is the ROUTE, and a route key is a
#: better anchor than a symbol anyway -- it is the thing the reader searches for,
#: it survives the file being rewritten around it, and `screens/expectations.md`
#: already names pages by exactly these keys.
_ROUTE_KEY = re.compile(r"""case\s+["']([\w/-]+)["']\s*:|^\s*["']([\w/-]+)["']\s*:""")


def _route_key(line: str) -> str | None:
    m = _ROUTE_KEY.search(line)
    if not m:
        return None
    key = next((g for g in m.groups() if g), None)
    # A single word is too weak to anchor on -- `case "list":` appears in a dozen
    # switches. A path-shaped key names one destination.
    return key if key and "/" in key else None


#: A DECLARED IDENTIFIER on the cited line -- `section("regression-runs", ...)`,
#: `slug: "classifications"`, `key: "governance"`, `type: "datastream"`. The
#: THIRD strategy, for declaration tables that are neither functions nor
#: route switches: `navigation.ts` is a nested literal, so a cited line there sits
#: inside no symbol at all and carries no path-shaped route key -- but it does
#: name the thing it declares, and that name is what a reader searches for.
_DECLARED_ID = re.compile(
    r"""\bsection\(\s*["']([\w/-]+)["']"""
    r"""|\b(?:slug|key|id|type)\s*:\s*["']([\w/-]+)["']"""
)


#: A route-table entry naming its handler: `Route("/api/x", endpoint=_h, ...)`.
_ROUTE_ENDPOINT = re.compile(r"endpoint\s*=\s*(\w+)")


def _declared_id(line: str) -> str | None:
    m = _DECLARED_ID.search(line)
    if not m:
        return None
    name = next((g for g in m.groups() if g), None)
    # Two characters is not an anchor; and a bare `type: "page"` repeats across
    # a file, so only names of four or more characters are taken.
    return name if name and len(name) >= 4 else None


def _current_home(symbol: str, prefer_dir: str) -> list[str]:
    """Every file that DEFINES *symbol* today, nearest neighbour first.

    Definitions only -- a call site is not a home, and offering one would send
    the reader to a place that merely mentions the name.
    """
    out = _run("grep", "-l", "-E",
               rf"^\s*(async )?(def|class|function|export (default )?(async )?function|"
               rf"export (const|interface|type|class)) {re.escape(symbol)}\b",
               "--", *SEARCH_ROOTS)
    hits = [h for h in out.splitlines() if h]
    hits.sort(key=lambda h: (not h.startswith(prefer_dir), len(h)))
    return hits


_BY_NAME: dict[str, list[Path]] = {}


def _index_by_name() -> dict[str, list[Path]]:
    """Every readable file, indexed by basename. Cached.

    A citation is written relative to the repo root OR as a BARE basename --
    `ui/admin/src/shell/navigation.ts#WORKSPACES`, which is how most of this repository's prose cites.
    Missing that form is why an earlier run of this tool reported zero resolvable
    while the gate counted sixty-eight: it only ever saw the fully-qualified ones,
    and every bare citation fell through `target.exists()` in silence.
    """
    if _BY_NAME:
        return _BY_NAME
    for path in _iter_sources():
        _BY_NAME.setdefault(path.name, []).append(path)
    return _BY_NAME


def _resolve_target(cited: str) -> Path | None:
    """The file a citation names, or None when it is ambiguous.

    Same rule as `check_line_citations._resolve`, deliberately: the two tools
    must agree on WHICH file a citation points at, or this one would repair a
    citation the gate is still counting -- or worse, a different file.
    """
    cited = cited.replace("\\", "/")
    direct = ROOT / cited
    if direct.is_file():
        return direct
    index = _index_by_name()
    if "/" not in cited:
        matches = index.get(cited, [])
        return matches[0] if len(matches) == 1 else None
    basename = cited.rsplit("/", 1)[-1]
    matches = [
        p for p in index.get(basename, [])
        if p.relative_to(ROOT).as_posix().endswith(cited)
    ]
    return matches[0] if len(matches) == 1 else None


def _iter_sources():
    for root in SEARCH_ROOTS:
        base = ROOT / root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in READABLE:
                continue
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            yield path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="only citations INTO this file")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the citations that resolved to a symbol")
    args = ap.parse_args()

    line_counts: dict[str, int] = {}
    resolutions: dict[tuple[str, int], str] = {}
    unresolved: list[str] = []
    historical: list[str] = []
    edits: dict[Path, list[tuple[str, str]]] = {}

    for path in _iter_sources():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        source_rel = path.relative_to(ROOT).as_posix()
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in CITATION.finditer(line):
                target = _resolve_target(m.group("path"))
                if target is None:
                    continue
                target_rel = target.relative_to(ROOT).as_posix()
                if args.target and target_rel != args.target:
                    continue
                cited = int(m.group("line"))
                if target_rel not in line_counts:
                    line_counts[target_rel] = len(
                        target.read_text(encoding="utf-8", errors="replace").splitlines()
                    )
                if cited <= line_counts[target_rel]:
                    continue  # not broken; this tool only speaks about past-EOF

                when = _written_at(source_rel, lineno)
                key = (target_rel, cited, when)
                if key not in resolutions:
                    resolutions[key] = _resolve(target_rel, cited, when)
                verdict = resolutions[key]
                if verdict.startswith("UNRESOLVED"):
                    unresolved.append(f"  {source_rel}:{lineno} cites {target_rel}:{cited} -- {verdict}")
                    continue
                old = m.group(0)
                new = verdict
                if source_rel.startswith(HISTORICAL_PREFIXES):
                    historical.append(f"  {source_rel}:{lineno}  {old}  ({new})")
                    continue
                print(f"  {source_rel}:{lineno}\n      {old}  ->  {new}")
                edits.setdefault(path, []).append((old, new))

    if args.apply:
        for path, pairs in edits.items():
            text = path.read_text(encoding="utf-8", errors="replace")
            for old, new in pairs:
                text = text.replace(old, new)
            path.write_text(text, encoding="utf-8")
        print(f"\nrewritten: {sum(len(v) for v in edits.values())} citations in {len(edits)} files")
    else:
        print(f"\nresolvable: {sum(len(v) for v in edits.values())} citations in {len(edits)} files")

    if historical:
        print(f"\ndated records ({len(historical)}) -- resolvable, never rewritten:")
        print("\n".join(sorted(set(historical))[:12]))
        if len(set(historical)) > 12:
            print(f"  ... and {len(set(historical)) - 12} more")

    if unresolved:
        print(f"\nUNRESOLVED ({len(unresolved)}) -- left alone, on purpose:")
        print("\n".join(sorted(set(unresolved))))
    return 0


def _resolve(target_rel: str, cited: int, when: str | None = None) -> str:
    """`path#symbol` for a past-EOF citation, or an UNRESOLVED reason.

    *when* is the date the CITING line was written. The search starts at the
    target's revision on that date and walks BACK, so the version opened is the
    one the author could have been reading -- never a later, shorter one where
    the same line number names something else entirely.
    """
    revisions = _revisions(target_rel)
    if when:
        anchor = _revision_at(target_rel, when)
        if anchor:
            short = anchor[:len(revisions[0])] if revisions else anchor
            if short in revisions:
                revisions = revisions[revisions.index(short):]
    for rev in revisions:
        lines = _blob(rev, target_rel)
        if lines is None or cited > len(lines):
            continue
        key = _route_key(lines[cited - 1])
        if key:
            # Anchored only if the key STILL EXISTS in the file today. A route
            # that was removed must stay visibly broken: rewriting it to a key
            # nobody serves would read as repaired.
            current = (ROOT / target_rel).read_text(encoding="utf-8", errors="replace")
            if f'"{key}"' in current or f"'{key}'" in current:
                return f"{target_rel}#{key}"
            return f"UNRESOLVED: route `{key}` (from {rev}) is gone from {target_rel}"

        # FOURTH strategy: a route-table entry. `Route("/api/x", endpoint=_h)`
        # sits inside no function, but it NAMES its handler -- and the handler is
        # what a reader following the citation wants. This is most of what is
        # left after the other three, because the citations into `admin_api.py`
        # that survive its decomposition point at its route table.
        endpoint = _ROUTE_ENDPOINT.search(lines[cited - 1])
        if endpoint:
            name = endpoint.group(1)
            homes = _current_home(name, prefer_dir=str(Path(target_rel).parent).replace("\\", "/"))
            if homes:
                return f"{homes[0]}#{name}"
            return f"UNRESOLVED: endpoint `{name}` (from {rev}) is defined nowhere today"

        declared = _declared_id(lines[cited - 1])
        if declared:
            current = (ROOT / target_rel).read_text(encoding="utf-8", errors="replace")
            if f'"{declared}"' in current or f"'{declared}'" in current:
                return f"{target_rel}#{declared}"
            return f"UNRESOLVED: `{declared}` (from {rev}) is gone from {target_rel}"

        symbol = _enclosing_symbol(lines, cited - 1)
        if not symbol:
            return f"UNRESOLVED: no enclosing symbol at {target_rel}:{cited} in {rev}"
        homes = _current_home(symbol, prefer_dir=str(Path(target_rel).parent).replace("\\", "/"))
        if not homes:
            return f"UNRESOLVED: `{symbol}` (from {rev}) is defined nowhere today"
        return f"{homes[0]}#{symbol}"
    return f"UNRESOLVED: no revision of {target_rel} ever had {cited} lines"


if __name__ == "__main__":
    sys.exit(main())
