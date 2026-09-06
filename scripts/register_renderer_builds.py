"""Project the shipped renderer registry into the build ledger, or say what is wrong.

WHY THIS TOOL EXISTS, measured 2026-08-13. `app.renderer_runtime_builds` is what a
Render's `renderer_build` and `runtime_build` pins point at. Without a row, the pin
names a build the deployment cannot claim it shipped, and the Render is refused.
The ledger held ONE row for EIGHT declared families, and that row had been inserted
by hand -- so seven families, `table` included, could not be pinned at all. Nothing
was wrong with the renderers; nothing carried them across.

THE CODE IS THE CATALOGUE, THIS TABLE IS ITS PROJECTION.
`ui/cards/shell/src/viz/renderers/index.ts` decides which renderers exist; a table
a connector could write would be executable presentation metadata, which AD-2
forbids. The manifest this tool reads is EMITTED from that registry
(`scripts/emit-renderer-builds.mjs`) and committed, so this tool runs on a checkout
with no node toolchain -- the deploy case.

ADDITIVE, AND THAT IS THE POINT. A build id already present is left EXACTLY as it
is: a Render may already pin it, and rewriting the row would change what a frozen
Render claims drew it. The row is insert-once in the database too -- migration 160
puts `app.reject_analytical_evidence_mutation()` on UPDATE and DELETE -- so no
gesture could repair it even if a person wanted one.

WHAT THE VERDICT IS, AND WHY IT IS NOT A COLUMN DIFF (measured 2026-08-24, closed
2026-08-25). The ratified clause this tool serves is four sentences long
(`docs/product-architecture/visualization-and-rendering.md:319-322`), and the two
halves of it that read the database are exactly these:

    a family is declared in code and absent from the ledger      -> MISSING
    a ledger row names a family or renderer the shipped registry
    does not declare                                             -> UNDECLARED

Nothing there asks a stored row to still look like today's manifest, and the same
paragraph forbids making it so: "a build id already present is left exactly as it
is, because a Render already pinned it". This tool used to compare every column of
an existing row and exit 1 on any difference, which made the ratified rule and a
green check MUTUALLY EXCLUSIVE. Migration 191 registered
`waterfall/toorow-echarts-waterfall@1.0.0` at runtime `+2e4aba0febbe` -- a real
identity, shipped at commit 0bc6aded, whose `buildInfo.generated.ts` carried that
exact hash -- and the runtime content hash has moved twice since. The row is TRUE
about the build it names, it cannot be edited, and it failed the check forever. An
instrument whose failure admits no repair is not measuring a defect.

So a still-declared renderer build whose frozen columns differ from today's
manifest is printed as HISTORY: visible, never erased, never fatal. What stays
fatal is what the clause names -- a declared family with no row, and a row naming a
family or renderer this build does not declare. Neither is ever repaired by
deleting: a Render may pin the row, and its absence from the code is what a reader
must see rather than lose.

    python scripts/register_renderer_builds.py            # apply
    python scripts/register_renderer_builds.py --check    # report only, exit 1 on a defect

Requires PLATFORM_DB_URL.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "ui" / "cards" / "shell" / "src" / "viz" / "rendererBuilds.generated.json"

#: The columns migration 160 declares. Named here so a manifest that grows a key
#: this table has no column for is refused instead of silently dropped.
COLUMNS = (
    "id",
    "runtime_build",
    "family",
    "renderer_id",
    "theme_version",
    "formatter_version",
    "responsive_profiles",
    "git_sha",
)

#: The columns the MANIFEST carries. `git_sha` is not one of them, deliberately:
#: the runtime's own identity is a CONTENT HASH of `src/viz/**`, and
#: `buildInfo.ts` explains at length why a git SHA there is wrong (it is generated
#: before the commit that contains the code). The ledger column asks a different
#: question -- "what do I check out to find this build" -- and the answer is known
#: at PROJECTION time, by the checkout being deployed.
MANIFEST_COLUMNS = tuple(column for column in COLUMNS if column != "git_sha")


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        raise SystemExit(
            f"no manifest at {MANIFEST.relative_to(REPO_ROOT)} -- run "
            "`node scripts/emit-renderer-builds.mjs` in ui/cards/shell first"
        )
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    builds = payload.get("builds") or []
    if not builds:
        raise SystemExit("the manifest declares no build: refusing to project an empty registry")
    for build in builds:
        missing = [column for column in MANIFEST_COLUMNS if column not in build]
        if missing:
            raise SystemExit(f"manifest entry {build.get('id')!r} lacks {', '.join(missing)}")
    return builds


def _stored(conn) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, runtime_build, family, renderer_id, theme_version, "
            "formatter_version, responsive_profiles FROM app.renderer_runtime_builds"
        )
        return {row[0]: dict(zip(COLUMNS, row)) for row in cur.fetchall()}


def _differs(stored: dict, declared: dict) -> list[str]:
    """Every column of a stored row that disagrees with the shipped registry.

    Informational, not a verdict. The row is frozen at the moment it was
    registered; this says what has moved since, so a reader can tell WHICH build a
    Render pinned to it was drawn by.
    """
    lines: list[str] = []
    for column in MANIFEST_COLUMNS[1:]:
        left, right = stored[column], declared[column]
        #  `responsive_profiles` comes back as a list from the driver and as a
        #  list from JSON; comparing them as sequences avoids calling a tuple and
        #  a list a difference.
        if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
            if list(left or []) != list(right or []):
                lines.append(f"{column}: ledger {list(left or [])}, registry {list(right or [])}")
        elif left != right:
            lines.append(f"{column}: ledger {left!r}, registry {right!r}")
    return lines


def _undeclared(stored: dict[str, dict], declared_renderers: set[tuple[str, str]]) -> list[str]:
    """Ledger rows naming a family or renderer the shipped registry does not declare.

    Keyed on `(family, renderer_id)` and NOT on the build id, because the clause is
    keyed that way: a build id the registry no longer emits -- an older semver, a
    build registered by an earlier runtime -- still names a renderer this build
    ships, and calling it undeclared would make every retained identity a defect.
    """
    return sorted(
        build_id
        for build_id, row in stored.items()
        if (row["family"], row["renderer_id"]) not in declared_renderers
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=os.environ.get("PLATFORM_DB_URL", ""))
    parser.add_argument(
        "--git-sha",
        default="",
        help="the commit that produced this runtime; defaults to `git rev-parse HEAD`",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is missing or undeclared, write nothing, exit 1 if anything is",
    )
    args = parser.parse_args(argv)
    if not args.dsn:
        print("register_renderer_builds requires --dsn or PLATFORM_DB_URL", file=sys.stderr)
        return 2

    import subprocess  # noqa: PLC0415

    import psycopg  # noqa: PLC0415 -- the tool is also read by machines with no driver

    git_sha = args.git_sha.strip()
    if not git_sha:
        git_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        ).stdout.strip()
    if not git_sha:
        print("no git sha: pass --git-sha", file=sys.stderr)
        return 2

    manifest = load_manifest()
    declared = {build["id"]: {**build, "git_sha": git_sha} for build in manifest}
    declared_renderers = {(build["family"], build["renderer_id"]) for build in manifest}

    with psycopg.connect(args.dsn, connect_timeout=30) as conn:
        stored = _stored(conn)

        history: list[str] = []
        for build_id, entry in sorted(declared.items()):
            if build_id in stored:
                for line in _differs(stored[build_id], entry):
                    history.append(f"{build_id} -- {line}")
        undeclared = _undeclared(stored, declared_renderers)
        missing = [entry for build_id, entry in sorted(declared.items()) if build_id not in stored]

        print(f"shipped registry: {len(declared)} build(s), ledger: {len(stored)}")
        for line in history:
            #  A frozen row that no longer matches today's manifest. Printed
            #  because a reader must be able to see WHICH build a Render pinned to
            #  it was drawn by; not a failure, because the row is insert-once and
            #  the ratified rule is to leave it exactly as it is.
            print(f"  HISTORY     {line}")
        for build_id in undeclared:
            #  A ledger row whose family or renderer the shipped registry does not
            #  declare. NOT deleted: a Render may pin it, and its absence from the
            #  code is exactly what a reader must see rather than have erased.
            print(
                f"  UNDECLARED  {build_id} -- names {stored[build_id]['family']}/"
                f"{stored[build_id]['renderer_id']}, which this build does not declare"
            )
        for entry in missing:
            print(f"  MISSING     {entry['id']}")

        if args.check:
            return 1 if (undeclared or missing) else 0

        if missing:
            with conn.cursor() as cur:
                for entry in missing:
                    cur.execute(
                        """
                        INSERT INTO app.renderer_runtime_builds
                            (id, runtime_build, family, renderer_id, theme_version,
                             formatter_version, responsive_profiles, git_sha)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                        """,
                        tuple(entry[column] for column in COLUMNS),
                    )
            conn.commit()
        print(f"projected: {len(missing)} added, {len(declared) - len(missing)} already registered")
        #  An undeclared row is reported and never repaired: choosing whether the
        #  code or the ledger is right belongs to a person, and a tool that picked
        #  one would erase the evidence for the other.
        return 1 if undeclared else 0


if __name__ == "__main__":
    raise SystemExit(main())
