"""Story 72.5 -- reading and writing the Chart Template object, and nothing else.

WHY THIS MODULE EXISTS AT ALL. Stories 72.1 to 72.4 delivered the table
(migration 333), the document grammar (`core.visualization_templates`), the
compatibility verdict (`core.template_compatibility`) and the platform-seed
projection (`core.visualization_template_seeds`) -- and not one line that LISTS a
template. Measured on 2026-09-01:

    grep -rn "app.visualization_templates" server/core/*.py
    #   template_materialization.py:478   (one head, joined for its label)
    #   visualization_template_seeds.py   (a comment; the module owns no SQL)

So the object had a table, a grammar, a verdict and a projection, and no reader.
A surface cannot show what nothing selects, which is why the coverage audit still
answered `ratified objects reaching no surface : 1`.

WHAT IT REFUSES TO BE. Not a second grammar: every document that enters here goes
through `validate_template_document`, and every predicate that leaves here was
produced by `predicates_of`. Not a second verdict: compatibility is
`core.template_compatibility`'s and is never computed here. Not a second writer of
Visualization Specs: applying a template is `core.template_materialization`'s
single function and this module does not import it.

A SENTENCE A PERSON READS IS COMPOSED HERE, NOT IN A BROWSER. The ratified
criterion is explicit -- "a refusal, an empty state or a compatibility sentence
renders a product identifier where a name is expected, or is composed in the
browser". So `requires_sentence` lives on this side, built from the same
`_asked_for` vocabulary the verdict uses.
"""

from __future__ import annotations

from typing import Any

from ulid import ULID

from core.template_compatibility import (
    TemplatePredicates,
    Unreadable,
    predicates_of,
    requirements_sentence,
)
from core.visualization_families import WELL_LABELS, get_family
from core.visualization_specs import (
    VisualizationNotFound,
    VisualizationRefusal,
    VisualizationSpecRefused,
)
from core.visualization_templates import (
    CHART_TEMPLATE_NOUN,
    validate_template_document,
)

__all__ = [
    "PROJECT_ORIGIN",
    "append_chart_template_version",
    "archive_chart_template",
    "insert_template_version",
    "refuse_version_on_archived_head",
    "create_chart_template",
    "list_chart_templates",
    "list_project_results",
    "load_chart_template",
    "restore_chart_template",
    "template_row_payload",
]

#: The origin a head carries once a person has edited it. Migration 333's
#: `ck_visualization_templates_seed_pair` forces both seed coordinates to NULL at
#: the same instant, which is the database saying what the target says: a
#: connector never owns a Chart Template.
PROJECT_ORIGIN = "project"

#: How each origin is spelled to a person. A seed origin is a stored token; the
#: screen never prints one. `connector_seed` is deliberately incomplete here --
#: the module name completes it in `template_row_payload`, because "seeded by"
#: with nothing after it is not a provenance.
_ORIGIN_LABELS: dict[str, str] = {
    "project": "This Project",
    "platform_seed": "Shipped with toorow",
    "connector_seed": "Seeded by",
}


def _origin_label(seed_origin: str, seed_module_name: str | None) -> str:
    base = _ORIGIN_LABELS.get(seed_origin, seed_origin)
    if seed_origin == "connector_seed":
        return f"{base} {seed_module_name}" if seed_module_name else base
    return base


def _predicates(document: Any, version_id: str) -> TemplatePredicates | Unreadable:
    return predicates_of(document, version_id=version_id)


