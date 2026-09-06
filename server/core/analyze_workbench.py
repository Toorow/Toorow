"""Story 50.2 -- the governed reads Explore and the Result workbench consume.

WHY A SECOND MODULE AND NOT A SECOND SERVICE. Story 50.1 owns the analytical
WRITE path: validate a Query Spec, version it, execute it, insert one immutable
Result. This module owns nothing of that. It composes READS over the objects 50.1
already wrote, plus the governed catalogues those objects point at. Every write
still goes through `core.query_specs` / `core.query_execution`, and nothing here
inserts, updates or deletes.

WHY THE UI CANNOT DO THIS COMPOSITION ITSELF (AC3, AC8). A browser assembling the
facet list would have to join the compiled artifact, the concept versions, the
classification registry and the query policy. Four owners joined in a client is
four chances to keep offering a member the day one of them stops proving it --
and the join would drift silently, because no test watches a browser's idea of
compatibility. So the server composes ONE response from the SAME compiled
artifact `core.query_specs` validates against, and the control vocabulary
(operators, comparisons, limits) is imported from that validator rather than
retyped here. If the validator's vocabulary changes, this response changes with
it; it cannot disagree.

HONEST ABSENCE IS A FIRST-CLASS ANSWER. Several links the Result lenses want do
not exist yet in this repository, and each one is named rather than filled:

  * per-value `(source_system, source_field, pull_id)` tuples are NOT on the
    Result manifest. `core.query_execution.run_execution` writes the manifest and
    belongs to Story 50.1; enriching it from here would mean re-deriving, at read
    time, evidence about a past execution from TODAY's binding state. That is the
    exact "reconstruct historical meaning from current state" failure AC8 names.
    So the provenance lens reports the link as `not_recorded` with its owner.
  * an AI Path exists only when an AI produced the Result. Human-only Explore
    work has no path, and the answer is the exact literal `No AI path` -- not
    null, not an empty panel, not `deferred`.
  * a grain vocabulary comes from the concept version's `allowed_grains`. When a
    published view carries none, the answer is an empty list WITH a reason, never
    a plausible `["day", "week", "month"]` invented here.

NON-DISCLOSURE. `WorkbenchNotFound` is raised for foreign, denied and absent
identities alike, and scope is part of every lookup's WHERE clause rather than a
check applied to a row already read. A caller cannot tell which of the three it
hit, so counts and labels never become a tenant-enumeration oracle (AC13).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core import business_identity_catalogue as catalogue
from core.query_specs import (
    _COMPARISONS,
    _FILTER_OPERATORS,
    _SORT_DIRECTIONS,
    COMPARISON_PRECONDITIONS,
    DEFAULT_ROW_LIMIT,
    MAX_DIMENSIONS,
    MAX_FILTERS,
    MAX_MEASURES,
    MAX_ROW_LIMIT,
)

#: Bumped when the composed shape changes. The client validates it before
#: rendering, so a stale bundle refuses rather than half-renders (Task 2).
FACETS_SCHEMA_VERSION = "analyze-query-facets.v1"
LENS_SCHEMA_VERSION = "analyze-result-lens.v1"

#: The six lenses AC5 fixes, in the order the workbench shows them. `view` is the
#: declared default -- declared, not `LENSES[0]`, because a default that is
#: inferred from list order changes when someone reorders the list.
LENSES = ("view", "data", "definitions", "quality", "provenance", "ai-path")
DEFAULT_LENS = "view"

#: Rows a lens will return inline. The Result payload is already bounded by
#: `query_execution.MAX_INLINE_ROWS`; this is the SECOND bound, applied per read,
#: and it is disclosed on every response that hits it. A bounded read that does
#: not say it is bounded reads exactly like a complete one.
LENS_ROW_SLICE = 200

#: Outcomes that produced NO answer. Their stored `row_count`, `cell_count`,
#: `byte_count` and `truncated` are facts about an EMPTY PAYLOAD, not measurements
#: of an answer -- so returning them as numbers renders "Rows 0 / Cells 0 /
#: Truncated No" for a question that was never asked, which is exactly what AC9
#: forbids ("`Unknown`, `Unavailable`, and `Unverifiable` are never rendered as
#: healthy or as zero"). They are returned as `null` instead, with the reason
#: beside them, so every reader -- this workbench, the MCP tools of Story 50.6,
#: any future consumer -- has to say "Unavailable" rather than "0".
NO_ANSWER_OUTCOMES = frozenset({"unavailable", "refused"})

#: Story 50.1 owns writing the manifest, including the evidence snapshot taken at
#: execution time. Named once so every "not recorded" reason points at the same
#: module instead of at three slightly different sentences.
EVIDENCE_OWNER = "core.query_execution.capture_evidence"


logger = logging.getLogger(__name__)


class WorkbenchNotFound(LookupError):
    """Foreign, denied or absent. Externally indistinguishable, by construction."""


class UnknownLens(ValueError):
    """A lens slug the contract does not declare. Never repaired into `view`."""


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _owner(
    *,
    workspace: str,
    section: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    tab: str | None = None,
    version_id: str | None = None,
) -> dict[str, Any]:
    """One exact-owner reference shape, used by every lens.

    Deliberately structured rather than a string. A lens that built `"/org/…"`
    itself would be a second router, and the two would disagree the first time
    the address grammar moved -- which it does in this very story.

    `section` is OPTIONAL and that is a decision, not an oversight. Which console
    section holds a given object type is a fact owned by `shell/navigation.ts`;
    a table of it here would be a second registry, and the two would disagree the
    first time an object moved section -- which is exactly how a hardcoded
    `section="knowledge-library"` made every AI Path step that was not a Context
    Topic unreachable. When this module does not know the section for certain, it
    emits the workspace/object-type/object-id triple with `section: null` and the
    client resolves it through the ONE registry, refusing when the answer is
    ambiguous.
    """
    return {
        "workspace": workspace,
        "section": section,
        "object_type": object_type,
        "object_id": object_id,
        "tab": tab,
        "version_id": version_id,
    }


# ---------------------------------------------------------------------------
# AC3 -- one server-composed option/facet response.
# ---------------------------------------------------------------------------


def _concept_semantics(conn, project_id: str, version_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Read the exact concept VERSIONS the compiled matrix names.

    Keyed by version id, never by concept id: two versions of one concept carry
    different formulas, and a lens that keyed by concept would show today's
    meaning under a historical Result.
    """
    if not version_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, concept_id, kind, name, label, definition, value_type, unit,
                   expression, aggregation, additivity_class, non_additive_dimensions,
                   currency_behavior, time_behavior, semantic_type, allowed_grains,
                   hierarchies, value_domain, version_number, status
              FROM app.semantic_concept_versions
             WHERE project_id = %s AND id = ANY(%s)
            """,
            (project_id, version_ids),
        )
        names = (
            "version_id", "concept_id", "kind", "name", "label", "definition", "value_type",
            "unit", "expression", "aggregation", "additivity_class", "non_additive_dimensions",
            "currency_behavior", "time_behavior", "semantic_type", "allowed_grains",
            "hierarchies", "value_domain", "version_number", "status",
        )
        return {row[0]: dict(zip(names, row)) for row in cur.fetchall()}


def _is_time_member(semantics: dict[str, Any]) -> bool:
    """Derived from stored columns, never from the label.

    A member is a time member when the semantic model SAYS so -- through its
    semantic type, its declared grains or its value type. Matching on a name
    containing "date" would make an English label load-bearing, and would pick up
    `signup_date_bucket` while missing `reporting_period`.
    """
    if str(semantics.get("semantic_type") or "").lower() in {"time", "date", "datetime", "period"}:
        return True
    if semantics.get("allowed_grains"):
        return True
    return str(semantics.get("value_type") or "").lower() in {"date", "timestamp", "datetime"}


def _classification_facets(
    conn, org_id: str, dimensions: list[dict[str, Any]]
) -> tuple[list, list]:
    """Facets for the dimensions whose value domain names a classification.

    "Only where an approved binding exists" (AC3) is enforced by the join: a
    dimension whose `value_domain` names no classification type produces NO facet
    at all, and a named type with no active classification rows produces an
    explicit unavailable entry. Neither produces an empty picker that looks
    operable and refuses on submit.
    """
    available: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for dimension in dimensions:
        domain = dimension.get("value_domain")
        facet_type = None
        if isinstance(domain, dict):
            facet_type = domain.get("classification_type") or domain.get("registry")
        if not facet_type:
            continue
        with conn.cursor() as cur:
            # Story 49.2: the facet's choices resolve through the authority, so a
            # classification minted in Master Data is offerable the day it is
            # created. Read from the superseded store alone, this answered "no
            # active `<type>` classification is approved in this organization"
            # about a type the organization had just declared.
            cur.execute(
                f"""
                SELECT c.id, c.slug, c.name, c.domain_id, c.parent_id,
                       (SELECT MAX(version_number)
                          FROM {catalogue.CLASSIFICATION_VERSION_SOURCE} v
                         WHERE v.classification_id = c.id AND v.org_id = c.org_id)
                  FROM {catalogue.CLASSIFICATION_SOURCE} c
                 WHERE c.org_id = %s AND c.classification_type = %s AND c.status = 'active'
                 ORDER BY c.name
                 LIMIT 500
                """,
                (org_id, str(facet_type)),
            )
            rows = cur.fetchall()
        if not rows:
            unavailable.append(
                {
                    "facet": str(facet_type),
                    "dimension_id": dimension["concept_id"],
                    "reason": (
                        f"no active `{facet_type}` classification is approved in this "
                        "organization, so this facet cannot be chosen"
                    ),
                }
            )
            continue
        available.append(
            {
                "facet": str(facet_type),
                "dimension_id": dimension["concept_id"],
                "dimension_version_id": dimension["version_id"],
                "members": [
                    {
                        "classification_object_id": row[0],
                        "slug": row[1],
                        "label": row[2],
                        "hierarchy_id": row[3],
                        "parent_id": row[4],
                        "hierarchy_version_id": (
                            f"{row[0]}@{row[5]}" if row[5] is not None else None
                        ),
                    }
                    for row in rows
                ],
                # AC3: `Other` and `Unknown` stay explicit where the governed
                # classification defines them. They are listed as reserved rather
                # than mixed into the member list, so nobody reads them as a real
                # product and nobody loses them by filtering "real" members.
                "reserved_members": ["Other", "Unknown"],
            }
        )
    return available, unavailable


def compose_query_facets(
    conn, *, org_id: str, project_id: str, semantic_view_id: str, semantic_view_version_id: str
) -> dict[str, Any]:
    """The single governed option response Explore renders its controls from."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.status, v.query_policy, v.label, v.name, v.version_number,
                   v.business_domain_refs, w.name
              FROM app.semantic_view_versions v
              JOIN app.semantic_views w ON w.id = v.view_id AND w.project_id = v.project_id
             WHERE v.id = %s AND v.view_id = %s AND v.project_id = %s
            """,
            (semantic_view_version_id, semantic_view_id, project_id),
        )
        version = cur.fetchone()
        if version is None:
            raise WorkbenchNotFound("semantic view version not found in this Project")
        cur.execute(
            """
            SELECT queryability_matrix
              FROM app.semantic_compiled_artifacts
             WHERE view_version_id = %s AND project_id = %s
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (semantic_view_version_id, project_id),
        )
        compiled = cur.fetchone()

    matrix = (compiled[0] if compiled else None) or {}
    policy = version[2] if isinstance(version[2], dict) else {}
    status = str(version[1])
    unavailable: list[dict[str, str]] = []
    if status != "published":
        unavailable.append(
            {
                "code": "version_not_executable",
                "message": (
                    f"this Semantic View version is {status}, not published. It is shown "
                    "exactly as pinned and no other version has been substituted."
                ),
            }
        )
    if not matrix:
        unavailable.append(
            {
                "code": "not_compiled",
                "message": "this Semantic View version has no compiled artifact to query",
            }
        )

    raw_measures = list(matrix.get("metrics") or [])
    raw_dimensions = list(matrix.get("dimensions") or [])
    semantics = _concept_semantics(
        conn,
        project_id,
        [str(m.get("version_id")) for m in raw_measures + raw_dimensions if m.get("version_id")],
    )

    def _member(entry: dict[str, Any]) -> dict[str, Any]:
        version_id = str(entry.get("version_id") or "")
        detail = semantics.get(version_id, {})
        return {
            "concept_id": str(entry.get("concept_id") or ""),
            "version_id": version_id,
            "label": str(entry.get("label") or detail.get("label") or detail.get("name") or ""),
            "definition": detail.get("definition"),
            "value_type": detail.get("value_type"),
            "unit": detail.get("unit"),
            "expression": detail.get("expression"),
            "aggregation": detail.get("aggregation"),
            "additivity_class": detail.get("additivity_class"),
            "non_additive_dimensions": detail.get("non_additive_dimensions"),
            "currency_behavior": detail.get("currency_behavior"),
            "time_behavior": detail.get("time_behavior"),
            "semantic_type": detail.get("semantic_type"),
            "allowed_grains": detail.get("allowed_grains") or [],
            "hierarchies": detail.get("hierarchies"),
            "value_domain": detail.get("value_domain"),
            # The owner this member is DEFINED by. Every reference in this
            # response can be opened; none of them is a label used as identity.
            "owner_ref": _owner(
                workspace="governance",
                section="semantic-model",
                object_type="semantic-concept",
                object_id=str(entry.get("concept_id") or ""),
                tab="definition",
            ),
        }

    measures = [_member(m) for m in raw_measures]
    dimensions = [_member(d) for d in raw_dimensions]
    time_members = [d for d in dimensions if _is_time_member(d)]
    grains = sorted({str(g) for d in time_members for g in (d.get("allowed_grains") or [])})

    facets, facets_unavailable = _classification_facets(conn, org_id, dimensions)
    ceiling = int(policy.get("max_row_limit") or MAX_ROW_LIMIT)

    return {
        "schema_version": FACETS_SCHEMA_VERSION,
        "project_id": project_id,
        "semantic_view_id": semantic_view_id,
        "semantic_view_version_id": semantic_view_version_id,
        "semantic_view_label": version[3] or version[4] or version[7],
        "semantic_view_version_number": version[5],
        "business_domain_refs": version[6] or [],
        "status": status,
        # Stated by the server. A client that inferred executability from an
        # empty member list would call a compiled-but-empty view unexecutable and
        # a published-but-uncompiled one executable -- both wrong.
        "executable": status == "published" and bool(matrix),
        "measures": measures,
        "dimensions": dimensions,
        "pairs": [
            {
                "measure_id": cell.get("metric_id"),
                "dimension_id": cell.get("dimension_id"),
                "queryable": bool(cell.get("queryable")),
                # The compiler's own words when it refused. A UI that wrote its
                # own reason would be guessing why the join failed.
                "reason": cell.get("reason"),
            }
            for cell in (matrix.get("cells") or [])
        ],
        "time": {
            "members": [
                {
                    "concept_id": m["concept_id"],
                    "version_id": m["version_id"],
                    "label": m["label"],
                    "allowed_grains": m["allowed_grains"],
                }
                for m in time_members
            ],
            "grains": grains,
            "grains_unavailable_reason": (
                None
                if grains
                else "no time member of this published version declares an allowed grain"
            ),
            "comparisons": sorted(_COMPARISONS),
            # THE THREE CONDITIONS A COMPARISON CANNOT BE COMPILED WITHOUT, WITH
            # THE VALIDATOR'S OWN SENTENCES. A screen must be able to stop asking
            # a question it knows the door would refuse -- but a screen that
            # RETYPES the refusal is how one product comes to have two wordings
            # for one refusal, and how the console starts lying the day this
            # validator changes its words. The order is the order the door tests
            # them in, so the first unmet condition named here is the one the
            # door would name.
            "comparison_preconditions": [
                {"condition": precondition.condition, "message": precondition.message}
                for precondition in COMPARISON_PRECONDITIONS
            ],
            # WHAT THIS PATH EXECUTES, AND WHAT IT REFUSES. A facet that offers a
            # control the read ignores is how a person comes to believe a boundary
            # was applied. `as_of` restricts the rows to those loaded by then; a
            # comparison labels two frozen windows; the other two are refused at
            # the door, and say why here rather than at the end of a run.
            "as_of_supported": True,
            "reporting_boundary_supported": False,
            "reporting_boundary_unsupported_reason": (
                "No governed reporting boundary exists to pin, and this path does not yet "
                "execute one. Use the From and To dates of the window."
            ),
            "timezone_supported": False,
            "timezone_unsupported_reason": (
                "Governed grains are dates. The reporting time zone is declared on the "
                "Datastream and signalled there; a Result is never re-projected under "
                "another zone when it is read."
            ),
        },
        "sort": {"directions": sorted(_SORT_DIRECTIONS)},
        "filter_operators": sorted(_FILTER_OPERATORS),
        "limits": {
            "default_row_limit": min(DEFAULT_ROW_LIMIT, ceiling),
            "max_row_limit": ceiling,
            "max_measures": MAX_MEASURES,
            "max_dimensions": MAX_DIMENSIONS,
            "max_filters": MAX_FILTERS,
        },
        "classification_facets": facets,
        "classification_facets_unavailable": facets_unavailable,
        "unavailable_reasons": unavailable,
        "owner_ref": _owner(
            workspace="governance",
            section="semantic-model",
            object_type="semantic-view",
            object_id=semantic_view_id,
            tab="versions",
            version_id=semantic_view_version_id,
        ),
    }


