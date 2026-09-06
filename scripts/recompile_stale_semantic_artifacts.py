"""AI-346 -- re-derive the compiled artifacts of published Semantic Views under the compiler.

    python scripts/recompile_stale_semantic_artifacts.py                     # every Project
    python scripts/recompile_stale_semantic_artifacts.py --project proj_X    # one Project
    python scripts/recompile_stale_semantic_artifacts.py --dry-run           # list, write nothing

Requires PLATFORM_DB_URL (or --dsn).

Exit code: 0 when no executable published version stays stale; 1 when at least
one does (listed by --dry-run, or refused by the sweep -- each with its codes and
the gesture); 2 on a usage error.

WHY THIS SCRIPT EXISTS. `COMPILER_VERSION` moved on 2026-08-22 and nothing
re-derived anything: the reference Project's published View kept a
`semantic-compiler.v1` artifact and every NEW question on it was refused
`stale_compiled_artifact` -- "publish it again" -- while the August Query Specs
still ran. The product now runs this same sweep at process start and every
night (`core.semantic_artifact_sweep`); this is the hand-run door for the day a
compiler bump is deployed, and the way to READ what stays stale and why.

WHAT IT WRITES, AND WHAT IT NEVER TOUCHES. One NEW row in
`app.semantic_compiled_artifacts` per stale version, under the current compiler;
one attempt record per version in `app.semantic_recompile_attempts`; one audit
row per recompiled version. No version row, no version number, no Test gate, no
UPDATE of an existing artifact. `--dry-run` reads only. Idempotent: run twice,
the second run lists nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "server"))


def _print_report(report: dict, *, dry_run: bool) -> None:
    compiler = report.get("compiler_version")
    stale = report.get("stale") or []
    if not stale:
        print(f"nothing stale: every executable published version is compiled by {compiler}")
        return
    print(f"{len(stale)} stale version(s) against {compiler}:")
    for entry in stale:
        print(
            f"  {entry['project_id']}  {entry['view_id']}  v{entry['version_number']}  "
            f"{entry['version_id']}  compiled by "
            f"{entry.get('compiled_by') or 'an unrecorded compiler'}"
        )
    if dry_run:
        print("dry run: nothing written")
        return
    for entry in report.get("recompiled") or []:
        summary = entry.get("summary") or {}
        print(
            f"recompiled  {entry['version_id']}  -> {entry['artifact_id']}  "
            f"(from {entry['composition_source']}; "
            f"{summary.get('accepted', '?')}/{summary.get('pairs', '?')} pairs accepted)"
        )
    for entry in report.get("refused") or []:
        print(
            f"REFUSED     {entry['version_id']}  ({entry['composition_source']})  "
            f"codes={','.join(entry.get('codes') or [])}"
        )
        for refusal in entry.get("refusals") or []:
            print(f"              - {refusal.get('code')}: {refusal.get('message')}")
        print(f"              gesture: {entry.get('gesture')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=os.environ.get("PLATFORM_DB_URL", ""))
    parser.add_argument("--project", default=None, help="one Project id; default every Project")
    parser.add_argument("--dry-run", action="store_true", help="list stale versions, write nothing")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    if not args.dsn:
        print(
            "recompile_stale_semantic_artifacts requires --dsn or PLATFORM_DB_URL",
            file=sys.stderr,
        )
        return 2

    try:
        import psycopg  # noqa: PLC0415
    except ImportError:
        print("psycopg is not installed in this interpreter", file=sys.stderr)
        return 2

    from core.semantic_model import RECOMPILE_ACTOR, recompile_stale_artifacts  # noqa: PLC0415

    with psycopg.connect(args.dsn) as conn:
        report = recompile_stale_artifacts(
            conn,
            project_id=args.project,
            actor=f"{RECOMPILE_ACTOR}:script",
            dry_run=args.dry_run,
        )
        if args.dry_run:
            conn.rollback()
        else:
            conn.commit()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_report(report, dry_run=args.dry_run)

    if args.dry_run:
        return 1 if report.get("stale") else 0
    return 1 if report.get("refused") else 0


if __name__ == "__main__":
    sys.exit(main())