def template_row_payload(
    *,
    head: dict[str, Any],
    document: Any,
    current_version_id: str | None,
    version_count: int,
) -> dict[str, Any]:
    """One list row, in the words of the person reading it.

    `answers_question`, `family` and the requirement sentence are read off the
    CURRENT version's document through the grammar, never off a column: a column
    would be a second statement of what the document already says, and the two
    would disagree the day a document is written by a path this module does not
    own.

    A head whose current version does not validate under the contract this
    deployment ships is NOT dropped from the list and is not shown as broken
    either: `readable` is false and the reason travels with it, which is the same
    third state the compatibility verdict has.
    """
    predicates = _predicates(document, current_version_id or "")
    readable = isinstance(predicates, TemplatePredicates)
    family = get_family(predicates.family) if readable else None
    return {
        "id": head["id"],
        "label": head["label"],
        "description": head["description"],
        "answers_question": predicates.answers_question if readable else None,
        "family": predicates.family if readable else None,
        "family_label": family.label if family is not None else None,
        "requires": (
            [
                {
                    "well": well,
                    "well_label": WELL_LABELS.get(well, well),
                    "accepts": sorted(entry.get("accepts") or []),
                    "min": entry.get("min"),
                    "max_cardinality": entry.get("max_cardinality"),
                }
                for well, entry in sorted(predicates.requires.items())
            ]
            if readable
            else []
        ),
        #: THE SENTENCE, COMPOSED HERE. "one measure, one dimension with at most
        #: 12 distinct values" -- the row's own words, never a JSON blob the
        #: browser would have to phrase.
        "requires_sentence": requirements_sentence(predicates.requires) if readable else None,
        "readable": readable,
        "unreadable_reason": None if readable else predicates.refusal.as_dict(),
        "content_hash": predicates.content_hash if readable else None,
        "seed_origin": head["seed_origin"],
        "seed": (
            {"module_name": head["seed_module_name"], "template_id": head["seed_template_id"]}
            if head["seed_origin"] == "connector_seed"
            else None
        ),
        "origin_label": _origin_label(head["seed_origin"], head["seed_module_name"]),
        "current_version_id": current_version_id,
        "version_count": version_count,
        "archived": head["archived_at"] is not None,
        "archived_at": head["archived_at"],
        "created_by": head["created_by"],
        "created_at": head["created_at"],
        "updated_at": head["updated_at"],
    }


_HEAD_COLUMNS = """
    t.id, t.label, t.description, t.seed_origin, t.seed_module_name,
    t.seed_template_id, t.current_version_id, t.archived_at, t.created_by,
    t.created_at, t.updated_at
"""


def _head_dict(row) -> dict[str, Any]:
    return {
        "id": row[0],
        "label": row[1],
        "description": row[2],
        "seed_origin": row[3],
        "seed_module_name": row[4],
        "seed_template_id": row[5],
        "current_version_id": row[6],
        "archived_at": row[7].isoformat() if row[7] else None,
        "created_by": row[8],
        "created_at": row[9].isoformat() if row[9] else None,
        "updated_at": row[10].isoformat() if row[10] else None,
    }


