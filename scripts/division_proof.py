#!/usr/bin/env python3
r"""Prove that a responsibility MOVED -- criteria 5 and 11 of `module-boundaries.md`.

WHY THIS EXISTS. `scripts/division_candidates.py` says WHICH file to divide. It
says nothing about whether a division that landed was a division at all, and the
two criteria that ask exactly that had no command:

    5.  A responsibility is moved and its call sites are updated by hand rather
        than through a seam, so the diff cannot be read as "this code moved".
    11. A division is claimed without a before/after behaviour dump compared
        entry for entry, or with an edit to a moved body in the same commit.

Both were paragraphs. `module-boundaries.md` itself says what that costs -- *"a
rule written in prose beside a line expires; the same rule written as a test
follows the code"* -- and six extractions were proven by hand, once each, with a
one-off `python -c` nobody can re-run.

WHAT IT MEASURES, and it is two different questions.

  * `--moved` -- THE MOVE CENSUS. For a pair of revisions: every top-level `def`
    or `class` that LEFT one module and ARRIVED in another. Per move it prints
    whether the body is byte-identical (the diff reads "this code moved"), how
    the readers reach it -- the old module still resolving the name is a SEAM,
    a re-export or a forwarder; nothing resolving it means the call sites were
    updated BY HAND -- and every `patch("core.<old>.<name>")` that now dangles.
    A dangling patch site is the failure mode this repository has paid for
    59 + 18 + 9 times: a green test that intercepts nothing.

  * `--dump` / `--compare` -- THE BEHAVIOUR ARTEFACT. Criterion 11 asks for a
    dump taken before and after and compared ENTRY FOR ENTRY. AD-40 did it by
    hand against a detached worktree; nothing wrote the artefact down. `--dump`
    writes it, `--compare` reads two of them and names every divergence.

    python scripts/division_proof.py --dump /tmp/before.json   # BEFORE you cut
    python scripts/division_proof.py --dump /tmp/after.json    # AFTER
    python scripts/division_proof.py --compare /tmp/before.json /tmp/after.json

WHAT THE DUMP COVERS, DECLARED. The composed router entry for entry -- path,
methods, endpoint NAME and owning MODULE, which is what catches a splice
re-inserted at the wrong position -- and every MCP tool binding, read through
`division_candidates.registrations()` rather than through a second copy of the
rule. It does NOT cover "the callers' observable results" of a function
division: that dump is the divided function's own payloads, it differs per cut,
and pretending to produce it generically would be the quiet-guard defect
(criterion 13) written into a new instrument.

THE SCOPE OF THE CENSUS, DECLARED, because a guard whose scope stops before a
directory does not report that directory clean -- it goes quiet, and silence
reads as health. The census reads PYTHON only: a `.ts`/`.tsx` responsibility
moving (`ContentRouter.tsx`, `navigation.ts`) is a real division this AST cannot
follow, so the report COUNTS those files and says so, out loud, instead of
leaving them out of a total that would look complete.

    python scripts/division_proof.py --moved                 # HEAD~1..HEAD
    python scripts/division_proof.py --moved --since 6a40c1c1 --until 6a40c1c1
    python scripts/division_proof.py --dump PATH
    python scripts/division_proof.py --compare BEFORE AFTER
    python scripts/division_proof.py --gate                  # both, exit 1 on either
    python scripts/division_proof.py --json
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
SERVER = REPO / "server"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

#: The patch-site rule is written ONCE, in the conformance guard that refuses it
#: at rest. This instrument reports the same thing for one range, so it imports
#: that guard rather than deriving a second copy -- the same discipline
#: `test_register_bodies_name_their_tools.py` applies to `registrations()`. Two
#: readings of "can this module resolve this name" is two answers.
_GUARD = SERVER / "tests" / "conformance" / "test_patch_targets_follow_the_caller.py"

#: The router and the MCP bindings, measured 2026-09-02 at HEAD. A dump taken
#: from a tree the walk could not read is empty, and an empty dump compares equal
#: to another empty dump -- which would make this instrument certify every
#: division ever attempted. Floors, refused BELOW: a commit that legitimately
#: removes routes lowers the number here, in that same commit.
ROUTES_AT_2026_09_02 = 625
TOOLS_AT_2026_09_02 = 129

#: Files a division never opens on purpose.
_IGNORED = ("__pycache__/", "/node_modules/", "/target/")


def _load_guard():
    spec = importlib.util.spec_from_file_location("_patch_target_guard", _GUARD)
    if spec is None or spec.loader is None:  # pragma: no cover - a moved guard
        raise SystemExit(f"the patch-target guard is not at {_GUARD}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return done.stdout


def _git_ok(*args: str) -> bool:
    done = subprocess.run(["git", *args], cwd=REPO, capture_output=True)
    return done.returncode == 0


# --------------------------------------------------------------------------- #
# The move census -- criterion 5, and the second half of criterion 11
# --------------------------------------------------------------------------- #


@dataclass
class Move:
    """One top-level definition that left a module and arrived in another."""

    name: str
    kind: str
    frm: str
    to: str
    body_identical: bool
    lines_before: int
    lines_after: int
    seam: bool
    dangling_patches: list[str]

    @property
    def verdict(self) -> str:
        """What the criterion asks, and nothing else.

        The verdict is NOT "was a seam used": AD-43 moved 38 handlers out of
        `admin_api` with no re-export at all, because their only reader is the
        router splice, and that is a correct division. The criterion's own
        `so that` clause says what the failure is -- *the diff cannot be read as
        "this code moved"* -- and two things make that true: a body edited on
        the way, and a call site left naming an address that no longer answers.
        """
        if not self.body_identical:
            return "EDITED"
        if self.dangling_patches:
            return "DANGLING PATCH"
        return "moved"


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def top_level_defs(text: str) -> dict[str, tuple[str, str]]:
    """`name -> (kind, source)` for every top-level def/class, decorators included.

    Decorators travel with the body on purpose: a handler that arrives with a
    different decorator did not merely move, and that is the whole question.
    """
    lines = _normalise(text).split("\n")
    try:
        tree = ast.parse(_normalise(text))
    except SyntaxError:
        return {}
    found: dict[str, tuple[str, str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        end = node.end_lineno or node.lineno
        body = "\n".join(line.rstrip() for line in lines[start - 1 : end]).strip("\n")
        kind = "class" if isinstance(node, ast.ClassDef) else "def"
        found[node.name] = (kind, body)
    return found


def _resolvable(text: str, guard) -> set[str]:
    """Every name a module TEXT defines or imports -- the guard's own reading."""
    scratch = pathlib.Path(tempfile.mkdtemp()) / "module.py"
    scratch.write_text(_normalise(text), encoding="utf-8")
    try:
        return guard._module_names(scratch)
    finally:
        scratch.unlink(missing_ok=True)


