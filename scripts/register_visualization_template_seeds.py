"""Project the shipped Chart Template catalogue into its table, or say what is wrong.

Story 72.4. `server/core/visualization_template_seeds.py` IS the catalogue;
`app.visualization_templates` / `app.visualization_template_versions` are its
projection. This is the same shape, and deliberately the same shape, as
`scripts/register_renderer_builds.py` -- read that file first if this one
surprises you.

    python scripts/register_visualization_template_seeds.py            # apply
    python scripts/register_visualization_template_seeds.py --check    # report only

Requires PLATFORM_DB_URL (or --dsn).

---------------------------------------------------------------------------
WHY A PROJECTION AND NOT A SEED SCRIPT THAT INSERTS ROWS ONCE
---------------------------------------------------------------------------
`CLAUDE.md`: *"Un catalogue livré avec le produit ne se lit pas dans une
table."* The ratified invariant of epic 72 states the consequence: a table a
connector could write would be executable presentation metadata, and AD-2
forbids it. So the code decides what this deployment ships, and this tool makes
the table agree -- ADDITIVELY, never by rewriting.

ADDITIVE, AND THAT IS THE POINT. A version already present is left EXACTLY as it
is. A Report version or a Notebook block may already pin it -- migration 333 gave
`presentation_version_id` the foreign key it never had -- and rewriting the row
would change what a frozen Report claims was its presentation. The row is
insert-once in the database too: migration 333 puts
`app.reject_analytical_evidence_mutation()` on UPDATE, DELETE and TRUNCATE, so no
gesture could repair it even if a person wanted one.

A CHANGED DECLARATION APPENDS A VERSION; IT NEVER EDITS ONE. When the catalogue's
document for a seed no longer matches any stored version, that is a MISSING
version and the projection APPENDS version n+1 with its predecessor named, then
advances the head. The versions it no longer declares stay, and are printed as
HISTORY -- visible, never erased, never fatal. This is `register_renderer_builds`'s
own finding, applied to the object it was found on: *"an instrument whose failure
admits no repair is not measuring a defect"*.

---------------------------------------------------------------------------
THE VERDICTS, AND WHICH OF THEM BLOCK
---------------------------------------------------------------------------
    a seed is declared in code and no version of it carries the declared
    document, in a Project of this deployment                     -> MISSING
    a platform_seed head names a seed the shipped catalogue does
    not declare                                                   -> UNDECLARED
    a stored version's document is not the one the code declares
    today                                                         -> HISTORY
    a person ARCHIVED this seed in this Project, and the catalogue
    has moved on since                                            -> ARCHIVED_STALE
    a person EDITED this seed in this Project, so the head is now
    project-owned, and the catalogue has moved on since           -> PROJECT_OWNED

MISSING and UNDECLARED exit 1. The other three never do. Neither MISSING nor
UNDECLARED is ever repaired by deleting: a Report may pin the row, and a seed's
absence from the code is what a reader must SEE rather than lose.

    A HEAD A PERSON HAS TAKEN RESPONSIBILITY FOR IS NOT THIS TOOL'S TO WRITE, and
    that is one rule with two faces. Archiving retires the seed IN THIS PROJECT
    (`chart_template_store._set_archived`) and editing it makes the head
    project-owned (AC15) -- in both cases the answer is the same: the head is
    NAMED, nothing is written, and `--check` does not fail, because an operator
    chose this. Measured on a disposable database, 2026-09-01, before this was
    true: the archived head gained version 2 of a catalogue it had been retired
    from, and the edited head made `apply` die on migration 333's own trigger
    (`a stable head advances only to a NEWER version (2 -> 1)`), refusing the
    deployment for a gesture the product offers.

    A DOCUMENT THAT DOES NOT PASS THE GRAMMAR IS A RED, NEVER A SKIP. The
    catalogue derives each document and validates it with 72.2's own validator at
    import time. A declared seed whose document the grammar refuses raises
    `SeedCatalogueDefect`, and this tool exits 2 naming the card. It never ships
    five seeds where six were declared.

    A CARD THE SHIPPED REGISTRY CANNOT DRAW IS LISTED BY ITS NAME (AC14). It is
    printed with every reason it did not land -- one line per family the card
    itself proposed -- and it is NOT a failure: a `funnel` is not a family this
    deployment declares, and no neighbouring family is substituted for it.

---------------------------------------------------------------------------
WHERE A PLATFORM SEED LIVES, AND WHY IT IS PER-PROJECT
---------------------------------------------------------------------------
`app.visualization_templates` is scoped `(org_id, project_id)` with a foreign key
onto `app.projects` (migration 333), so there is no such thing as a Chart
Template row belonging to nobody. A platform seed is therefore projected into
every ACTIVE Project, which is also what makes AC15 coherent: a connector seed
lands in the Project too, and becomes project-owned at its first edit.

The lag is named rather than hidden: a Project created after a deployment holds
no platform seed until this tool runs again. It is idempotent and additive, so
running it is always safe.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "server"))

#: The two tables this tool projects into. Probed by name before anything else,
#: so a checkout whose migrations are not applied is told the gesture that
#: repairs instead of meeting a `relation does not exist`.
REQUIRED_TABLES = (
    "app.visualization_templates",
    "app.visualization_template_versions",
)

MISSING = "MISSING"
UNDECLARED = "UNDECLARED"
HISTORY = "HISTORY"
ARCHIVED_STALE = "ARCHIVED_STALE"
PROJECT_OWNED = "PROJECT_OWNED"

#: The verdicts that refuse a deployment. Read by `--check`, and deliberately a
#: NAMED set rather than "everything except HISTORY": the two states this tool
#: gained for a head a person owns are not defects, and a gate that treated them
#: as defects would be worked around within the week.
BLOCKING = (MISSING, UNDECLARED)


@dataclass(frozen=True)
class Finding:
    """One disagreement between the shipped catalogue and the table."""

    state: str
    project_id: str
    seed_id: str
    detail: str


#: How many Project ids a grouped line names before it counts the rest.
#:
#: A platform seed is projected into EVERY active Project, so a finding is
#: (Project x seed) and the raw list grows with the deployment: measured on a
#: database holding 105 Projects, the ungrouped report was 630 lines, which is
#: not a deploy log anybody reads. The grouping keeps what a reader must act on
#: -- which seed, in how many Projects, why -- and drops the repetition.
_MAX_LISTED_PROJECTS = 5


def _grouped_lines(findings: list["Finding"]) -> list[str]:
    """One line per (verdict, seed), naming a few Projects and counting the rest."""
    groups: dict[tuple[str, str], list[Finding]] = {}
    for finding in findings:
        groups.setdefault((finding.state, finding.seed_id), []).append(finding)

    lines: list[str] = []
    for (state, seed_id), group in groups.items():
        shown = ", ".join(finding.project_id for finding in group[:_MAX_LISTED_PROJECTS])
        rest = len(group) - _MAX_LISTED_PROJECTS
        if rest > 0:
            shown = f"{shown}, +{rest} more"
        wordings = {finding.detail for finding in group}
        suffix = f" (+{len(wordings) - 1} other wording(s))" if len(wordings) > 1 else ""
        lines.append(
            f"  {state:<14} {seed_id} -- {len(group)} Project(s) [{shown}] -- "
            f"{group[0].detail}{suffix}"
        )
    return lines


@dataclass(frozen=True)
class Plan:
    """What one seed needs in one Project, when it needs anything."""

    org_id: str
    project_id: str
    seed_id: str
    head_id: str
    head_exists: bool
    version_number: int
    predecessor_version_id: str | None


def _absent_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t, to_regclass(t) IS NOT NULL FROM unnest(%s::text[]) AS t",
            (list(REQUIRED_TABLES),),
        )
        return [name for name, present in cur.fetchall() if not present]


def _active_projects(conn) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, org_id FROM app.projects "
            "WHERE status = 'active' AND archived_at IS NULL ORDER BY id"
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def _stored(
    conn, origin: str, declared_ids: list[str]
) -> tuple[dict[str, dict], dict[str, list[tuple[int, str, str]]]]:
    """Every platform-seed head plus every head a declared seed id names, and their versions.

    Returns `({head_id: {...}}, {head_id: [(version_number, version_id, hash)]})`,
    the version lists ordered by number.

    THE SECOND HALF OF THAT SELECT IS THE POINT. Reading `seed_origin =
    'platform_seed'` alone made a head INVISIBLE the moment a person edited it --
    `append_chart_template_version` sets `seed_origin = 'project'` (AC15) -- and an
    invisible head is reported MISSING and re-created, which is how `apply` came to
    die on `a stable head advances only to a NEWER version (2 -> 1)`. The id of a
    platform seed is derived and stable, so asking for it by name is what tells
    "this Project never received the seed" apart from "this Project owns it now".
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, org_id, project_id, current_version_id, seed_origin, archived_at "
            "FROM app.visualization_templates "
            "WHERE seed_origin = %s OR id = ANY(%s)",
            (origin, declared_ids),
        )
        heads = {
            row[0]: {
                "org_id": row[1],
                "project_id": row[2],
                "current_version_id": row[3],
                "seed_origin": row[4],
                "archived_at": row[5],
            }
            for row in cur.fetchall()
        }
        versions: dict[str, list[tuple[int, str, str]]] = {}
        if heads:
            cur.execute(
                "SELECT template_id, version_number, id, content_hash "
                "FROM app.visualization_template_versions "
                "WHERE template_id = ANY(%s) ORDER BY template_id, version_number",
                (list(heads),),
            )
            for template_id, number, version_id, content_hash in cur.fetchall():
                versions.setdefault(template_id, []).append((number, version_id, content_hash))
    return heads, versions