def list_chart_templates(
    conn, *, org_id: str, project_id: str, include_archived: bool = False
) -> list[dict[str, Any]]:
    """Every Chart Template head of one Project, newest first.

    The current version's document travels with the head in ONE query, so the
    list does not fan out into N reads to say what each template answers.
    """
    clause = "" if include_archived else "AND t.archived_at IS NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_HEAD_COLUMNS},
                   v.document,
                   (SELECT COUNT(*) FROM app.visualization_template_versions c
                     WHERE c.template_id = t.id AND c.org_id = t.org_id
                       AND c.project_id = t.project_id)
            FROM app.visualization_templates t
            LEFT JOIN app.visualization_template_versions v
              ON v.id = t.current_version_id AND v.org_id = t.org_id
             AND v.project_id = t.project_id
            WHERE t.org_id = %s AND t.project_id = %s {clause}
            ORDER BY t.updated_at DESC, t.id
            """,
            (org_id, project_id),
        )
        rows = cur.fetchall()
    return [
        template_row_payload(
            head=_head_dict(row),
            document=row[11] if isinstance(row[11], dict) else None,
            current_version_id=row[6],
            version_count=int(row[12] or 0),
        )
        for row in rows
    ]


def load_chart_template(conn, *, org_id: str, project_id: str, template_id: str) -> dict[str, Any]:
    """One head, every immutable version, and what already uses it.

    `used_by` is READ, never assumed. A Report version that pins a version of this
    template and a Visualization Spec version materialised from one are two
    different facts and are kept apart -- the first is a presentation pin
    (migration 154), the second is provenance (migration 335).
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_HEAD_COLUMNS}
            FROM app.visualization_templates t
            WHERE t.id = %s AND t.org_id = %s AND t.project_id = %s
            """,
            (template_id, org_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise VisualizationNotFound(f"{CHART_TEMPLATE_NOUN} not found in this Project")
        head = _head_dict(row)

        cur.execute(
            """
            SELECT id, version_number, family, spec_contract_version, schema_version,
                   document, content_hash, predecessor_version_id, proposed_by,
                   created_by, created_at
            FROM app.visualization_template_versions
            WHERE template_id = %s AND org_id = %s AND project_id = %s
            ORDER BY version_number DESC
            """,
            (template_id, org_id, project_id),
        )
        version_rows = cur.fetchall()

        cur.execute(
            """
            SELECT r.id, r.label, rv.id
            FROM app.analysis_report_versions rv
            JOIN app.analysis_reports r ON r.id = rv.report_id AND r.org_id = rv.org_id
            WHERE rv.org_id = %s AND rv.project_id = %s
              AND rv.presentation_kind = 'visualization_template_version'
              AND rv.presentation_version_id IN (
                    SELECT id FROM app.visualization_template_versions
                    WHERE template_id = %s AND org_id = %s AND project_id = %s
              )
            ORDER BY r.label
            """,
            (org_id, project_id, template_id, org_id, project_id),
        )
        report_rows = cur.fetchall()

        cur.execute(
            """
            SELECT sv.visualization_id, sv.id
            FROM app.visualization_spec_versions sv
            WHERE sv.org_id = %s AND sv.project_id = %s
              AND sv.materialized_from_template_version_id IN (
                    SELECT id FROM app.visualization_template_versions
                    WHERE template_id = %s AND org_id = %s AND project_id = %s
              )
            ORDER BY sv.created_at DESC
            """,
            (org_id, project_id, template_id, org_id, project_id),
        )
        derived_rows = cur.fetchall()

    documents = {r[0]: (r[5] if isinstance(r[5], dict) else None) for r in version_rows}
    current_document = documents.get(head["current_version_id"])
    payload = template_row_payload(
        head=head,
        document=current_document,
        current_version_id=head["current_version_id"],
        version_count=len(version_rows),
    )
    payload["versions"] = [
        {
            "id": r[0],
            "version_number": r[1],
            "family": r[2],
            "spec_contract_version": r[3],
            "schema_version": r[4],
            "document": r[5],
            "content_hash": r[6],
            "predecessor_version_id": r[7],
            "proposed_by": r[8],
            "created_by": r[9],
            "created_at": r[10].isoformat() if r[10] else None,
        }
        for r in version_rows
    ]
    payload["used_by"] = {
        "reports": [
            {"report_id": r[0], "label": r[1], "report_version_id": r[2]} for r in report_rows
        ],
        "visualizations": [
            {"visualization_id": r[0], "visualization_spec_version_id": r[1]} for r in derived_rows
        ],
    }
    return payload


def _label_refusal(label: str) -> None:
    if not label.strip() or len(label.strip()) > 200:
        raise VisualizationSpecRefused(
            "chart_template_refused",
            f"a {CHART_TEMPLATE_NOUN} needs a name a person can find it by",
            [
                VisualizationRefusal(
                    "invalid_value",
                    "a name of 1 to 200 characters is required",
                    "/label",
                    "Give this template a short name.",
                )
            ],
        )


def create_chart_template(
    conn,
    *,
    org_id: str,
    project_id: str,
    label: str,
    description: str | None,
    document: Any,
    actor: str,
    proposed_by: str = "person",
) -> dict[str, Any]:
    """A new project-owned head plus its version 1. Both in the caller's transaction.

    The document is validated FIRST and by the one validator: a head is never
    created for a document the grammar refuses, because a head with no version is
    unreadable as a presentation (AC1) and creating one would leave exactly that.
    """
    _label_refusal(label)
    validated = validate_template_document(document)
    template_id = f"vtpl_{ULID()}"
    version_id = f"vtv_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.visualization_templates
                (id, org_id, project_id, label, description, seed_origin, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (template_id, org_id, project_id, label.strip(), description, PROJECT_ORIGIN, actor),
        )
        insert_template_version(
            cur,
            version_id=version_id,
            template_id=template_id,
            org_id=org_id,
            project_id=project_id,
            version_number=1,
            validated=validated,
            predecessor_version_id=None,
            proposed_by=proposed_by,
            actor=actor,
        )
        cur.execute(
            "UPDATE app.visualization_templates SET current_version_id = %s, updated_at = NOW() "
            "WHERE id = %s AND org_id = %s AND project_id = %s",
            (version_id, template_id, org_id, project_id),
        )
    return {
        "template_id": template_id,
        "version_id": version_id,
        "version_number": 1,
        "content_hash": validated.content_hash,
        "family": validated.family,
        "seed_origin": PROJECT_ORIGIN,
    }


def insert_template_version(
    cur,
    *,
    version_id: str,
    template_id: str,
    org_id: str,
    project_id: str,
    version_number: int,
    validated,
    predecessor_version_id: str | None,
    proposed_by: str,
    actor: str,
) -> None:
    import json  # noqa: PLC0415 -- one call site, and it is not on a hot path.

    cur.execute(
        """
        INSERT INTO app.visualization_template_versions
            (id, template_id, org_id, project_id, version_number, family,
             spec_contract_version, schema_version, document, content_hash,
             predecessor_version_id, proposed_by, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
        """,
        (
            version_id,
            template_id,
            org_id,
            project_id,
            version_number,
            validated.family,
            validated.document["spec_contract_version"],
            validated.document["schema_version"],
            json.dumps(validated.document),
            validated.content_hash,
            predecessor_version_id,
            proposed_by,
            actor,
        ),
    )


def refuse_version_on_archived_head(archived_at: Any) -> None:
    """The ONE refusal an archived head makes, wherever a version is about to be written.

    Extracted from `append_chart_template_version` rather than restated, because a
    second copy of this sentence is a second policy and the second one drifts. Its
    other caller is `scripts/register_visualization_template_seeds.py`: the
    projection of the shipped catalogue is a WRITER of versions like any other, and
    the day the catalogue changes a `content_hash` it would otherwise have appended
    a version to a head somebody archived -- which this refusal exists to forbid
    (measured 2026-09-01, AI-353: one version written onto an archived head).

    Passing `None` -- a head that is not archived -- is the whole non-refusal, so
    every write path can call this unconditionally.
    """
    if archived_at is None:
        return
    raise VisualizationSpecRefused(
        "chart_template_archived",
        f"this {CHART_TEMPLATE_NOUN} is archived and accepts no new version",
        [
            VisualizationRefusal(
                "archived",
                "an archived template keeps its versions as evidence and gains none",
                "",
                "List archived templates and restore this one first.",
            )
        ],
    )


def append_chart_template_version(
    conn,
    *,
    org_id: str,
    project_id: str,
    template_id: str,
    document: Any,
    actor: str,
    proposed_by: str = "person",
) -> dict[str, Any]:
    """Succeed the current version. Never rewrite one.

    AND THIS IS WHERE A SEED STOPS BEING A SEED (AC15). Editing a head whose
    origin is `connector_seed` or `platform_seed` makes it project-owned in the
    same statement that appends the version: `seed_origin` becomes `project` and
    migration 333's pair CHECK nulls both coordinates with it. The seed VERSION
    stays exactly as it was -- it is version 1 of this head's lineage and the
    predecessor of what was just written -- so "a seed is read, never rewritten"
    holds byte for byte, and the module that shipped it keeps no claim on the
    object a person has now edited.
    """
    validated = validate_template_document(document)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT current_version_id, seed_origin, archived_at
            FROM app.visualization_templates
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (template_id, org_id, project_id),
        )
        head = cur.fetchone()
        if head is None:
            raise VisualizationNotFound(f"{CHART_TEMPLATE_NOUN} not found in this Project")
        refuse_version_on_archived_head(head[2])
        predecessor = head[0]
        cur.execute(
            "SELECT COALESCE(MAX(version_number), 0) FROM app.visualization_template_versions "
            "WHERE template_id = %s AND org_id = %s AND project_id = %s",
            (template_id, org_id, project_id),
        )
        version_number = int(cur.fetchone()[0]) + 1
        version_id = f"vtv_{ULID()}"
        insert_template_version(
            cur,
            version_id=version_id,
            template_id=template_id,
            org_id=org_id,
            project_id=project_id,
            version_number=version_number,
            validated=validated,
            predecessor_version_id=predecessor,
            proposed_by=proposed_by,
            actor=actor,
        )
        cur.execute(
            """
            UPDATE app.visualization_templates
            SET current_version_id = %s,
                seed_origin = %s,
                seed_module_name = NULL,
                seed_template_id = NULL,
                updated_at = NOW()
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (version_id, PROJECT_ORIGIN, template_id, org_id, project_id),
        )
    return {
        "template_id": template_id,
        "version_id": version_id,
        "version_number": version_number,
        "content_hash": validated.content_hash,
        "family": validated.family,
        "predecessor_version_id": predecessor,
        "seed_origin": PROJECT_ORIGIN,
        "became_project_owned": head[1] != PROJECT_ORIGIN,
    }