def changed_files(since: str, until: str) -> list[str]:
    listing = _git("diff", "--name-only", f"{since}..{until}")
    return [
        path
        for path in listing.replace("\r\n", "\n").split("\n")
        if path and not any(bad in f"/{path}" for bad in _IGNORED)
    ]


def tree_at(ref: str, paths: list[str]) -> dict[str, str]:
    """`path -> source` at *ref*, silently omitting what does not exist there."""
    out: dict[str, str] = {}
    for path in paths:
        if not path.endswith(".py"):
            continue
        if not _git_ok("cat-file", "-e", f"{ref}:{path}"):
            continue
        out[path] = _git("show", f"{ref}:{path}")
    return out


def moves(before: dict[str, str], after: dict[str, str]) -> tuple[list[Move], list[str]]:
    """Every definition that left one module and arrived in another, and the ties.

    Matching is by NAME, and a name that left two modules or arrived in two is
    reported as ambiguous rather than guessed. `_list_notebooks` exists twice in
    `core` today; a census that picked one silently would repeat the defect
    AD-43's fourth step named -- *"a name-only lookup finds the right object by
    accident often enough to be dangerous"*.
    """
    defs_before = {path: top_level_defs(text) for path, text in before.items()}
    defs_after = {path: top_level_defs(text) for path, text in after.items()}

    left: dict[str, list[str]] = {}
    arrived: dict[str, list[str]] = {}
    for path, names in defs_before.items():
        for name in names:
            if name not in defs_after.get(path, {}):
                left.setdefault(name, []).append(path)
    for path, names in defs_after.items():
        for name in names:
            if name not in defs_before.get(path, {}):
                arrived.setdefault(name, []).append(path)

    found: list[Move] = []
    ambiguous: list[str] = []
    for name in sorted(set(left) & set(arrived)):
        sources, targets = sorted(left[name]), sorted(arrived[name])
        if len(sources) != 1 or len(targets) != 1:
            ambiguous.append(
                f"{name}: left {sources} and arrived in {targets} -- a name-only "
                "match cannot say which is which. Read it."
            )
            continue
        frm, to = sources[0], targets[0]
        kind, body_before = defs_before[frm][name]
        _, body_after = defs_after[to][name]
        found.append(
            Move(
                name=name,
                kind=kind,
                frm=frm,
                to=to,
                body_identical=body_before == body_after,
                lines_before=len(body_before.split("\n")),
                lines_after=len(body_after.split("\n")),
                seam=False,
                dangling_patches=[],
            )
        )
    return found, ambiguous


