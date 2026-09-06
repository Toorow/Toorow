#!/usr/bin/env python
"""Chantier B -- the command behind every number the contract states.

Prints, for one Project and one measure: how many Datastreams CARRY it (derived
from the active mappings), how many a Semantic View version BINDS, whether a
total authority is DECLARED, and what each breakdown was declared to sum to.

The two numbers the ratified contract quotes come from here, so a reader can
re-run it instead of trusting a sentence:

    python scripts/metric_grain_report.py --project proj_EXAMPLE --concept views

Add `--view <semantic_view_version_id>` to get the bound count as well. Without
it the reading is about the Project alone.

Reads only. It opens no cursor that writes and takes no `--fix`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))


def _dsn(explicit: str | None) -> str:
    if explicit:
        return explicit
    url = os.environ.get("PLATFORM_DB_URL")
    if url:
        return url
    env = Path(__file__).resolve().parents[1] / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("PLATFORM_DB_URL="):
                return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("no DSN: pass --dsn or set PLATFORM_DB_URL")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--concept", required=True, help="the measure's canonical name")
    parser.add_argument("--view", default=None, help="a semantic view version id")
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args()

    import psycopg

    from core.metric_grain import carriers_of, get_declaration

    with psycopg.connect(_dsn(args.dsn)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT cv.concept_id
                FROM app.semantic_concept_versions cv
                JOIN app.semantic_concepts c ON c.id = cv.concept_id
                WHERE cv.name = %s AND c.project_id = %s
                """,
                (args.concept, args.project),
            )
            concept_rows = cur.fetchall()

        carriers = carriers_of(
            conn,
            project_id=args.project,
            concept_name=args.concept,
            view_version_id=args.view,
        )
        print(f"project  {args.project}")
        print(f"measure  {args.concept}")
        print(f"carriers {len(carriers)}  (Datastreams whose ACTIVE mapping targets it)")
        if args.view:
            bound = sum(1 for c in carriers if c["bound_in_view"])
            print(f"bound    {bound} of {len(carriers)}  in view version {args.view}")

        declaration = None
        if len(concept_rows) == 1:
            declaration = get_declaration(
                conn, project_id=args.project, concept_id=str(concept_rows[0][0])
            )
        elif len(concept_rows) > 1:
            print(
                f"note     {len(concept_rows)} Concepts of this Project are named "
                f"`{args.concept}`; no single declaration can be read."
            )

        total_id = str(declaration["total_datastream_id"]) if declaration else None
        print(f"total    {total_id or 'NOT DECLARED'}")
        rules = {
            str(entry["datastream_id"]): entry
            for entry in (declaration or {}).get("breakdowns", ())
        }

        print()
        for carrier in carriers:
            role = "TOTAL " if carrier["datastream_id"] == total_id else "break "
            mark = "" if args.view is None else ("bound  " if carrier["bound_in_view"] else "UNBOUND")
            rule = rules.get(carrier["datastream_id"])
            says = (
                "-"
                if carrier["datastream_id"] == total_id
                else (rule["sums_to"] if rule else "undeclared")
            )
            print(
                f"  {role} {mark:8s} {says:18s} {carrier['datastream_name']}\n"
                f"          grain={carrier['grain']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