def inspect(conn) -> tuple[list[Finding], list[Plan], int]:
    """Read-only. What the table says versus what the code declares, both ways."""
    from core import visualization_template_seeds as catalogue  # noqa: PLC0415

    projects = _active_projects(conn)
    declared_ids = [
        catalogue.seed_head_id(seed.seed_id, project_id)
        for project_id, _ in projects
        for seed in catalogue.PLATFORM_SEEDS
    ]
    heads, versions = _stored(conn, catalogue.PLATFORM_SEED_ORIGIN, declared_ids)

    findings: list[Finding] = []
    plans: list[Plan] = []

    for project_id, org_id in projects:
        for seed in catalogue.PLATFORM_SEEDS:
            head_id = catalogue.seed_head_id(seed.seed_id, project_id)
            stored_versions = versions.get(head_id, [])
            hashes = {content_hash for _, _, content_hash in stored_versions}

            #  A head this Project has taken responsibility for -- retired, or
            #  edited and thereby owned. It is NAMED and left alone, and only when
            #  the shipped document is absent from it: an operator who archived a
            #  seed the catalogue has not moved is not owed a line every deploy.
            #  No HISTORY is printed for it either -- on such a head every version
            #  is evidence by construction, and repeating that per version would
            #  bury the one line that says what happened.
            held = heads.get(head_id)
            if held is not None and (
                held["archived_at"] is not None
                or held["seed_origin"] != catalogue.PLATFORM_SEED_ORIGIN
            ):
                if seed.content_hash not in hashes:
                    archived = held["archived_at"] is not None
                    findings.append(
                        Finding(
                            ARCHIVED_STALE if archived else PROJECT_OWNED,
                            project_id,
                            seed.seed_id,
                            (
                                "this Project archived this seed, so the version this "
                                f"build declares ({seed.content_hash[:12]}) is not added "
                                "-- restore it here to receive it"
                                if archived
                                else "this Project has edited this seed and owns the head, "
                                f"so the version this build declares "
                                f"({seed.content_hash[:12]}) is not added"
                            ),
                        )
                    )
                continue

            if head_id not in heads:
                findings.append(
                    Finding(
                        MISSING,
                        project_id,
                        seed.seed_id,
                        "this Project holds no head for a seed the code declares",
                    )
                )
                plans.append(
                    Plan(org_id, project_id, seed.seed_id, head_id, False, 1, None)
                )
                continue

            #  Every stored version whose document the code no longer declares.
            #  Printed, never erased: a Report may pin it, and it is the evidence
            #  of what this seed said when that Report was written.
            for number, version_id, content_hash in stored_versions:
                if content_hash != seed.content_hash:
                    findings.append(
                        Finding(
                            HISTORY,
                            project_id,
                            seed.seed_id,
                            f"version {number} ({version_id}) carries {content_hash[:12]}, "
                            f"which this build no longer declares",
                        )
                    )

            if seed.content_hash not in hashes:
                last = stored_versions[-1] if stored_versions else None
                findings.append(
                    Finding(
                        MISSING,
                        project_id,
                        seed.seed_id,
                        "no stored version carries the document this build declares "
                        f"({seed.content_hash[:12]})",
                    )
                )
                plans.append(
                    Plan(
                        org_id,
                        project_id,
                        seed.seed_id,
                        head_id,
                        True,
                        (last[0] + 1) if last else 1,
                        last[1] if last else None,
                    )
                )

    #  The other direction. A head carrying the platform origin that this build
    #  does not declare -- a seed removed from the code, or a row written by a
    #  hand. Never deleted: its absence from the code is exactly what must be
    #  seen rather than tidied away.
    #
    #  JUDGED SEED BY SEED, NOT AGAINST THE ACTIVE PROJECTS, and the difference is
    #  a measured one. Deriving the declared set from the ACTIVE Projects made
    #  every head of an ARCHIVED Project undeclared the moment somebody archived
    #  it -- observed on a disposable database, 48 false UNDECLARED lines from
    #  eight archived Projects -- which would refuse a deployment for a gesture
    #  the product offers. An archived Project's rows are evidence, not a defect.
    #  The question this verdict asks is only "does the shipped catalogue declare
    #  this seed?", so it is asked against the head's own Project.
    for head_id, head in sorted(heads.items()):
        #  Asked of platform-seed heads only. The select above also returns heads
        #  a Project has taken over, and "does the catalogue declare this seed?"
        #  is not a question about a row that no longer claims to be one.
        if head["seed_origin"] != catalogue.PLATFORM_SEED_ORIGIN:
            continue
        declared_here = {
            catalogue.seed_head_id(seed.seed_id, head["project_id"])
            for seed in catalogue.PLATFORM_SEEDS
        }
        if head_id in declared_here:
            continue
        findings.append(
            Finding(
                UNDECLARED,
                head["project_id"],
                head_id,
                "a platform_seed head this build's catalogue does not declare",
            )
        )

    return findings, plans, len(projects)