def _module_ref(path: str) -> str | None:
    """`server/core/x.py` -> `core.x`, the shape a `patch()` target is written in."""
    if not path.startswith("server/") or not path.endswith(".py"):
        return None
    return path[len("server/") : -len(".py")].replace("/", ".")


def add_seam_and_patches(found: list[Move], after: dict[str, str], ref: str, guard) -> None:
    """Fill in, per move: does the OLD module still resolve it, and does a patch dangle."""
    resolvable: dict[str, set[str]] = {}
    for move in found:
        source = after.get(move.frm)
        if source is None and _git_ok("cat-file", "-e", f"{ref}:{move.frm}"):
            source = _git("show", f"{ref}:{move.frm}")
        if source is not None:
            if move.frm not in resolvable:
                resolvable[move.frm] = _resolvable(source, guard)
            move.seam = move.name in resolvable[move.frm]

        module = _module_ref(move.frm)
        if module is None or move.seam:
            continue
        # Only the sites that name the OLD module: the definition moved, so a
        # patch that followed the DEFINITION is the one that stopped biting.
        hits = _git("grep", "-n", f"{module}.{move.name}", ref, "--", "server/tests")
        for line in _normalise(hits).split("\n"):
            if not line.strip():
                continue
            body = line.split(":", 1)[1] if line.startswith(f"{ref}:") else line
            if "patch" not in body and "setattr" not in body:
                continue
            move.dangling_patches.append(body.strip())


# --------------------------------------------------------------------------- #
# The behaviour dump -- criterion 11
# --------------------------------------------------------------------------- #


def behaviour_dump() -> dict:
    """The observable surface a division must leave unchanged, entry for entry."""
    if str(SERVER) not in sys.path:
        sys.path.insert(0, str(SERVER))
    import division_candidates  # noqa: PLC0415
    from core.admin_api import router  # noqa: PLC0415

    routes = []
    for route in router.routes:
        endpoint = getattr(route, "endpoint", None)
        routes.append(
            {
                "path": getattr(route, "path", ""),
                "methods": sorted(getattr(route, "methods", None) or []),
                "handler": getattr(endpoint, "__name__", "?"),
                "module": getattr(endpoint, "__module__", "?"),
            }
        )

    tools = []
    for row in division_candidates.registrations():
        for tool in row["tools"]:
            tools.append({"tool": tool, "binder": f"{row['file']}::{row['binder']}"})
    tools.sort(key=lambda entry: (entry["tool"], entry["binder"]))
    return {"routes": routes, "tools": tools}


def dump_failures(dump: dict) -> list[str]:
    """Why this dump proves nothing. An empty artefact certifies every division."""
    failures = []
    if len(dump.get("routes", [])) < ROUTES_AT_2026_09_02:
        failures.append(
            f"the dump holds {len(dump.get('routes', []))} routes, "
            f"{ROUTES_AT_2026_09_02} were mounted on 2026-09-02. Either the router "
            "did not compose, or routes were removed -- in which case lower "
            "ROUTES_AT_2026_09_02 in scripts/division_proof.py in the SAME commit."
        )
    if len(dump.get("tools", [])) < TOOLS_AT_2026_09_02:
        failures.append(
            f"the dump holds {len(dump.get('tools', []))} MCP tools, "
            f"{TOOLS_AT_2026_09_02} were bound on 2026-09-02. Same rule: a real "
            "retirement lowers TOOLS_AT_2026_09_02 with the measurement."
        )
    routes = dump.get("routes", [])
    keys = [_key(entry) for entry in routes]
    if len(set(keys)) != len(keys):
        failures.append(
            f"{len(keys) - len(set(keys))} route(s) share a path AND a method set, so "
            "the entry-for-entry comparison would merge them and read one as proof of "
            "the other. Widen `_key` before trusting a comparison of this artefact."
        )
    return failures