# ---------------------------------------------------------------------------
# The immutable Result, and its six lenses.
# ---------------------------------------------------------------------------


def load_result_record(conn, *, org_id: str, project_id: str, result_id: str) -> dict[str, Any]:
    """Read one Result with its mandatory payload and its Query Spec version.

    The three tables are joined in ONE statement so a Result can never be shown
    with a payload from a different read -- and scope is in the WHERE clause of
    that statement, so a foreign id produces no row rather than a filtered one.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.attempt_id, r.query_spec_version_id, r.outcome, r.ai_path_id,
                   r.ai_path_absent_literal, r.content_hash, r.row_count, r.cell_count,
                   r.byte_count, r.truncated, r.predecessor_result_id, r.started_at, r.ended_at,
                   p.result_schema, p.manifest, p.rows_chunk,
                   v.query_spec_id, v.version_number, v.semantic_view_id,
                   v.semantic_view_version_id, v.spec, v.content_hash, v.created_at,
                   -- The WORDS behind the two pins.
                   --
                   -- LEFT BY DEFENCE, AND FOR NO OTHER REASON. The first version
                   -- of this comment justified it with "a cross-source Result can
                   -- pin a view version this Project no longer carries" -- an
                   -- invented design decision, and the schema forbids it:
                   -- `fk_query_spec_versions_semantic_scope`
                   -- (`151_query_specs_and_results.sql:93`) makes
                   -- (semantic_view_version_id, semantic_view_id, project_id) a
                   -- foreign key, the join condition here is a strict subset of
                   -- it, the version row is immutable and undeletable
                   -- (`142_semantic_model.sql:392`, BEFORE UPDATE OR DELETE), and
                   -- `label` / `name` / `version_number` are all NOT NULL. The
                   -- join cannot miss. LEFT stays because a read has no business
                   -- turning a schema surprise into "Result not found", not
                   -- because anyone has seen it miss.
                   sv.label, sv.name, sv.version_number
              FROM app.query_results r
              JOIN app.query_result_payloads p
                ON p.result_id = r.id AND p.org_id = r.org_id AND p.project_id = r.project_id
              JOIN app.query_spec_versions v
                ON v.id = r.query_spec_version_id AND v.org_id = r.org_id
               AND v.project_id = r.project_id
              LEFT JOIN app.semantic_view_versions sv
                ON sv.id = v.semantic_view_version_id AND sv.project_id = v.project_id
             WHERE r.id = %s AND r.org_id = %s AND r.project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkbenchNotFound("result not found in this Project")
        names = (
            "id", "attempt_id", "query_spec_version_id", "outcome", "ai_path_id",
            "ai_path_absent_literal", "content_hash", "row_count", "cell_count", "byte_count",
            "truncated", "predecessor_result_id", "started_at", "ended_at", "result_schema",
            "manifest", "rows_chunk", "query_spec_id", "version_number", "semantic_view_id",
            "semantic_view_version_id", "spec", "spec_content_hash", "spec_created_at",
            "semantic_view_label", "semantic_view_name", "semantic_view_version_number",
        )
        record = dict(zip(names, row))

        # The retry chain, forwards. `predecessor_result_id` gives the past; a
        # person looking at a superseded Result needs to know a newer one exists,
        # and inferring that from "no rows" would be a guess.
        cur.execute(
            """
            SELECT id, outcome, ended_at FROM app.query_results
             WHERE predecessor_result_id = %s AND org_id = %s AND project_id = %s
             ORDER BY started_at
            """,
            (result_id, org_id, project_id),
        )
        record["successors"] = [
            {"result_id": s[0], "outcome": s[1], "ended_at": _iso(s[2])} for s in cur.fetchall()
        ]
    return record


def _query_context(record: dict[str, Any]) -> dict[str, Any]:
    spec = record.get("spec") or {}
    time = spec.get("time") or {}
    return {
        "semantic_view_id": record["semantic_view_id"],
        "semantic_view_version_id": record["semantic_view_version_id"],
        "query_spec_id": record["query_spec_id"],
        "query_spec_version_id": record["query_spec_version_id"],
        "query_spec_version_number": record["version_number"],
        "measures": spec.get("measures") or [],
        "dimensions": spec.get("dimensions") or [],
        "filters": spec.get("filters") or [],
        "sort": spec.get("sort") or [],
        "comparison": spec.get("comparison"),
        # The two frozen windows when one was compiled, so the Data lens states the
        # boundary it actually read rather than the enum that asked for it.
        "comparison_windows": spec.get("comparison_windows"),
        "grain": spec.get("grain"),
        "row_limit": spec.get("row_limit"),
        # `timezone` and `reporting_boundary_id` are not echoed: they are refused
        # at the door, so a request can no longer carry either, and printing a
        # dead key would keep suggesting the read honoured something.
        "time": {
            "member_id": time.get("member_id"),
            "start": time.get("start"),
            "end": time.get("end"),
            "as_of": time.get("as_of"),
        },
    }


def _datum_key(result_id: str, row_index: int, field: str) -> str:
    """The stable identity of one displayed figure.

    It is derived from the IMMUTABLE Result id plus the row's position in the
    immutable payload, so the same figure keeps the same key across refreshes,
    across lenses and inside a copied address. A key minted per render would make
    a shared evidence link point at a different number tomorrow.
    """
    return f"{result_id}:{row_index}:{field}"


def _declared_governance(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Column name -> what the plan FROZE the number to mean (`value_type`, `unit`).

    Read from `result_schema.fields`, which `multi_source_execution.result_schema`
    writes at plan time -- the same fields `pivot_projection` reads to fill
    `value_fields`. One source, so the Console table, the pivot matrix and the MCP
    App cannot state one amount three ways (amendment 2026-08-15).

    NOTHING IS INFERRED. A field the schema declares without a `value_type` gets
    no key here, and the reader prints the plain number it is: a Result frozen
    before the plan carried units says nothing about them, and silence is not a
    currency.
    """
    schema = record.get("result_schema") or {}
    governance: dict[str, dict[str, Any]] = {}
    for field in schema.get("fields") or []:
        if not isinstance(field, dict) or not field.get("name"):
            continue
        stated = {
            key: field[key]
            for key in ("value_type", "unit")
            if field.get(key) is not None
        }
        if stated:
            governance[str(field["name"])] = stated
    return governance