def apply(conn, plans: list[Plan]) -> int:
    """Insert what is MISSING. Never updates a document, never deletes a row.

    THE VERSION IS WRITTEN BY THE SERVICE, NOT BY SQL OF ITS OWN.
    `chart_template_store.insert_template_version` is the single writer of
    `app.visualization_template_versions` -- the one `create_chart_template`,
    `append_chart_template_version` and the connector seeder already use -- and
    the document it writes is the one `validate_template_document` returns for
    the seed's own document, which is byte for byte the document the catalogue
    derived (the hash is asserted, not assumed).

    AND IT REFUSES BY NAME ON A HEAD IT NO LONGER OWNS. `inspect` plans nothing
    for an archived or project-owned head, so this guard is never met on the
    happy path; it is here because a head can be archived between the read and
    the write, and because a hand-built plan must not be the way around a
    refusal the service makes.
    """
    from core import visualization_template_seeds as catalogue  # noqa: PLC0415
    from core.chart_template_store import (  # noqa: PLC0415
        PROJECT_ORIGIN,
        insert_template_version,
        refuse_version_on_archived_head,
    )
    from core.visualization_templates import validate_template_document  # noqa: PLC0415

    written = 0
    with conn.cursor() as cur:
        for plan in plans:
            seed = catalogue.SEEDS_BY_ID[plan.seed_id]
            cur.execute(
                "SELECT seed_origin, archived_at FROM app.visualization_templates "
                "WHERE id = %s AND org_id = %s AND project_id = %s",
                (plan.head_id, plan.org_id, plan.project_id),
            )
            held = cur.fetchone()
            if held is None:
                cur.execute(
                    """
                    INSERT INTO app.visualization_templates
                        (id, org_id, project_id, label, seed_origin, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        plan.head_id,
                        plan.org_id,
                        plan.project_id,
                        seed.label,
                        catalogue.PLATFORM_SEED_ORIGIN,
                        catalogue.PLATFORM_SEED_AUTHOR,
                    ),
                )
            else:
                refuse_version_on_archived_head(held[1])
                if held[0] == PROJECT_ORIGIN:
                    #  Taken over between the read and the write. Left exactly as
                    #  it is, for the reason `inspect` states.
                    continue

            validated = validate_template_document(seed.document)
            if validated.content_hash != seed.content_hash:
                raise catalogue.SeedCatalogueDefect(
                    f"the seed `{plan.seed_id}` does not round-trip through the validator: "
                    f"declared {seed.content_hash[:12]}, revalidated "
                    f"{validated.content_hash[:12]}"
                )
            version_id = catalogue.seed_version_id(
                plan.seed_id, plan.project_id, plan.version_number
            )
            insert_template_version(
                cur,
                version_id=version_id,
                template_id=plan.head_id,
                org_id=plan.org_id,
                project_id=plan.project_id,
                version_number=plan.version_number,
                validated=validated,
                predecessor_version_id=plan.predecessor_version_id,
                proposed_by=catalogue.PLATFORM_SEED_PROPOSED_BY,
                actor=catalogue.PLATFORM_SEED_AUTHOR,
            )
            #  The head advances onto the version it just gained. The trigger of
            #  migration 333 refuses any move that is not forward, so this is the
            #  one mutation a head accepts.
            cur.execute(
                "UPDATE app.visualization_templates "
                "SET current_version_id = %s, updated_at = NOW() WHERE id = %s",
                (version_id, plan.head_id),
            )
            written += 1
    return written


def report(findings: list[Finding], projects: int, out=sys.stdout) -> None:
    from core import visualization_template_seeds as catalogue  # noqa: PLC0415

    print(
        f"shipped catalogue: {len(catalogue.PLATFORM_SEEDS)} Chart Template seed(s), "
        f"active Projects: {projects}",
        file=out,
    )
    for card in catalogue.NOT_PROJECTABLE:
        #  AC14. Named, with every reason, and never fatal: the shipped registry
        #  simply does not draw what this card composes.
        print(f"  NOT PROJECTABLE {card.card_id} ({card.title})", file=out)
        for reason in card.reasons:
            print(f"                  {reason}", file=out)
    for line in _grouped_lines(findings):
        print(line, file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dsn", default=os.environ.get("PLATFORM_DB_URL", ""))
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is missing or undeclared, write nothing, exit 1 if anything is",
    )
    args = parser.parse_args(argv)
    if not args.dsn:
        print(
            "register_visualization_template_seeds requires --dsn or PLATFORM_DB_URL",
            file=sys.stderr,
        )
        return 2

    try:
        from core import visualization_template_seeds as catalogue  # noqa: PLC0415, F401
    except Exception as exc:  # noqa: BLE001 -- a catalogue that cannot be built is a RED
        print(f"the shipped Chart Template catalogue is not projectable: {exc}", file=sys.stderr)
        print(
            "      Geste qui repare : corriger la declaration fautive dans "
            "server/core/visualization_template_seeds.py, puis relancer.",
            file=sys.stderr,
        )
        return 2

    import psycopg  # noqa: PLC0415 -- the tool is also read by machines with no driver

    with psycopg.connect(args.dsn, connect_timeout=30) as conn:
        absent = _absent_tables(conn)
        if absent:
            print(
                f"the Chart Template tables are not in this database: {', '.join(absent)}",
                file=sys.stderr,
            )
            print(
                "      Geste qui repare : python scripts/apply_migrations.py, puis relancer.",
                file=sys.stderr,
            )
            return 2

        findings, plans, projects = inspect(conn)
        report(findings, projects)

        undeclared = [f for f in findings if f.state == UNDECLARED]
        blocking = [f for f in findings if f.state in BLOCKING]

        if args.check:
            return 1 if blocking else 0

        written = apply(conn, plans)
        conn.commit()
        declared_pairs = projects * len(catalogue.PLATFORM_SEEDS)
        print(
            f"projected: {written} version(s) written, "
            f"{declared_pairs - written} (Project, seed) pair(s) already in place"
        )
        #  An undeclared head is reported and never repaired: choosing whether the
        #  code or the table is right belongs to a person, and a tool that picked
        #  one would erase the evidence for the other.
        return 1 if undeclared else 0


if __name__ == "__main__":
    raise SystemExit(main())