def _key(entry: dict) -> str:
    if "path" in entry:
        return f"{entry['path']} {','.join(entry['methods'])}"
    return entry["tool"]


def compare(before: dict, after: dict) -> list[str]:
    """Entry for entry, both surfaces. Every divergence, named.

    ORDER IS COMPARED, and it is not a refinement. Starlette resolves routes in
    the order they are declared and `admin_api.py` states six such orders in
    comments; a splice re-inserted one position off changes which handler answers
    an address while the SET of addresses stays identical. A comparison that only
    diffed the two sets would print "zero divergence" over exactly the regression
    the before/after dump exists to catch.
    """
    divergences: list[str] = []
    for surface in ("routes", "tools"):
        left = {_key(e): e for e in before.get(surface, [])}
        right = {_key(e): e for e in after.get(surface, [])}
        for key in sorted(set(left) - set(right)):
            divergences.append(f"{surface}: GONE      {key} -- {left[key]}")
        for key in sorted(set(right) - set(left)):
            divergences.append(f"{surface}: ADDED     {key} -- {right[key]}")
        for key in sorted(set(left) & set(right)):
            if left[key] != right[key]:
                divergences.append(
                    f"{surface}: CHANGED   {key}\n"
                    f"      before: {left[key]}\n"
                    f"      after : {right[key]}"
                )
        if surface != "routes":
            continue
        keys_before = [_key(e) for e in before.get(surface, [])]
        keys_after = [_key(e) for e in after.get(surface, [])]
        if set(keys_before) != set(keys_after):
            continue  # the set moved; the positions are not comparable yet
        for index, (was, now) in enumerate(zip(keys_before, keys_after)):
            if was != now:
                divergences.append(
                    f"{surface}: ORDER     position {index} was {was!r}, is now {now!r} "
                    "-- Starlette resolves in declaration order"
                )
    return divergences


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def census(since: str, until: str) -> dict:
    guard = _load_guard()
    paths = changed_files(since, until)
    unreadable = [p for p in paths if p.endswith((".ts", ".tsx"))]
    before = tree_at(since, paths)
    after = tree_at(until, paths)
    found, ambiguous = moves(before, after)
    add_seam_and_patches(found, after, until, guard)
    return {
        "since": since,
        "until": until,
        "python_files_in_range": len([p for p in paths if p.endswith(".py")]),
        "unreadable_by_this_census": unreadable,
        "moves": [asdict(m) | {"verdict": m.verdict} for m in found],
        "ambiguous": ambiguous,
    }


def _print_census(result: dict) -> None:
    found = result["moves"]
    print(f"Responsibilities that changed module, {result['since']}..{result['until']}")
    print(f"  python files in the range : {result['python_files_in_range']}")
    print(f"  moves found               : {len(found)}\n")
    if not found:
        print("  none -- nothing in this range left one module for another.")
    seams = sum(1 for move in found if move["seam"])
    if found:
        print(
            f"  through a seam            : {seams} "
            "(the old module still resolves the name: a facade re-export or a\n"
            "                              forwarder, so no reader had to be told). "
            f"{len(found) - seams} had no reader\n"
            "                              left to keep -- which is correct when the "
            "only reader is a splice.\n"
        )
    for move in found:
        seam = "old module still resolves it" if move["seam"] else "no seam left behind"
        print(
            f"  {move['verdict']:<14} {move['kind']} {move['name']}\n"
            f"                 {move['frm']}  ->  {move['to']}\n"
            f"                 body {move['lines_before']} l. -> {move['lines_after']} l., "
            f"{'identical' if move['body_identical'] else 'EDITED IN THE SAME COMMIT'}; {seam}"
        )
        for site in move["dangling_patches"]:
            print(f"             !! {site}")
    if result["ambiguous"]:
        print("\n  ambiguous, read them one at a time:")
        for line in result["ambiguous"]:
            print(f"    {line}")
    if result["unreadable_by_this_census"]:
        print(
            f"\n  NOT READ -- this census is Python-only, and "
            f"{len(result['unreadable_by_this_census'])} TypeScript file(s) changed in "
            "this range. A .ts/.tsx division is real and invisible here; it is said\n"
            "  rather than left out of a total that would look complete:"
        )
        for path in result["unreadable_by_this_census"][:20]:
            print(f"    {path}")


