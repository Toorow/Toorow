# -*- coding: utf-8 -*-
"""Every `/api/...` address the console BUILDS, against every route the server SERVES.

WHY THIS EXISTS. On 2026-08-02 the Datastream Workbench showed no tabs at all:
`workbenchApi.ts` fetches `${base}/${tab}` -- `/workbench/overview`, `/workbench/data`,
`/workbench/mapping` ... -- while `datastream_workbench_api.py` serves exactly one
route, `/workbench`. Every tab answered 503, on the main screen of the product, for
every Datastream. The same class had been found four hours earlier between the
platform-clocks screen (`/api/admin/platform-clocks`) and its API
(`/api/platform/clocks`): every call would have 404'd.

Neither was caught by a test, and both are trivially detectable: the console names
the address it calls, the server names the address it serves, and nothing ever
compared the two lists. Unit tests on both sides pass precisely because each side
is consistent with ITSELF.

Reported, never repaired here: a mismatch is either a route to add or a caller to
correct, and only the surface's owner knows which.

WHY IT WAS REPAIRED (audit 2026-09-05). It announced **39** addresses BUILT BUT
NOT SERVED and every one of the 39 was false, for two reasons it could not see:

  1. it read the SERVED side as `"/api/..."` strings in `.py` files, so a router
     that composes its paths -- `_BASE = "/api/projects/{project_id}/answerable-topics"`
     then `f"{_BASE}/{{topic_id}}/queries"` -- served routes this script could not
     find. It now boots the composed router, like `element_inventory.py`, and
     REFUSES rather than falling back: a quiet fallback would inherit the drift.
  2. it read the BUILT side without distinguishing code from prose, so an
     `/api/dq/summary` named in a comment EXPLAINING that the route was retired
     was counted as a live caller. Comment and JSDoc lines are skipped, and an
     address that is a segment-prefix of a served route -- `const base =
     `/api/projects/${id}/analyze`` -- is a base, not a dead call.

Both historic defects stay visible under the new rule: `/workbench/:p` is LONGER
than the served `/workbench`, and `/api/admin/platform-clocks` prefixes nothing.

An instrument that reports 39 false alarms is read as noise, and the one true
alarm in it would have been read as noise too.

    python scripts/ui_server_seam_audit.py [--json]
"""
from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui" / "admin" / "src"
#: `server/` -- its PARENT goes on `sys.path` so `core.admin_api` imports.
SERVER = ROOT / "server" / "core"

#: A literal `"/api/..."` or a template `` `/api/...` `` in the console.
UI_PATH = re.compile(r"""["'`](/api/[^"'`\s]*)["'`]""")

#: `${encodeURIComponent(projectId)}` and friends become a single segment.
TEMPLATE_HOLE = re.compile(r"\$\{[^}]*\}")
#: `{project_id}` server-side becomes the same placeholder.
SERVER_HOLE = re.compile(r"\{[^}]*\}")


#: `${a}${b}` and `${a}/${b}` both collapse to ONE unknown, not two.
RUN_OF_HOLES = re.compile(r"(?::p)+")
#: `/* ... */`, and the `{/* ... */}` JSX form, across as many lines as it spans.
BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def normalise(path: str) -> str:
    """Both sides reduced to the same shape: a parameter is one opaque segment."""
    path = TEMPLATE_HOLE.sub(":p", path)
    path = SERVER_HOLE.sub(":p", path)
    path = RUN_OF_HOLES.sub(":p", path)
    path = path.split("?")[0].rstrip("/")
    return path


def segment_matches(built: str, served: str) -> bool:
    """One segment of a built address against one of a served route.

    `:p` on either side is an unknown and matches anything -- the console builds
    `.../${confirmationId}/${action}` where the router declares `/confirm` and
    `/rollback`, and no reader of this file can enumerate a variable. A segment
    that GLUES a template to a literal (`health${query}`) matches a served
    segment carrying that literal prefix.
    """
    if built == served or ":p" in (built, served):
        return True
    return built.endswith(":p") and served.startswith(built[: -len(":p")])


def is_served(address: str, served: set[str]) -> bool:
    """Segment-wise, and only at EQUAL depth.

    Equal depth is what keeps the defect this script exists for visible: the
    console's `.../workbench/${tab}` has one segment more than the served
    `/workbench`, so it is still reported. `/api/admin/platform-clocks` has the
    depth of `/api/platform/clocks` and disagrees on a literal, so it too stays.
    """
    parts = address.split("/")
    return any(
        len(other := route.split("/")) == len(parts)
        and all(segment_matches(b, s) for b, s in zip(parts, other))
        for route in served
    )


def collect(root: Path, pattern: re.Pattern[str], suffixes: tuple[str, ...],
            skip: tuple[str, ...] = ()) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for file in root.rglob("*"):
        if file.suffix not in suffixes or any(s in file.as_posix() for s in skip):
            continue
        try:
            text = file.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        # A `/api/...` inside a comment is prose ABOUT a route, not a call to one
        # -- three retired `/api/dq/*` addresses were counted as callers by the
        # very JSX comment that recorded their retirement, and that block spans
        # five lines. Block comments go first, then the line comments.
        text = BLOCK_COMMENT.sub("", text)
        for line in text.splitlines():
            if line.lstrip().startswith(("//", "*")):
                continue
            for raw in pattern.findall(line):
                key = normalise(raw)
                if key.startswith("/api"):
                    found.setdefault(key, []).append(file.relative_to(ROOT).as_posix())
    return found


def served_addresses() -> set[str]:
    """Every address the COMPOSED router mounts. Booted, never read as text.

    `element_inventory.py` boots the same router for the same reason: a router
    that builds its paths from a base constant declares nothing a regex can find.
    No fallback -- a scan that silently degrades to the old text read would
    announce the old 39 again and call it a measurement.
    """
    if str(SERVER.parent) not in sys.path:
        sys.path.insert(0, str(SERVER.parent))
    from core.admin_api import router  # noqa: PLC0415

    return {
        normalise(path) for route in router.routes
        if (path := getattr(route, "path", ""))
    }


def is_base_of(address: str, served: set[str]) -> bool:
    """`/api/projects/:p/analyze` when `/api/projects/:p/analyze/reports` is served.

    A console constant the call sites extend. The boundary is a SEGMENT: `/api/foo`
    is not a base of `/api/foobar`. An address that prefixes nothing served stays
    a finding -- that is the platform-clocks defect this script was written for.
    """
    return any(route.startswith(address + "/") for route in served)


def main(argv: list[str]) -> int:
    called = collect(UI, UI_PATH, (".ts", ".tsx"), skip=("__tests__", ".test."))
    served = served_addresses()

    # A server route with a trailing segment the console appends dynamically --
    # `${base}/${tab}` -- reads as `/workbench/:p` here, so a served `/workbench`
    # does NOT cover it. That is the Workbench defect, and it must stay visible.
    unserved = {p: files for p, files in sorted(called.items())
                if not is_served(p, served)}
    bases = {p: f for p, f in unserved.items() if is_base_of(p, served)}
    missing = {p: f for p, f in unserved.items() if p not in bases}

    print("addresses the console builds : %d" % len(called))
    print("routes the composed router mounts : %d" % len(served))
    print("of the built addresses, base constants the call sites extend : %d"
          % len(bases))
    print("BUILT BUT NOT SERVED         : %d" % len(missing))
    print()
    for path, files in missing.items():
        print("  %s" % path)
        for f in sorted(set(files))[:2]:
            print("      %s" % f)

    if "--json" in argv:
        Path("seam-audit.json").write_text(
            json.dumps({"missing": missing}, indent=2), encoding="utf-8")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