def _set_archived(
    conn,
    *,
    org_id: str,
    project_id: str,
    template_id: str,
    archiving: bool,
) -> dict[str, Any]:
    """The ONE writer of `archived_at`, in both directions. Idempotent by construction.

    IT IS `analyze_artifacts._archive_stable_head` ON THIS OBJECT, and deliberately
    the same shape: a stable head whose versions are evidence, retired by a
    timestamp and never by a DELETE. Migration 333 says so in the table itself --
    "Archive, never delete. Every Report version that pinned a version of this
    head is evidence, and evidence does not disappear because a template went out
    of use." So nothing here removes a row, a version or a pin: an archived
    template keeps every version READABLE, and a Report that pinned one goes on
    resolving it.

    IDEMPOTENT, AND IT MATTERS FOR A CONFIRMATION DIALOG. A double-submit, or a
    second person clicking the same control, must not answer an error for a state
    that is already what they asked for. The UPDATE is guarded on the state it is
    leaving, and a zero-row result is RE-READ to tell "already in that state"
    apart from "not yours / not here" -- the two answers a bare rowcount merges.

    NON-DISCLOSING on the second of those: a template of another Project answers
    `VisualizationNotFound`, exactly like every read of this module.

    A SEED IS MADE INACTIVE HERE, NEVER MUTATED. `seed_origin`,
    `seed_module_name` and `seed_template_id` are untouched in both directions --
    unlike `append_chart_template_version`, which takes a head away from its seed
    because EDITING the document is what makes it project-owned. Archiving edits
    no document. The platform catalogue is
    `core.visualization_template_seeds` -- code, not a row (`CLAUDE.md`: *"Un
    catalogue livré avec le produit ne se lit pas dans une table"*) -- and the
    row this touches is the Project's own projection of it, so archiving a
    platform seed retires it IN THIS PROJECT and says nothing about what the
    product ships. `scripts/register_visualization_template_seeds.py` inserts
    only what is MISSING and writes `archived_at` nowhere, so a re-projection
    does not resurrect what a person retired here. It does not append to what is
    retired here either: it calls `refuse_version_on_archived_head` before every
    version it writes, and names the head instead (AI-353).
    """
    leaving = "IS NULL" if archiving else "IS NOT NULL"
    setter = "NOW()" if archiving else "NULL"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE app.visualization_templates
               SET archived_at = {setter}, updated_at = NOW()
             WHERE id = %s AND org_id = %s AND project_id = %s
               AND archived_at {leaving}
            RETURNING id, archived_at
            """,  # noqa: S608 -- both interpolations are literals of this function
            (template_id, org_id, project_id),
        )
        row = cur.fetchone()
        if row is not None:
            return {
                "template_id": row[0],
                "archived": archiving,
                "archived_at": row[1].isoformat() if row[1] else None,
                "unchanged": False,
            }

        # Nothing moved. Which of the two reasons is it?
        cur.execute(
            """
            SELECT archived_at FROM app.visualization_templates
             WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (template_id, org_id, project_id),
        )
        existing = cur.fetchone()
    if existing is None:
        raise VisualizationNotFound(f"{CHART_TEMPLATE_NOUN} not found in this Project")
    return {
        "template_id": template_id,
        "archived": existing[0] is not None,
        "archived_at": existing[0].isoformat() if existing[0] else None,
        #: Said rather than hidden: the caller asked for a state change that had
        #: already happened, and a screen may want to say so quietly instead of
        #: reporting a write it did not make.
        "unchanged": True,
    }