def _field_names(record: dict[str, Any]) -> list[str]:
    schema = record.get("result_schema") or {}
    fields = [str(f.get("name")) for f in (schema.get("fields") or []) if f.get("name")]
    if fields:
        return fields
    rows = record.get("rows_chunk") or []
    return [str(k) for k in (rows[0].keys() if rows else [])]


def _bounded_rows(record: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    rows = list(record.get("rows_chunk") or [])
    sliced = rows[:LENS_ROW_SLICE]
    return sliced, len(rows) > LENS_ROW_SLICE


def _has_answer(record: dict[str, Any]) -> bool:
    """False when the outcome means the question was never answered."""
    return str(record.get("outcome")) not in NO_ANSWER_OUTCOMES


def _counts_unavailable_reason(record: dict[str, Any]) -> str | None:
    """Why the counts are `null` -- in the outcome's own terms, never generic."""
    if _has_answer(record):
        return None
    outcome = str(record.get("outcome"))
    if outcome == "refused":
        return (
            "this query was refused, so no row, cell or byte count describes an answer. "
            "The stored counts measure an empty payload and are withheld rather than "
            "reported as zero."
        )
    return (
        "this query could not be asked, so no row, cell or byte count describes an answer. "
        "The stored counts measure an empty payload and are withheld rather than reported "
        "as zero -- `we could not ask` and `the answer is zero` are different facts."
    )


def _answer_count(record: dict[str, Any], value: Any) -> Any:
    """A measurement, or `None` when the outcome measured nothing.

    Deliberately applied at the SERVER, not at each screen. Story 50.4's Builder
    and Story 50.6's MCP tools read the same fields; a rule enforced only in this
    workbench would be a rule three consumers each had to rediscover, and two of
    them would print `0`.
    """
    return value if _has_answer(record) else None


def _member_columns(record: dict[str, Any]) -> dict[str, str]:
    """Concept id -> payload column name, from the manifest's OWN snapshot.

    `build_sql` emits `SUM(col) AS col`, so a payload field name is a physical
    column, while a Query Spec names semantic concept ids (`sc_…`). The two are
    joined by the member/column map `capture_evidence` snapshotted at execution
    time -- read here, never re-derived from today's mapping, which would label a
    historical Result with a binding that has since moved.
    """
    provenance = (record.get("manifest") or {}).get("provenance") or {}
    return {
        str(entry.get("member_id")): str(entry.get("source_field"))
        for entry in (provenance.get("values") or [])
        if entry.get("member_id") and entry.get("source_field")
    }


def _quality_summary(record: dict[str, Any]) -> dict[str, Any]:
    """Freshness, completeness and limitation -- three different facts.

    `Unknown` is returned as `Unknown`. It is never rendered as zero and never
    folded into "healthy": a missing freshness stamp and a fresh one are not the
    same answer, and AC9 exists because collapsing them is the easy mistake.
    """
    manifest = record.get("manifest") or {}
    outcome = record["outcome"]
    limitations: list[dict[str, str]] = []
    if record.get("truncated"):
        limitations.append(
            {
                "code": "truncated",
                "message": (
                    f"the query returned more than its {manifest.get('row_limit') or 'declared'} "
                    "row limit; the rows shown are a bounded slice, not the complete answer"
                ),
            }
        )
    if manifest.get("unavailable_reason"):
        limitations.append(
            {
                "code": str(manifest.get("missing_link") or "unavailable"),
                "message": str(manifest["unavailable_reason"]),
            }
        )
    if outcome == "empty":
        limitations.append(
            {
                "code": "empty",
                "message": (
                    "the query ran against the governed path and nothing matched. "
                    "This is an answer, not a missing answer."
                ),
            }
        )
    as_of = ((record.get("spec") or {}).get("time") or {}).get("as_of")
    return {
        "outcome": outcome,
        "freshness": {
            "result_ended_at": _iso(record.get("ended_at")),
            "requested_as_of": as_of,
            "state": "Unknown" if record.get("ended_at") is None else "recorded",
        },
        "completeness": {
            # `null`, not `0`, when the outcome answered nothing. The raw stored
            # counts still drive the limitations above -- what is withheld is the
            # NUMBER a screen would print in a tile, not the fact.
            "row_count": _answer_count(record, record.get("row_count")),
            "cell_count": _answer_count(record, record.get("cell_count")),
            "truncated": _answer_count(record, bool(record.get("truncated"))),
            "row_limit": manifest.get("row_limit"),
            "counts_unavailable_reason": _counts_unavailable_reason(record),
        },
        "limitations": limitations,
    }


def _lens_view(conn, record: dict[str, Any], project_id: str) -> dict[str, Any]:
    """A bounded inspection projection. NOT a Visualization (AC6).

    It carries no chart type, no encoding, no renderer option and no saved state.
    Everything below is read straight out of the immutable Result: the values are
    the payload's own rows, the totals are the server's own counts. Nothing is
    aggregated, converted, sorted or re-typed on the way through -- which is what
    keeps this a projection instead of a second analytical contract.
    """
    fields = _field_names(record)
    rows, sliced = _bounded_rows(record)
    spec = record.get("spec") or {}

    # A cell's `kind` is resolved through the manifest's member->column snapshot,
    # NOT by testing a physical column name against a set of concept ids: those
    # two vocabularies never intersect, so the naive test labels every cell
    # `dimension` and no fixture ever notices. When the snapshot is absent the
    # answer is `unknown` with a reason -- a plausible wrong label is worse than
    # an admitted gap, and Stories 50.4/50.6 read this field.
    columns = _member_columns(record)
    measure_fields = {
        columns[str(m.get("id"))]
        for m in (spec.get("measures") or [])
        if str(m.get("id")) in columns
    }
    dimension_fields = {
        columns[str(d.get("id"))]
        for d in (spec.get("dimensions") or [])
        if str(d.get("id")) in columns
    }

    def _kind(field: str) -> str:
        if field in measure_fields:
            return "measure"
        if field in dimension_fields:
            return "dimension"
        return "unknown"

    values = [
        {
            "row_index": index,
            "cells": [
                {
                    "field": field,
                    "value": row.get(field),
                    "datum_key": _datum_key(record["id"], index, field),
                    "kind": _kind(field),
                }
                for field in fields
            ],
        }
        for index, row in enumerate(rows)
    ]
    # AI-289 -- LA DEFINITION VOYAGE AVEC LA COLONNE.
    #
    # Elle existait deja, dans le lens `definitions` : un AUTRE onglet. Une
    # personne qui lit un nombre ici devait donc changer de vue pour savoir ce
    # que ce nombre compte, et une definition qu'il faut aller chercher est une
    # definition que personne ne lit. Le critere de la ligne d'action item le dit
    # ainsi : elle doit atteindre la personne LA OU ELLE LIT LE NOMBRE.
    #
    # MEME LECTURE que le lens `definitions`, jamais une seconde : `_concept_semantics`
    # est keye par VERSION, donc ce qui s'affiche ici est le sens que la mesure
    # avait au moment de l'execution -- pas celui d'aujourd'hui. Deux lectures se
    # seraient contredites le jour ou quelqu'un republie un Concept.
    #
    # Keye par NOM DE COLONNE et non par id de concept, parce que c'est le nom de
    # colonne que la cellule porte : l'ecran n'a alors rien a rapprocher.
    members = list(spec.get("measures") or []) + list(spec.get("dimensions") or [])
    semantics = _concept_semantics(
        conn, project_id, [str(m.get("version_id")) for m in members if m.get("version_id")]
    )
    # WHAT THE NUMBER IS, frozen by the plan and carried by the Result schema
    # (amendment 2026-08-15). The pivot projection echoes the SAME two keys from
    # the SAME place (`pivot_projection.matrix.value_fields`), so a Console cell
    # and an MCP App cell cannot state one amount two ways. The concept version
    # is only a fallback for a Result frozen before the schema carried them --
    # and when neither says, nothing is inferred and the plain number is read.
    declared_governance = _declared_governance(record)
    field_semantics: dict[str, dict[str, Any]] = {}
    for member in members:
        column = columns.get(str(member.get("id")))
        detail = semantics.get(str(member.get("version_id"))) or {}
        if not column:
            continue
        governance = declared_governance.get(column) or {}
        field_semantics[column] = {
            "label": detail.get("label") or detail.get("name"),
            "definition": detail.get("definition"),
            "value_type": governance.get("value_type") or detail.get("value_type"),
            "unit": governance.get("unit") or detail.get("unit"),
            "aggregation": detail.get("aggregation"),
            "version_number": detail.get("version_number"),
            # Une version epinglee qui ne se relit pas se DIT, exactement comme
            # dans le lens definitions : montrer la version courante a sa place
            # serait la reecriture d'historique qu'AC8 interdit.
            "resolved": bool(detail),
            "owner_ref": _owner(
                workspace="governance",
                section="semantic-model",
                object_type="semantic-concept",
                object_id=str(member.get("id") or ""),
                tab="definition",
                version_id=str(member.get("version_id") or "") or None,
            ),
        }

    return {
        "outcome": record["outcome"],
        "query_context": _query_context(record),
        "quality_summary": _quality_summary(record),
        "fields": fields,
        # Nom de colonne -> ce que la mesure VEUT DIRE. Absent pour une colonne
        # que le Query Spec ne nomme pas : le lens ne fabrique pas de sens.
        "field_semantics": field_semantics,
        "values": values,
        "kind_unavailable_reason": (
            None
            if columns
            else (
                "this Result's manifest carries no member-to-column map, so no cell can be "
                f"labelled measure or dimension. The map is snapshotted by {EVIDENCE_OWNER} "
                "at execution time; it has NOT been re-derived here from today's mapping."
            )
        ),
        # The three disclosures AC6 requires to stay visible whatever the browser
        # does with local presentation state. `null` when the outcome measured
        # nothing -- see NO_ANSWER_OUTCOMES.
        "server_row_count": _answer_count(record, record.get("row_count")),
        "returned_row_count": _answer_count(record, len(rows)),
        "truncated": _answer_count(record, bool(record.get("truncated"))),
        "counts_unavailable_reason": _counts_unavailable_reason(record),
        "read_sliced": sliced,
        "read_slice_size": LENS_ROW_SLICE,
        "local_interaction_scope": (
            "Hiding or selecting rows here changes only what is shown of the rows this "
            "Result already returned. Any other change needs a new query and produces a "
            "new Result."
        ),
    }


def _lens_data(record: dict[str, Any]) -> dict[str, Any]:
    fields = _field_names(record)
    rows, sliced = _bounded_rows(record)
    schema = record.get("result_schema") or {}
    declared = {str(f.get("name")): f for f in (schema.get("fields") or []) if f.get("name")}
    governance = _declared_governance(record)
    return {
        "schema": [
            {
                "name": field,
                # `Unknown` rather than a type guessed from the first value: one
                # null in row 0 would type a whole column wrong, permanently, in
                # a surface whose job is to be exact.
                "type": str(declared.get(field, {}).get("type") or "Unknown"),
                # The member id the Result was executed for, when the Result
                # carries it. A Visualization Spec binds to CONCEPT IDS, so a lens
                # that dropped this key handed the rendering runtime a projection
                # it could not match the spec against -- and the runtime refused a
                # spec this same server had just validated.
                **(
                    {"id": str(declared[field]["id"])}
                    if isinstance(declared.get(field), dict) and declared[field].get("id")
                    else {}
                ),
                # What the number MEANS, echoed from the frozen plan. `type` above
                # is the storage type of the column; these two are the governed
                # ones, and they are what lets a read divide micros exactly once
                # and attach the currency (amendment 2026-08-15). Absent keys mean
                # the Result never carried them -- never a guess from the name.
                **(governance.get(field) or {}),
            }
            for field in fields
        ],
        "rows": rows,
        # `null`, not `0`. A Result that says "Rows 0 / Cells 0 / Bytes 120" for a
        # query nobody could ask reads as an answer of zero, and the person
        # concludes their campaigns produced nothing (AC9).
        "row_count": _answer_count(record, record.get("row_count")),
        "cell_count": _answer_count(record, record.get("cell_count")),
        "byte_count": _answer_count(record, record.get("byte_count")),
        "truncated": _answer_count(record, bool(record.get("truncated"))),
        "counts_unavailable_reason": _counts_unavailable_reason(record),
        "read_sliced": sliced,
        "read_slice_size": LENS_ROW_SLICE,
        "integrity": {
            "result_content_hash": record.get("content_hash"),
            "query_spec_content_hash": record.get("spec_content_hash"),
            "attempt_id": record.get("attempt_id"),
        },
        "query_context": _query_context(record),
        "manifest": record.get("manifest") or {},
        # AC7: an empty or refused Result keeps an inspectable manifest saying
        # why no rows are shown. That sentence is composed here, from the
        # outcome, rather than left to a screen that might print nothing.
        "no_rows_explanation": (
            None
            if rows
            else (
                str((record.get("manifest") or {}).get("unavailable_reason"))
                if (record.get("manifest") or {}).get("unavailable_reason")
                else "the query ran and nothing matched its filters"
                if record["outcome"] == "empty"
                else "this Result carries no rows"
            )
        ),
    }


def _lens_definitions(conn, record: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Exact immutable references. Never `current` state (AC8)."""
    spec = record.get("spec") or {}
    members = list(spec.get("measures") or []) + list(spec.get("dimensions") or [])
    semantics = _concept_semantics(
        conn, project_id, [str(m.get("version_id")) for m in members if m.get("version_id")]
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.label, v.name, v.version_number, v.status, w.name
              FROM app.semantic_view_versions v
              JOIN app.semantic_views w ON w.id = v.view_id AND w.project_id = v.project_id
             WHERE v.id = %s AND v.project_id = %s
            """,
            (record["semantic_view_version_id"], project_id),
        )
        view = cur.fetchone()

    def _entry(member: dict[str, Any], kind: str) -> dict[str, Any]:
        detail = semantics.get(str(member.get("version_id")), {})
        return {
            "kind": kind,
            "concept_id": member.get("id"),
            "version_id": member.get("version_id"),
            "label": detail.get("label") or detail.get("name"),
            "definition": detail.get("definition"),
            "expression": detail.get("expression"),
            "aggregation": detail.get("aggregation"),
            "additivity_class": detail.get("additivity_class"),
            "non_additive_dimensions": detail.get("non_additive_dimensions"),
            "unit": detail.get("unit"),
            "value_type": detail.get("value_type"),
            "currency_behavior": detail.get("currency_behavior"),
            "time_behavior": detail.get("time_behavior"),
            "allowed_grains": detail.get("allowed_grains") or [],
            "version_number": detail.get("version_number"),
            # The pinned version resolved to no stored concept version. Said
            # plainly, because the alternative -- reading the concept's CURRENT
            # version instead -- is exactly the historical rewrite AC8 forbids.
            "resolved": bool(detail),
            "unresolved_reason": (
                None
                if detail
                else "this exact member version is not readable in this Project; the current "
                "version has NOT been shown in its place"
            ),
            "owner_ref": _owner(
                workspace="governance",
                section="semantic-model",
                object_type="semantic-concept",
                object_id=str(member.get("id") or ""),
                tab="versions",
                version_id=str(member.get("version_id") or "") or None,
            ),
        }

    classification_pins = [
        {
            "member_id": f.get("member_id"),
            "classification_object_id": f.get("classification_object_id"),
            "hierarchy_id": f.get("hierarchy_id"),
            "hierarchy_version_id": f.get("hierarchy_version_id"),
            "owner_ref": _owner(
                workspace="governance",
                section="master-data",
                object_type="master-data-object",
                object_id=str(f.get("classification_object_id") or ""),
                tab="overview",
            ),
        }
        for f in (spec.get("filters") or [])
        if f.get("classification_object_id")
    ]

    return {
        "semantic_view": {
            "id": record["semantic_view_id"],
            "version_id": record["semantic_view_version_id"],
            "label": (view[0] or view[1] or view[4]) if view else None,
            "version_number": view[2] if view else None,
            "status": view[3] if view else None,
            "resolved": view is not None,
            "owner_ref": _owner(
                workspace="governance",
                section="semantic-model",
                object_type="semantic-view",
                object_id=record["semantic_view_id"],
                tab="versions",
                version_id=record["semantic_view_version_id"],
            ),
        },
        "members": [_entry(m, "measure") for m in (spec.get("measures") or [])]
        + [_entry(d, "dimension") for d in (spec.get("dimensions") or [])],
        "classification_pins": classification_pins,
        "source_comparison": _comparison_disclosure(spec),
    }


def _comparison_disclosure(spec: dict[str, Any]) -> dict[str, Any]:
    """What the period comparison of THIS Result actually was.

    THE LENS DESCRIBES WHAT RAN, NEVER WHAT WAS MEANT. Until the 2026-08-17
    repair this returned a sentence -- "the same window immediately before this
    one" -- for a control `build_sql` did not read: the rows covered one window
    and the lens described two. So the meaning is now derived from the windows the
    Spec FROZE and the read actually labelled, and a Result whose Spec carries a
    comparison without frozen windows -- every one written before that repair --
    says so rather than borrowing the new behaviour's description.

    The two controls that used to sit beside it here, `reporting_boundary` and
    `timezone`, are gone from this lens because they are gone from the request:
    both are refused at the door (`query_specs._refuse_unexecuted_time_controls`),
    so there is nothing left to describe.
    """
    requested = str(spec.get("comparison") or "none")
    windows = spec.get("comparison_windows")
    if requested == "none":
        return {"requested": "none", "executed": False, "meaning": "one window, no comparison"}
    if not isinstance(windows, dict):
        return {
            "requested": requested,
            "executed": False,
            "meaning": (
                "This Result was produced before period comparison was executed on this "
                "path. Its rows cover the requested window only; nothing was compared. "
                "Run the question again to obtain the comparison."
            ),
        }
    current = windows.get("current") or {}
    baseline = windows.get("baseline") or {}
    return {
        "requested": requested,
        "executed": True,
        "period_field": windows.get("period_field"),
        "current_window": current,
        "baseline_window": baseline,
        "meaning": (
            f"Each row is labelled `{windows.get('period_field')}`: `current` for "
            f"{current.get('start')} to {current.get('end')}, `baseline` for "
            f"{baseline.get('start')} to {baseline.get('end')}. The two windows are "
            "carried side by side and never summed together."
        ),
    }


#: The analytical path THIS module serves, and the disclosure of the other one.
#:
#: `analyze-and-test.md:424-426` is incomplete while "the same business question
#: can return two different numbers on two Analyze surfaces WITHOUT EITHER
#: DISCLOSING THE OTHER". Until today only one side disclosed: `core.envelope`
#: names this path from the mart side (CAV-17, `3efa5b9`), while a person reading
#: a Result in the Console had no way to learn that a chat answer to the same
#: question may differ. That asymmetry is what this constant closes.
#:
#: It does NOT close the divergence itself. CAV-17 says converging the two paths
#: is architectural work owned by Epic 50 and it is not done: `core.cards` still
#: reads the `fact_daily_kpi` aggregate mart while this path reads the
#: Datastream's published output relation through a Query Spec. Disclosure is the
#: honest state in the meantime, not the repair.
#:
#: The mart's identity is IMPORTED, never retyped -- two hand-written copies of
#: the same relation name is how a disclosure starts describing a path that has
#: since moved.
ANALYTICAL_PATH_GOVERNED = {
    "path": "governed",
    "governed_result": True,
    "note": (
        "This figure comes from a Query Spec executed against a published Semantic "
        "View, and pins its Semantic View version, Query Spec version and output "
        "relation on the Result. The MCP card path reads an aggregate mart instead, "
        "so the two can disagree; neither reconciles against the other."
    ),
}


def _parallel_path_disclosure() -> dict[str, Any]:
    """What the OTHER Analyze surface reads, named from its own module."""
    from core.envelope import ANALYTICAL_PATH_MART  # noqa: PLC0415

    return {
        "this_path": dict(ANALYTICAL_PATH_GOVERNED),
        "other_path": dict(ANALYTICAL_PATH_MART),
        "reconciled": False,
    }


def _lens_quality(conn, record: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Freshness, completeness, DQ evaluations, limitations -- and their owners.

    DQ evidence is REFERENCED, never copied: the evaluations below carry their
    monitor identity and a Controls & Quality owner link, and this module makes
    no judgement about them. Copying the verdict into Analyze would put a second
    DQ authority in a workspace whose contract says it authors none.
    """
    summary = _quality_summary(record)
    manifest = record.get("manifest") or {}
    evaluations: list[dict[str, Any]] = []
    dq_unavailable_reason = None
    # Only evidence the Result itself points at. A "recent evaluations in this
    # Project" list would attach unrelated DQ state to this Result and read as
    # if it described it.
    refs = [str(r) for r in (manifest.get("dq_evaluation_ids") or []) if r]
    if refs:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, monitor_id, monitor_version_id, outcome, window_start, window_end,
                       evaluated_at, total_eligible, evaluated_count, passed_count, failed_count,
                       unavailable_count
                  FROM app.dq_evaluations
                 WHERE project_id = %s AND id = ANY(%s)
                """,
                (project_id, refs),
            )
            for row in cur.fetchall():
                evaluations.append(
                    {
                        "evaluation_id": row[0],
                        "monitor_id": row[1],
                        "monitor_version_id": row[2],
                        "outcome": row[3],
                        "window_start": _iso(row[4]),
                        "window_end": _iso(row[5]),
                        "evaluated_at": _iso(row[6]),
                        "counts": {
                            "total_eligible": row[7],
                            "evaluated": row[8],
                            "passed": row[9],
                            "failed": row[10],
                            "unavailable": row[11],
                        },
                        "owner_ref": _owner(
                            workspace="governance",
                            section="controls-quality",
                            object_type="dq-monitor",
                            object_id=row[1],
                            tab="history",
                        ),
                    }
                )
        # A reference that resolves to nothing is the dangerous case: the list
        # comes back empty and the panel reads "no DQ issue" for a Result that
        # named evidence we could not read. Counted and said, never swallowed.
        unresolved = len(refs) - len(evaluations)
        if unresolved > 0:
            dq_unavailable_reason = (
                f"{unresolved} of {len(refs)} DQ references recorded on this Result did not "
                f"resolve to a DQ evaluation in this Project. They are written by "
                f"{EVIDENCE_OWNER} (Story 50.1) and read here from `app.dq_evaluations` "
                "(Story 49.4); no other evaluation has been shown in their place, and this "
                "is not a statement that quality is healthy."
            )
    else:
        dq_unavailable_reason = str(manifest.get("dq_unavailable_reason") or "").strip() or (
            "this Result's manifest references no DQ evaluation. Story 50.1 writes the "
            "manifest and does not yet record one; no evaluation has been attached in its "
            "place."
        )
    return {
        **summary,
        "dq_evaluations": evaluations,
        "dq_unavailable_reason": dq_unavailable_reason,
        "degraded_reason": manifest.get("degraded_reason"),
        "refused_reason": manifest.get("refused_reason"),
        "unavailable_reason": manifest.get("unavailable_reason"),
        "missing_link": manifest.get("missing_link"),
        "analytical_paths": _parallel_path_disclosure(),
        # CHANTIER B -- the distance between this breakdown and the total its
        # measure declares, snapshotted at execution. Read as it was written and
        # judged nowhere here: Analyze authors no verdict, and this one was
        # authored by `core.metric_grain` from the client's own declaration.
        #
        # Three states, and they must stay three: a LIST means the comparison
        # ran; an EMPTY list means there was nothing to compare (this Result is
        # the total, or the measure declares none); `None` means the comparison
        # itself failed. Collapsing the last two would let a failure read as
        # "nothing to reconcile", which is the shape of every quiet lie this
        # workspace exists to refuse.
        "grain_reconciliation": manifest.get("grain_reconciliation", []),
        "chosen_by": manifest.get("chosen_by"),
        # CHANTIER C -- which dimensions of this Result an inventory of
        # entities-without-detail can be taken for. The INVENTORY itself is not
        # computed here: it costs a DISTINCT over a published relation, and
        # paying that on every Result read would make looking at a figure
        # expensive to punish nobody. This is the cheap half -- one indexed read
        # per Project -- and it decides only whether the question is offerable.
        "entity_gap_candidates": _entity_gap_candidates(conn, record, project_id),
        # CHANTIER 67-15c -- whether the reconciliation gate was asked about each
        # measure of this Result, and what it answered. It rides the QUALITY lens
        # rather than Definitions on purpose: `additivity_class` (already on
        # Definitions) says what a measure MEANS, while "nobody checked that adding
        # these two sources is legitimate" says how far this answer can be trusted.
        # They are neighbours, not the same fact, and the reader asking the second
        # question is the one already looking at freshness and limitations.
        "metric_combination": _metric_combination(conn, record, project_id),
        "semantic_view_version_id": manifest.get("semantic_view_version_id"),
    }


def _metric_combination(conn, record: dict[str, Any], project_id: str) -> list[dict[str, Any]]:
    """Per measure of this Result: WAS THE COMBINATION CHECKED, and was it refused.

    THE DEFECT THIS CLOSES. `analyze-and-test.md:611-621` has carried two open
    clauses since 2026-08-10 -- *"a combined total does not say whether anyone
    checked it"* and *"a refused metric reaches a surface as an absence, with
    nothing naming the refusal"* -- with the owed line spelled out under them: *"no
    `.tsx` reads `metrics_not_combinable` or `combination_check`"*. The server states
    both facts on the mart path (`rollup.compute_rollup` -> `reports.py:351`,
    `cards.py`), and both travel over REST on `GET /api/cards`. Measured 2026-08-17:
    that route has ZERO call sites in `ui/admin/src` -- only `/api/cards/templates`
    is fetched, by the catalog page, which renders no figure. So the one console
    screen that renders real measures, the Result workbench, read a lens envelope
    that never carried the verdict, and a measure the gate would refuse reached it
    as a hole.

    THE SAME AUTHORITY, NOT A SECOND ONE. This asks `rollup.combination_refusal`
    with the resolver `metric_reconciliation.route_status_resolver` hands every other
    call site. Nothing is recomputed here and no rule is restated: this function only
    supplies the two inputs that authority takes -- the metric's governed NAME and
    the connectors this Result actually observed -- and passes its answer through.

    A READ, AND ONLY A READ. The observed sources come from the provenance snapshot
    `query_execution.capture_evidence` wrote at execution time, never from current
    bindings: describing a past execution with today's bindings is the historical
    rewrite AC8 forbids, and it would let a Result change its verdict without being
    re-run.

    WHAT IS DELIBERATELY NOT ANSWERED. When the manifest carries no source tuple for
    a measure, this reports NOTHING and says why. `combination_refusal` would answer
    `single_source` for an empty list -- correct for a caller that counted its own
    rows and saw one connector, a LIE for a caller that could not count at all. "One
    source, nothing to reconcile" and "we could not tell how many sources" are two
    different facts, and only one of them is reassuring.
    """
    spec = record.get("spec") or {}
    measures = list(spec.get("measures") or [])
    if not measures:
        return []

    semantics = _concept_semantics(
        conn, project_id, [str(m.get("version_id")) for m in measures if m.get("version_id")]
    )

    # member id -> the connectors that contributed a value for it, deduplicated and
    # ordered so two reads of one Result cannot disagree.
    observed: dict[str, set[str]] = {}
    provenance = (record.get("manifest") or {}).get("provenance") or {}
    for entry in provenance.get("values") or []:
        if not isinstance(entry, dict):
            continue
        member_id = str(entry.get("member_id") or "")
        source_system = str(entry.get("source_system") or "")
        if member_id and source_system:
            observed.setdefault(member_id, set()).add(source_system)

    from core.metric_reconciliation import route_status_resolver  # noqa: PLC0415
    from core.rollup import combination_refusal  # noqa: PLC0415

    resolver = route_status_resolver(project_id)

    entries: list[dict[str, Any]] = []
    for member in measures:
        member_id = str(member.get("id") or "")
        detail = semantics.get(str(member.get("version_id")), {})
        name = str(detail.get("name") or "")
        sources = sorted(observed.get(member_id, set()))

        # An unresolvable member version has no governed name to route. Asking the
        # gate about a concept id would resolve the reconciliation cascade for a
        # metric that does not exist and return a permission by vacuity.
        if not name:
            check, refused = None, None
            unavailable_reason = (
                "this measure's pinned version is not readable in this Project, so it "
                "carries no governed name to ask the reconciliation gate about. The "
                "current version has NOT been asked about in its place."
            )
        elif not sources:
            check, refused = None, None
            unavailable_reason = (
                "this Result's manifest records no source system for this measure, so the "
                "number of sources behind it is unknown. It has NOT been read as one "
                "source: an unchecked total and a single-source total are different facts."
            )
        else:
            refused, check = combination_refusal(name, sources, resolver)
            unavailable_reason = None

        entries.append(
            {
                "member_id": member_id,
                "member_name": name or None,
                "label": detail.get("label") or detail.get("name"),
                "check": check,
                "refused": refused,
                "source_systems": sources,
                "unavailable_reason": unavailable_reason,
            }
        )
    return entries


def _entity_gap_candidates(conn, record: dict[str, Any], project_id: str) -> list[dict[str, Any]]:
    """The Result's dimensions whose name is an entity kind some event carries.

    THE EQUALITY IS THE CONCEPT'S NAME, the same one that turns a mapping's
    canonical target into a member everywhere else. A Connector writes
    `entity_kind` in its own vocabulary (`video`, `product`, `campaign`) and the
    governed dimension carries that name; comparing anything else would compare
    two sets that were never the same set.

    An unreadable read returns NO candidate rather than raising: the inventory is
    an offer, and a Result must still open when the offer cannot be made.
    """
    # THE NAME IS THE CONCEPT'S, NOT THE COLUMN'S. A Result field carries its
    # member id and its PHYSICAL column name (`shape_result_payload`), and on the
    # reference Project those two happen to read the same -- `video` maps to a
    # field also called `video`. That coincidence is not the contract: the
    # inventory endpoint resolves the concept's own name, so an offer keyed on the
    # column name would offer a dimension the inventory then measures under
    # another name, or refuse one it could have answered. Both halves read the
    # same row of `semantic_concept_versions`.
    members = [
        str(f["id"])
        for f in ((record.get("result_schema") or {}).get("fields") or [])
        if isinstance(f, dict) and f.get("id")
    ]
    if not members:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT entity_kind FROM app.context_events
                WHERE project_id = %s AND entity_kind IS NOT NULL
                """,
                (project_id,),
            )
            kinds = {str(row[0]) for row in cur.fetchall()}
            if not kinds:
                return []
            cur.execute(
                """
                SELECT DISTINCT concept_id, name FROM app.semantic_concept_versions
                WHERE project_id = %s AND concept_id = ANY(%s)
                """,
                (project_id, members),
            )
            named = {str(row[0]): str(row[1]) for row in cur.fetchall()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("analyze_workbench: entity kinds unreadable: %s", exc, exc_info=True)
        return []
    return [
        {"member_id": member_id, "member_name": name}
        for member_id, name in sorted(named.items(), key=lambda pair: pair[1])
        if name in kinds
    ]


def _lens_provenance(record: dict[str, Any]) -> dict[str, Any]:
    """The exact owner chain, link by link, with the gaps NAMED.

    Every link is either `recorded` with its identity, or `not_recorded` with the
    module that owns writing it. There is deliberately no third state -- and a
    link simply MISSING FROM THE LIST is that third state, which is why the six
    `analyze-and-test.md:114` contracts (source, pull or virtual pull, mapping,
    publication, semantic query, result) are all built here, in that order, even
    when the manifest records none of them. A chain that silently carries four
    links reads as a complete chain of four.

    Nothing below is derived from today's bindings. Source, pull and mapping come
    from the snapshot `core.query_execution.capture_evidence` takes at execution
    time; when the execution failed before that snapshot -- a plan that resolved
    to no published output, for instance -- the three links are `not_recorded`
    with that owner named, which is the truth about that Result forever.
    """
    manifest = record.get("manifest") or {}
    provenance = manifest.get("provenance") or {}
    datastream_id = provenance.get("datastream_id")
    # The manifest's own reason for having no evidence, when it has one; otherwise
    # a sentence that names the writer rather than shrugging.
    from core.analyze_render_mcp import project_freshness  # noqa: PLC0415

    not_captured = str(
        manifest.get("unavailable_reason")
        or f"no execution evidence was snapshotted for this Result by {EVIDENCE_OWNER}"
    )

    def link(name: str, present: Any, *, owner: dict | None = None, missing: str) -> dict:
        return {
            "link": name,
            "status": "recorded" if present else "not_recorded",
            "identity": present or None,
            "owner_ref": owner if present else None,
            "reason": None if present else missing,
        }

    def _datastream_owner(tab: str) -> dict | None:
        """The Datastream tab that owns this link, or nothing when unidentified."""
        if not datastream_id:
            return None
        return _owner(
            workspace="data",
            section="datastreams",
            object_type="datastream",
            object_id=str(datastream_id),
            tab=tab,
        )

    chain = [
        link(
            "source",
            datastream_id,
            owner=_datastream_owner("overview"),
            missing=(
                f"this Result records no source Datastream. {not_captured}. It has NOT been "
                f"looked up from current bindings; owner: {EVIDENCE_OWNER}."
            ),
        ),
        link(
            # "pull or virtual pull" in `analyze-and-test.md:114`. The identity is
            # the Datastream execution that materialized the relation read.
            "pull",
            provenance.get("pull_id"),
            owner=_datastream_owner("runs"),
            missing=(
                f"this Result records no pull. {not_captured}. The most recent pull of this "
                f"Datastream has NOT been substituted for the one that was read; owner: "
                f"{EVIDENCE_OWNER}."
            ),
        ),
        link(
            "mapping",
            provenance.get("mapping_version_id"),
            owner=_datastream_owner("mapping"),
            missing=(
                f"this Result records no mapping version. {not_captured}. The Datastream's "
                f"current mapping version has NOT been shown in its place; owner: "
                f"{EVIDENCE_OWNER}."
            ),
        ),
        link(
            "published_output_relation",
            manifest.get("relation") or provenance.get("relation"),
            owner=_datastream_owner("outputs"),
            missing=str(
                manifest.get("unavailable_reason")
                or "this Result names no materialized relation"
            ),
        ),
        link(
            "semantic_view_version",
            record.get("semantic_view_version_id"),
            owner=_owner(
                workspace="governance",
                section="semantic-model",
                object_type="semantic-view",
                object_id=record.get("semantic_view_id"),
                tab="versions",
                version_id=record.get("semantic_view_version_id"),
            ),
            missing="no Semantic View version is recorded on this Result",
        ),
        link(
            "query_spec_version",
            record.get("query_spec_version_id"),
            owner=_owner(
                workspace="analyze",
                section="explore",
                object_type="query-spec",
                object_id=record.get("query_spec_id"),
                tab="query",
                version_id=record.get("query_spec_version_id"),
            ),
            missing="no Query Spec version is recorded on this Result",
        ),
        link(
            "result",
            record.get("id"),
            owner=_owner(
                workspace="analyze",
                section="explore",
                object_type="result",
                object_id=record.get("id"),
                tab="provenance",
            ),
            missing="unreachable: a Result always carries its own identity",
        ),
    ]
    value_source_tuples = [
        {
            "member_id": entry.get("member_id"),
            "source_system": entry.get("source_system"),
            "source_field": entry.get("source_field"),
            "pull_id": entry.get("pull_id"),
        }
        for entry in (provenance.get("values") or [])
    ]
    return {
        "chain": chain,
        "missing_link": manifest.get("missing_link"),
        "predecessor": (
            {
                "result_id": record.get("predecessor_result_id"),
                "owner_ref": _owner(
                    workspace="analyze",
                    section="explore",
                    object_type="result",
                    object_id=record.get("predecessor_result_id"),
                    tab="provenance",
                ),
            }
            if record.get("predecessor_result_id")
            else None
        ),
        "successors": [
            {
                **s,
                "owner_ref": _owner(
                    workspace="analyze",
                    section="explore",
                    object_type="result",
                    object_id=s["result_id"],
                    tab="provenance",
                ),
            }
            for s in (record.get("successors") or [])
        ],
        # AC10 asks for `(source_system, source_field, pull_id)` on every directly
        # sourced value. Story 50.1 now snapshots exactly that at execution time,
        # so it is READ here -- not assembled from current bindings, which is the
        # reason this list was empty when the story first landed.
        "source_system": provenance.get("source_system"),
        "value_source_tuples": value_source_tuples,
        "value_source_unavailable": (
            None
            if value_source_tuples
            else {
                "code": "manifest_carries_no_source_tuple",
                "message": (
                    "this Result's manifest carries no (source_system, source_field, pull_id) "
                    f"tuple. {not_captured}. It has NOT been enriched from current bindings "
                    "here, because a past execution described with present bindings is not "
                    "evidence."
                ),
                "owner": EVIDENCE_OWNER,
            }
        ),
        # THE SAME PROJECTION THE RENDER SURFACE USES, and not a key picked by
        # hand (AI-296). This line read `manifest.get("stale_since")` -- a key at
        # the manifest ROOT that nothing in the repository ever writes, so it was
        # always None and the console's only freshness line never rendered once.
        # A hand-picked key is how the three readers of this one fact came to
        # disagree with the one writer; going through `project_freshness` means
        # the Workbench and the shared Render answer the freshness question with
        # the same words, and a change to the manifest reaches both.
        "freshness": project_freshness(manifest),
    }


def _lens_ai_path(conn, record: dict[str, Any], project_id: str) -> dict[str, Any]:
    """Delegate the Workbench lens to the one bounded observed-path shape."""
    from core.ai_paths import project_observed_ai_path  # noqa: PLC0415

    ai_path = record.get("ai_path_id") or record.get("ai_path_absent_literal")
    return project_observed_ai_path(conn, project_id=project_id, ai_path=ai_path)


def compose_result_lens(
    conn,
    *,
    org_id: str,
    project_id: str,
    result_id: str,
    lens: str,
    include_feedback_context: bool = False,
) -> dict[str, Any]:
    """One lens of one immutable Result. Unknown lens slugs are refused, not repaired."""
    if lens not in LENS_BUILDERS:
        raise UnknownLens(f"`{lens}` is not a Result lens")
    record = load_result_record(conn, org_id=org_id, project_id=project_id, result_id=result_id)
    body = LENS_BUILDERS[lens](conn, record, project_id)
    envelope = {
        "schema_version": LENS_SCHEMA_VERSION,
        "result_id": record["id"],
        "project_id": project_id,
        "lens": lens,
        "outcome": record["outcome"],
        "content_hash": record["content_hash"],
        "query_spec_id": record["query_spec_id"],
        "query_spec_version_id": record["query_spec_version_id"],
        # THE WORDS THE HEADER PRINTS, SERVED RATHER THAN INFERRED. The Result
        # header read `Query Spec version qsv_01KZ… · Semantic View version
        # svv_01KZ…` -- identifiers where a person expects a name and a number,
        # the same defect story 66.8 arbitrated for the Builder rail and the
        # Explorer repaired on its own line. A fallback the browser takes is a
        # name the server did not serve, so the server serves it.
        "query_spec_version_number": record["version_number"],
        "semantic_view_id": record["semantic_view_id"],
        "semantic_view_version_id": record["semantic_view_version_id"],
        # `label` is what a person named the view; `name` is its stable slug.
        # Both are NOT NULL, so this is a word, not a maybe -- see the join above.
        "semantic_view_label": record.get("semantic_view_label")
        or record.get("semantic_view_name"),
        "semantic_view_version_number": record.get("semantic_view_version_number"),
        "started_at": _iso(record.get("started_at")),
        "ended_at": _iso(record.get("ended_at")),
        "lenses": list(LENSES),
        lens.replace("-", "_"): body,
    }
    if include_feedback_context:
        from core.analyze_feedback import (  # noqa: PLC0415
            feedback_fields_from_schema,
            mint_eligible_delivery_feedback_context,
        )
        from core.tracing import current_trace_id_hex  # noqa: PLC0415

        path_steps = body.get("steps", []) if lens == "ai-path" and isinstance(body, dict) else []
        if lens == "view":
            delivered_rows = list(record.get("rows_chunk") or [])[:LENS_ROW_SLICE]
            delivered_fields = list(body.get("fields") or [])
        elif lens == "data":
            delivered_rows = list(body.get("rows") or [])
            delivered_fields = [
                field["name"]
                for field in (body.get("schema") or [])
                if isinstance(field, dict) and isinstance(field.get("name"), str)
            ]
        else:
            delivered_rows = []
            delivered_fields = []
        safe_fields = set(feedback_fields_from_schema(record.get("result_schema")))
        delivered_fields = [field for field in delivered_fields if field in safe_fields]
        feedback_context = mint_eligible_delivery_feedback_context(
            conn,
            org_id=org_id,
            project_id=project_id,
            result_id=result_id,
            result_content_hash=record["content_hash"],
            surface="console",
            stored_rows=list(record.get("rows_chunk") or []),
            delivered_rows=delivered_rows,
            delivered_fields=delivered_fields,
            ai_path_id=record.get("ai_path_id"),
            path_step_ordinals=[step["ordinal"] for step in path_steps if "ordinal" in step],
            w3c_trace_id=current_trace_id_hex(),
        )
        if feedback_context is not None:
            envelope["feedback_context"] = feedback_context
    return envelope


#: Dispatch by declared slug. A dict rather than a chain of `if`s so an
#: unregistered lens cannot fall through into `view` -- AC5 requires it to stay
#: Unknown.
LENS_BUILDERS = {
    "view": lambda conn, record, project_id: _lens_view(conn, record, project_id),
    "data": lambda conn, record, project_id: _lens_data(record),
    "definitions": lambda conn, record, project_id: _lens_definitions(conn, record, project_id),
    "quality": lambda conn, record, project_id: _lens_quality(conn, record, project_id),
    "provenance": lambda conn, record, project_id: _lens_provenance(record),
    "ai-path": lambda conn, record, project_id: _lens_ai_path(conn, record, project_id),
}


def load_query_spec_version(
    conn, *, org_id: str, project_id: str, query_spec_version_id: str
) -> dict[str, Any]:
    """The exact immutable analytical intent behind one version, plus its Results.

    Story 50.1's `GET /query-specs/{id}` lists version identities without their
    specs. The Query workbench tab needs the spec itself, and reading it here
    keeps the browser from reconstructing intent out of the Result manifest.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.id, v.query_spec_id, v.version_number, v.semantic_view_id,
                   v.semantic_view_version_id, v.spec, v.content_hash,
                   v.predecessor_version_id, v.created_at, v.created_by, s.name,
                   s.current_version_id
              FROM app.query_spec_versions v
              JOIN app.query_specs s
                ON s.id = v.query_spec_id AND s.org_id = v.org_id AND s.project_id = v.project_id
             WHERE v.id = %s AND v.org_id = %s AND v.project_id = %s
            """,
            (query_spec_version_id, org_id, project_id),
        )
        row = cur.fetchone()
        if row is None:
            raise WorkbenchNotFound("query spec version not found in this Project")
        cur.execute(
            """
            SELECT id, outcome, row_count, truncated, started_at, ended_at, content_hash
              FROM app.query_results
             WHERE query_spec_version_id = %s AND org_id = %s AND project_id = %s
             ORDER BY started_at DESC
             LIMIT 50
            """,
            (query_spec_version_id, org_id, project_id),
        )
        results = [
            {
                "result_id": r[0],
                "outcome": r[1],
                "row_count": r[2],
                "truncated": r[3],
                "started_at": _iso(r[4]),
                "ended_at": _iso(r[5]),
                "content_hash": r[6],
                "owner_ref": _owner(
                    workspace="analyze",
                    section="explore",
                    object_type="result",
                    object_id=r[0],
                    tab=DEFAULT_LENS,
                ),
            }
            for r in cur.fetchall()
        ]
    return {
        "schema_version": LENS_SCHEMA_VERSION,
        "query_spec_id": row[1],
        "query_spec_version_id": row[0],
        "version_number": row[2],
        "semantic_view_id": row[3],
        "semantic_view_version_id": row[4],
        "spec": row[5] or {},
        "content_hash": row[6],
        "predecessor_version_id": row[7],
        "created_at": _iso(row[8]),
        "created_by": row[9],
        "name": row[10],
        "is_current_version": row[11] == row[0],
        "results": results,
        "semantic_view_owner_ref": _owner(
            workspace="governance",
            section="semantic-model",
            object_type="semantic-view",
            object_id=row[3],
            tab="versions",
            version_id=row[4],
        ),
    }