def _gate(since: str, until: str) -> int:
    result = census(since, until)
    failures: list[str] = []

    edited = [m for m in result["moves"] if not m["body_identical"]]
    if edited:
        failures.append(
            f"{len(edited)} moved bod(y|ies) were EDITED in the same commit, so the "
            "diff cannot be read as 'this code moved' (criteria 5 and 11). Move "
            "first, edit second, in two commits:\n  "
            + "\n  ".join(f"{m['name']}  {m['frm']} -> {m['to']}" for m in edited)
        )

    dangling = [m for m in result["moves"] if m["dangling_patches"]]
    if dangling:
        failures.append(
            f"{len(dangling)} moved name(s) left a `patch()` site naming a module "
            "that can no longer resolve them -- a green test that intercepts "
            "nothing (criterion 5):\n  "
            + "\n  ".join(
                f"{m['name']}: {site}" for m in dangling for site in m["dangling_patches"]
            )
        )

    try:
        dump = behaviour_dump()
    except Exception as error:  # noqa: BLE001 - the reason is the whole message
        failures.append(
            f"the behaviour dump could not be produced, so criterion 11 has no "
            f"artefact to compare: {type(error).__name__}: {error}"
        )
        dump = {}
    failures.extend(dump_failures(dump))

    print(f"moves in {since}..{until} : {len(result['moves'])}")
    print(f"  bodies edited in the same commit : {len(edited)}")
    print(f"  moved names with a dangling patch: {len(dangling)}")
    print(f"  behaviour dump                   : {len(dump.get('routes', []))} routes, "
          f"{len(dump.get('tools', []))} tools")
    if failures:
        print(f"\nREFUSED on {len(failures)} count(s):")
        for failure in failures:
            print(f"\n- {failure}")
        return 1
    print("\nOK: every move in this range reads as 'this code moved', and the dump bites.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--since", default="HEAD~1", help="the revision BEFORE the division")
    parser.add_argument("--until", default="HEAD", help="the revision AFTER it")
    parser.add_argument("--moved", action="store_true", help="the move census")
    parser.add_argument("--dump", metavar="PATH", help="write the behaviour artefact")
    parser.add_argument(
        "--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="two artefacts, entry for entry"
    )
    parser.add_argument("--gate", action="store_true", help="refuse an edited move or a dead dump")
    parser.add_argument("--json", action="store_true", help="machine-readable census")
    args = parser.parse_args(argv)

    if args.compare:
        before = json.loads(pathlib.Path(args.compare[0]).read_text(encoding="utf-8"))
        after = json.loads(pathlib.Path(args.compare[1]).read_text(encoding="utf-8"))
        stale = dump_failures(before) + dump_failures(after)
        if stale:
            print("the artefacts do not carry a surface to compare:")
            for line in stale:
                print(f"  - {line}")
            return 1
        divergences = compare(before, after)
        print(
            f"routes {len(before['routes'])} -> {len(after['routes'])}, "
            f"tools {len(before['tools'])} -> {len(after['tools'])}"
        )
        if not divergences:
            print(
                "zero divergence, entry for entry: same paths, same methods, same "
                "handlers,\nsame owning modules, same declaration ORDER; and the same "
                "MCP tools bound by the\nsame binders."
            )
            return 0
        print(f"\n{len(divergences)} divergence(s):")
        for line in divergences:
            print(f"  {line}")
        return 1

    if args.dump:
        dump = behaviour_dump()
        failures = dump_failures(dump)
        if failures:
            print("REFUSED -- an empty artefact certifies every division:")
            for line in failures:
                print(f"  - {line}")
            return 1
        target = pathlib.Path(args.dump)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(dump, indent=1, sort_keys=True), encoding="utf-8")
        print(
            f"{target}: {len(dump['routes'])} routes, {len(dump['tools'])} MCP tools.\n"
            "Take the second one after the cut, then --compare them."
        )
        return 0

    if args.gate:
        return _gate(args.since, args.until)

    result = census(args.since, args.until)
    if args.json:
        print(json.dumps(result, indent=1))
        return 0
    _print_census(result)
    print("\nThe two criteria: docs/product-architecture/module-boundaries.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