def archive_chart_template(
    conn, *, org_id: str, project_id: str, template_id: str
) -> dict[str, Any]:
    """Retire one Chart Template of this Project. Its versions stay readable.

    What archiving COSTS is what `append_chart_template_version` already refuses
    by name: an archived head accepts no new version. What it does NOT cost is
    anything a Report or a Visualization already holds -- a pin resolves after
    this call exactly as it did before, and `load_chart_template` keeps listing
    every version.
    """
    return _set_archived(
        conn, org_id=org_id, project_id=project_id, template_id=template_id, archiving=True
    )


def restore_chart_template(
    conn, *, org_id: str, project_id: str, template_id: str
) -> dict[str, Any]:
    """Bring one archived Chart Template back into the Project's live list.

    The way back the archive confirmation promises. It restores the head and
    NOTHING else, because nothing else moved: the versions were never touched,
    the current version is the one it had, and the head takes new versions again
    the moment `archived_at` is null.
    """
    return _set_archived(
        conn, org_id=org_id, project_id=project_id, template_id=template_id, archiving=False
    )


def list_project_results(
    conn, *, org_id: str, project_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """The Results a template can be tried against, newest first.

    Only the outcomes that carry an answer are offered. A Result that never
    answered cannot be said to be compatible or incompatible with anything
    (`template_compatibility._OUTCOMES_WITH_AN_ANSWER`), so offering it in the
    picker would put a guaranteed `unavailable` verdict behind a control.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.outcome, r.row_count, r.created_at, q.name
            FROM app.query_results r
            LEFT JOIN app.query_spec_versions qv ON qv.id = r.query_spec_version_id
            LEFT JOIN app.query_specs q ON q.id = qv.query_spec_id
            WHERE r.org_id = %s AND r.project_id = %s
              AND r.outcome IN ('success', 'degraded')
            ORDER BY r.created_at DESC
            LIMIT %s
            """,
            (org_id, project_id, max(1, min(int(limit), 200))),
        )
        rows = cur.fetchall()
    return [
        {
            "result_id": r[0],
            "outcome": r[1],
            "row_count": int(r[2] or 0),
            "created_at": r[3].isoformat() if r[3] else None,
            "question": r[4],
        }
        for r in rows
    ]
