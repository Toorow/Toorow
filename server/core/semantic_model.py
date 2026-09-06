"""The Semantic Model application service (Story 49.3).

One authority for Semantic Concepts and Semantic Views. Console and MCP both
call THIS module; neither reaches the tables, and there is no second writer.

The lifecycle is deliberately three calls, not one:

``create`` (a change set with intent and an exact base)
    → ``prepare`` (validate formulas, relationships, mapping and Master Data
      pins, used-by impact, compilation and the Test gate; mint a single-use
      confirmation)
    → ``confirm`` (recompute the dependency fingerprint, refuse if anything
      moved, then commit state + pointers + audit + outbox + idempotency
      atomically).

Splitting it is what makes "the thing you approved is the thing that happened"
checkable. A one-shot publish can only promise it.

What the browser supplies: intent, exact base references, an idempotency key.
What the server owns: the diff, the coverage, the impact, the compiled
artifacts, the version numbers and the final state.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Sequence

from ulid import ULID

from core import business_identity_catalogue as catalogue
from core.audit import declare_action
from core.semantic_compiler import (
    COMPILER_VERSION,
    OSSIE_SPEC_VERSION,
    TOOROW_EXTENSION_VERSION,
    ConceptMember,
    DatasetRef,
    Relationship,
    canonical_json,
    compile_semantic_view,
    content_hash,
    dependency_fingerprint,
)
from core.semantic_expressions import (
    ConceptResolver,
    Refusal,
    validate_aggregation,
    validate_expression,
)

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12). Celles-ci n'etaient declarees NULLE PART : la valeur
# etait retapee en dur ici, parce que la liste centrale de `core/audit.py`
# etait trop loin pour valoir le detour. Mesure ce jour-la sur le journal
# vivant : 29 des 64 actions reellement ecrites -- 45 % -- etaient dans ce
# cas, et rien ne pouvait distinguer une action d'une faute de frappe.
ACTION_SEMANTIC_MODEL_CONFIRM_CHANGE_SET = declare_action("semantic_model.confirm_change_set")
# AI-346 (2026-09-01). Written by `recompile_stale_artifacts` for every artifact it
# re-derives under a moved compiler. Not a publication: no version, no gate.
ACTION_SEMANTIC_MODEL_RECOMPILE_ARTIFACT = declare_action("semantic_model.recompile_artifact")


#: How long a prepared change set stays confirmable. Long enough to read the
#: impact, short enough that the world it was measured against still exists.
CONFIRMATION_TTL = timedelta(minutes=15)

SEMANTIC_POLICY_VERSION = "semantic-model.policy.v1"


class SemanticNotFound(LookupError):
    """The object, version or change set is not in the authorized Project."""


class SemanticRefused(ValueError):
    """A named refusal. Carries the refusals so the caller can show them."""

    def __init__(self, code: str, message: str, refusals: Sequence[Refusal] = ()):
        super().__init__(message)
        self.code = code
        self.refusals = list(refusals)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "refusals": [refusal.as_dict() for refusal in self.refusals],
        }


class SemanticStale(SemanticRefused):
    """The prepared world moved. Confirming would apply an unreviewed change."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mint(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _fetch(conn: Any, query: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(query, params)
        columns = [description[0] for description in cur.description]
        return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

# Platform-scoped Concepts (project_id IS NULL) are shared DEFINITIONS, exactly
# as app.mdm_canonical_fields already shares them. No project data crosses here:
# every binding, coverage row and View that references one is Project-scoped.
_CONCEPTS = """
    SELECT c.id, c.project_id, c.kind, c.name, c.lifecycle_status,
           c.current_version_id, c.pending_version_id, c.last_known_good_version_id,
           c.created_by, c.created_at, c.updated_at,
           v.id AS version_id, v.version_number, v.status AS version_status,
           v.label, v.definition, v.value_type, v.unit, v.format, v.owner,
           v.business_domain_refs, v.master_data_refs,
           v.expression, v.aggregation, v.additivity_class, v.non_additive_dimensions,
           v.currency_behavior, v.time_behavior, v.display,
           v.semantic_type, v.allowed_grains, v.hierarchies, v.value_domain,
           v.conformance, v.time_semantics, v.provenance, v.content_hash,
           v.created_at AS version_created_at,
           vc.version_count,
           uv.used_by_view_count
    FROM app.semantic_concepts c
    LEFT JOIN app.semantic_concept_versions v ON v.id = c.current_version_id
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS version_count
        FROM app.semantic_concept_versions cv WHERE cv.concept_id = c.id
    ) vc ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(DISTINCT vv.view_id) AS used_by_view_count
        FROM app.semantic_view_version_concepts vvc
        JOIN app.semantic_view_versions vv ON vv.id = vvc.view_version_id
        WHERE vvc.concept_id = c.id AND vv.project_id = %(project_id)s
    ) uv ON TRUE
    WHERE c.project_id = %(project_id)s OR c.project_id IS NULL
    ORDER BY c.name, c.id
"""

_CONCEPT_VERSIONS = """
    SELECT v.id, v.concept_id, v.version_number, v.status, v.kind, v.name, v.label,
           v.definition, v.value_type, v.unit, v.format, v.owner,
           v.business_domain_refs, v.master_data_refs, v.expression, v.aggregation,
           v.additivity_class, v.non_additive_dimensions, v.currency_behavior,
           v.time_behavior, v.display, v.semantic_type, v.allowed_grains,
           v.hierarchies, v.value_domain, v.conformance, v.time_semantics,
           v.provenance, v.content_hash, v.created_by, v.created_at
    FROM app.semantic_concept_versions v
    JOIN app.semantic_concepts c ON c.id = v.concept_id
    WHERE v.concept_id = %(concept_id)s
      AND (c.project_id = %(project_id)s OR c.project_id IS NULL)
    ORDER BY v.version_number DESC
"""

_VIEWS = """
    SELECT s.id, s.project_id, s.name, s.lifecycle_status,
           s.current_version_id, s.pending_version_id, s.last_known_good_version_id,
           s.created_by, s.created_at, s.updated_at,
           v.id AS version_id, v.version_number, v.status AS version_status,
           v.label, v.description, v.business_scope, v.business_domain_refs,
           v.master_data_refs, v.query_policy, v.evidence_refs,
           v.dependency_fingerprint, v.content_hash, v.created_at AS version_created_at,
           vc.version_count, mc.metric_count, dc.dimension_count, ds.datastream_count,
           ca.queryability_matrix, ca.compiler_version, ca.ossie_spec_version
    FROM app.semantic_views s
    LEFT JOIN app.semantic_view_versions v ON v.id = s.current_version_id
    LEFT JOIN LATERAL (
        SELECT COUNT(*) AS version_count
        FROM app.semantic_view_versions sv WHERE sv.view_id = s.id
    ) vc ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) FILTER (WHERE role = 'metric') AS metric_count
        FROM app.semantic_view_version_concepts WHERE view_version_id = v.id
    ) mc ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(*) FILTER (WHERE role = 'dimension') AS dimension_count
        FROM app.semantic_view_version_concepts WHERE view_version_id = v.id
    ) dc ON TRUE
    LEFT JOIN LATERAL (
        SELECT COUNT(DISTINCT datastream_id) AS datastream_count
        FROM app.semantic_view_version_bindings WHERE view_version_id = v.id
    ) ds ON TRUE
    LEFT JOIN LATERAL (
        SELECT a.queryability_matrix, a.compiler_version, a.ossie_spec_version
        FROM app.semantic_compiled_artifacts a
        WHERE a.view_version_id = v.id
        ORDER BY a.created_at DESC LIMIT 1
    ) ca ON TRUE
    WHERE s.project_id = %(project_id)s
    ORDER BY s.name, s.id
"""

_VIEW_VERSIONS = """
    SELECT v.id, v.view_id, v.version_number, v.status, v.name, v.label,
           v.description, v.business_scope, v.business_domain_refs,
           v.master_data_refs, v.query_policy, v.evidence_refs,
           v.dependency_fingerprint, v.content_hash, v.created_by, v.created_at
    FROM app.semantic_view_versions v
    JOIN app.semantic_views s ON s.id = v.view_id
    WHERE v.view_id = %(view_id)s AND s.project_id = %(project_id)s
    ORDER BY v.version_number DESC
"""

_VIEW_MEMBERS = """
    SELECT vc.ordinal, vc.concept_id, vc.concept_version_id, vc.role,
           v.name, v.label, v.version_number, v.value_type, v.unit,
           v.currency_behavior,
           v.aggregation, v.additivity_class, v.non_additive_dimensions,
           v.expression, v.semantic_type, v.allowed_grains, v.time_semantics
    FROM app.semantic_view_version_concepts vc
    JOIN app.semantic_concept_versions v ON v.id = vc.concept_version_id
    WHERE vc.view_version_id = %(view_version_id)s
    ORDER BY vc.ordinal
"""

_VIEW_RELATIONSHIPS = """
    SELECT ordinal, name, from_dataset, to_dataset, from_columns, to_columns,
           cardinality_type, bridge_dataset, fan_out_policy,
           mdm_common_key_version_id, left_datastream_id, right_datastream_id
    FROM app.semantic_view_version_relationships
    WHERE view_version_id = %(view_version_id)s
    ORDER BY ordinal
"""

_VIEW_BINDINGS = """
    SELECT b.ordinal, b.concept_id, b.datastream_id, b.mapping_version_id,
           b.output_ref, b.binding_state, b.confidence,
           d.name AS datastream_name,
           mv.version_number AS mapping_version_number
    FROM app.semantic_view_version_bindings b
    LEFT JOIN app.datastreams d
      ON d.id = b.datastream_id AND d.project_id = b.project_id
    LEFT JOIN app.datastream_mapping_versions mv ON mv.id = b.mapping_version_id
    WHERE b.view_version_id = %(view_version_id)s
    ORDER BY b.ordinal
"""


def load_concepts(conn: Any, project_id: str) -> list[dict[str, Any]]:
    rows = _fetch(conn, _CONCEPTS, {"project_id": project_id})
    for row in rows:
        for key in (
            "business_domain_refs",
            "master_data_refs",
            "expression",
            "aggregation",
            "currency_behavior",
            "time_behavior",
            "display",
            "hierarchies",
            "value_domain",
            "time_semantics",
            "provenance",
        ):
            row[key] = _json(row.get(key))
    return rows


def load_concept(conn: Any, project_id: str, concept_id: str) -> dict[str, Any]:
    for row in load_concepts(conn, project_id):
        if str(row["id"]) == concept_id:
            return row
    raise SemanticNotFound(concept_id)


def load_concept_versions(
    conn: Any, project_id: str, concept_id: str
) -> list[dict[str, Any]]:
    rows = _fetch(
        conn, _CONCEPT_VERSIONS, {"project_id": project_id, "concept_id": concept_id}
    )
    for row in rows:
        for key in (
            "business_domain_refs",
            "master_data_refs",
            "expression",
            "aggregation",
            "currency_behavior",
            "time_behavior",
            "display",
            "hierarchies",
            "value_domain",
            "time_semantics",
            "provenance",
        ):
            row[key] = _json(row.get(key))
    return rows


def load_views(conn: Any, project_id: str) -> list[dict[str, Any]]:
    rows = _fetch(conn, _VIEWS, {"project_id": project_id})
    for row in rows:
        for key in (
            "business_domain_refs",
            "master_data_refs",
            "query_policy",
            "evidence_refs",
            "queryability_matrix",
        ):
            row[key] = _json(row.get(key))
    return rows


def load_view(conn: Any, project_id: str, view_id: str) -> dict[str, Any]:
    for row in load_views(conn, project_id):
        if str(row["id"]) == view_id:
            return row
    raise SemanticNotFound(view_id)


def load_view_versions(conn: Any, project_id: str, view_id: str) -> list[dict[str, Any]]:
    rows = _fetch(conn, _VIEW_VERSIONS, {"project_id": project_id, "view_id": view_id})
    for row in rows:
        for key in (
            "business_domain_refs",
            "master_data_refs",
            "query_policy",
            "evidence_refs",
        ):
            row[key] = _json(row.get(key))
    return rows


def load_view_members(conn: Any, view_version_id: str) -> list[dict[str, Any]]:
    rows = _fetch(conn, _VIEW_MEMBERS, {"view_version_id": view_version_id})
    for row in rows:
        for key in ("currency_behavior", "aggregation", "expression", "time_semantics"):
            row[key] = _json(row.get(key))
    return rows


def load_view_relationships(conn: Any, view_version_id: str) -> list[dict[str, Any]]:
    return _fetch(conn, _VIEW_RELATIONSHIPS, {"view_version_id": view_version_id})


def load_view_bindings(conn: Any, view_version_id: str) -> list[dict[str, Any]]:
    rows = _fetch(conn, _VIEW_BINDINGS, {"view_version_id": view_version_id})
    for row in rows:
        row["output_ref"] = _json(row.get("output_ref"))
    return rows


def concept_resolver(conn: Any, project_id: str) -> ConceptResolver:
    """Every Concept version readable in this Project, keyed by the EXACT pair.

    Built from the authorized snapshot, so a formula can never reference a
    version of another Project by guessing an id: the resolver simply does not
    contain it, and the reference is refused as unknown.
    """
    rows = _fetch(
        conn,
        """
        SELECT v.id, v.concept_id, v.value_type, v.unit, v.currency_behavior,
               v.kind, v.name, v.status
        FROM app.semantic_concept_versions v
        JOIN app.semantic_concepts c ON c.id = v.concept_id
        WHERE c.project_id = %(project_id)s OR c.project_id IS NULL
        """,
        {"project_id": project_id},
    )
    return ConceptResolver(
        {
            (str(row["concept_id"]), str(row["id"])): {
                "value_type": row.get("value_type"),
                "unit": row.get("unit"),
                "currency_behavior": _json(row.get("currency_behavior")) or {},
                "kind": row.get("kind"),
                "name": row.get("name"),
                "status": row.get("status"),
            }
            for row in rows
        }
    )


def concept_names(conn: Any, project_id: str) -> dict[str, str]:
    """Concept id -> machine name, for retargeting name-based mapping bindings."""
    return {
        str(row["id"]): str(row["name"])
        for row in _fetch(
            conn,
            """
            SELECT id, name FROM app.semantic_concepts
            WHERE project_id = %(project_id)s OR project_id IS NULL
            """,
            {"project_id": project_id},
        )
    }


# ---------------------------------------------------------------------------
# Change sets
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChangeSet:
    id: str
    project_id: str
    object_type: str
    object_id: str | None
    base_version_id: str | None
    intent: dict[str, Any]
    diff: dict[str, Any]
    dependency_fingerprint: str | None
    validation: dict[str, Any]
    test_gate_state: str
    state: str
    expires_at: str | None
    result_version_id: str | None
    created_by: str
    created_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "change_set_id": self.id,
            "project_ref": {"object_type": "project", "id": self.project_id},
            "object_type": self.object_type,
            "object_id": self.object_id,
            "base_version_id": self.base_version_id,
            "intent": self.intent,
            "diff": self.diff,
            "dependency_fingerprint": self.dependency_fingerprint,
            "validation": self.validation,
            "test_gate_state": self.test_gate_state,
            "state": self.state,
            "expires_at": self.expires_at,
            "result_version_id": self.result_version_id,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "policy_version": SEMANTIC_POLICY_VERSION,
        }


_SUPPORTED_INTENTS = frozenset(
    {"create_concept", "edit_concept", "create_view", "edit_view", "archive_object"}
)


def _load_change_set_row(conn: Any, project_id: str, change_set_id: str) -> dict[str, Any]:
    rows = _fetch(
        conn,
        """
        SELECT * FROM app.semantic_change_sets
        WHERE id = %(id)s AND project_id = %(project_id)s
        """,
        {"id": change_set_id, "project_id": project_id},
    )
    if not rows:
        raise SemanticNotFound(change_set_id)
    row = rows[0]
    for key in ("intent", "diff", "validation", "test_gate_override"):
        row[key] = _json(row.get(key))
    return row


def _as_change_set(row: Mapping[str, Any]) -> ChangeSet:
    return ChangeSet(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        object_type=str(row["object_type"]),
        object_id=row.get("object_id"),
        base_version_id=row.get("base_version_id"),
        intent=row.get("intent") or {},
        diff=row.get("diff") or {},
        dependency_fingerprint=row.get("dependency_fingerprint"),
        validation=row.get("validation") or {},
        test_gate_state=str(row.get("test_gate_state") or "unevaluated"),
        state=str(row.get("state") or "open"),
        expires_at=_iso(row.get("expires_at")),
        result_version_id=row.get("result_version_id"),
        created_by=str(row.get("created_by") or ""),
        created_at=_iso(row.get("created_at")),
    )


def get_change_set(conn: Any, project_id: str, change_set_id: str) -> ChangeSet:
    return _as_change_set(_load_change_set_row(conn, project_id, change_set_id))


def create_change_set(
    conn: Any,
    project_id: str,
    *,
    actor: str,
    object_type: str,
    object_id: str | None,
    base_version_id: str | None,
    intent: Mapping[str, Any],
    idempotency_key: str,
) -> ChangeSet:
    """Open an editable change set from an EXACT base.

    Editing "the current version" is refused: between reading the screen and
    submitting it, current may have moved, and the edit would silently rebase
    onto something the author never saw.
    """
    if object_type not in {"semantic-concept", "semantic-view"}:
        raise SemanticRefused("unknown_object_type", f"{object_type!r} is not a governed type.")
    action = str(intent.get("action") or "")
    if action not in _SUPPORTED_INTENTS:
        raise SemanticRefused(
            "unknown_intent",
            f"{action!r} is not one of {sorted(_SUPPORTED_INTENTS)}.",
        )
    if not idempotency_key:
        raise SemanticRefused(
            "missing_idempotency_key",
            "A consequential command carries an idempotency key so a retried "
            "request cannot create a second change set.",
        )
    if action.startswith("edit") or action == "archive_object":
        if not object_id or not base_version_id:
            raise SemanticRefused(
                "missing_exact_base",
                "Editing begins from an exact object and an exact base version. "
                "Without one, the edit rebases onto whatever became current.",
            )
        _assert_base_belongs(conn, project_id, object_type, object_id, base_version_id)

    # An action names WHAT is changing; the payload under `concept` or `view`
    # carries it. Refusing here, at the earliest moment, is the whole point:
    # every reader downstream does `intent.get("view") or {}` (:959, :1582 and
    # five more), so an intent that puts its fields at the top level resolves to
    # an EMPTY payload and fails far away from its cause — `_apply_view` would
    # write `str(payload.get("name"))`, which is the string "None".
    #
    # Measured on 2026-08-03: this is not hypothetical. Both console creation
    # dialogs send the flat shape today, and the failure is invisible from the
    # screen — `prepare` is called with `.catch(() => null)`, so its refusal is
    # swallowed, `confirm` is skipped for want of a token, and the dialog then
    # announces that the object was created. A change set exists; nothing is
    # published. A named refusal at creation makes that impossible to ship.
    if action != "archive_object":
        expected = "concept" if action.endswith("_concept") else "view"
        body = intent.get(expected)
        if not isinstance(body, Mapping) or not body:
            raise SemanticRefused(
                "missing_intent_payload",
                f"An intent for {action!r} carries its fields under "
                f"`intent.{expected}`. This one has none, and every reader "
                f"downstream would resolve it to an empty payload — the object "
                f"would be written with absent values rather than refused.",
            )

    key_hash = _sha256(f"{project_id}:{actor}:{idempotency_key}")
    existing = _fetch(
        conn,
        """
        SELECT * FROM app.semantic_change_sets
        WHERE project_id = %(project_id)s AND idempotency_key_hash = %(hash)s
        """,
        {"project_id": project_id, "hash": key_hash},
    )
    if existing:
        row = existing[0]
        for key in ("intent", "diff", "validation", "test_gate_override"):
            row[key] = _json(row.get(key))
        return _as_change_set(row)

    change_set_id = _mint("scs")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_change_sets
                (id, project_id, object_type, object_id, base_version_id, intent,
                 idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            """,
            (
                change_set_id,
                project_id,
                object_type,
                object_id,
                base_version_id,
                canonical_json(dict(intent)),
                key_hash,
                actor,
            ),
        )
    return get_change_set(conn, project_id, change_set_id)


def _assert_base_belongs(
    conn: Any, project_id: str, object_type: str, object_id: str, base_version_id: str
) -> None:
    """A base version of another object, or of another Project, is not found —
    never silently swapped for this object's current version."""
    if object_type == "semantic-concept":
        rows = _fetch(
            conn,
            """
            SELECT v.id FROM app.semantic_concept_versions v
            JOIN app.semantic_concepts c ON c.id = v.concept_id
            WHERE v.id = %(version_id)s AND v.concept_id = %(object_id)s
              AND (c.project_id = %(project_id)s OR c.project_id IS NULL)
            """,
            {
                "version_id": base_version_id,
                "object_id": object_id,
                "project_id": project_id,
            },
        )
    else:
        rows = _fetch(
            conn,
            """
            SELECT v.id FROM app.semantic_view_versions v
            JOIN app.semantic_views s ON s.id = v.view_id
            WHERE v.id = %(version_id)s AND v.view_id = %(object_id)s
              AND s.project_id = %(project_id)s
            """,
            {
                "version_id": base_version_id,
                "object_id": object_id,
                "project_id": project_id,
            },
        )
    if not rows:
        raise SemanticNotFound(base_version_id)


# ---------------------------------------------------------------------------
# Prepare
# ---------------------------------------------------------------------------


def _bound_datastreams(conn: Any, project_id: str) -> dict[str, str]:
    """The Datastream that carries each Concept, read from the bindings.

    THE DATASET WAS NEVER WRITTEN WHERE THE COMPILER LOOKS FOR IT.
    `semantic_view_version_concepts` stores `(view_version_id, ordinal,
    concept_id, concept_version_id, role)` and no dataset column, so
    `entry.get("dataset")` answered None for every member of every stored View
    and the compiler compared two empty strings -- which it read as "same
    dataset", and therefore as a proven join. Measured 2026-08-19 on the one
    production View: 22 pairs, all `queryable: true`, four of them impossible.

    The information exists and is written on the other side:
    `semantic_view_version_bindings` binds each Concept to the DATASTREAM that
    produces it, one row per binding, 63 of them for that Project. Reading it
    here is not a new source of truth -- it is the one the platform already
    keeps, finally reaching the compiler.

    ONE CONCEPT, ONE DATASET, and where a Concept is bound to several the answer
    is deliberately the SMALLEST id rather than an arbitrary one: two
    compilations of the same View must not disagree because a row came back in
    another order. A Concept bound nowhere is absent from this map, keeps an
    empty dataset, and every pair it takes part in is refused by name.
    """
    bound: dict[str, str] = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT concept_id, MIN(datastream_id)
                FROM app.semantic_view_version_bindings
                WHERE project_id = %s AND datastream_id IS NOT NULL
                GROUP BY concept_id
                """,
                (project_id,),
            )
            bound = {str(row[0]): str(row[1]) for row in cur.fetchall()}
    except Exception:  # noqa: BLE001 -- an unreadable binding table is an absent dataset
        return {}
    return bound


def _concept_kinds(conn: Any, concept_ids: Sequence[str]) -> dict[str, tuple[str, str]]:
    """`(kind, word)` of each Concept head, looked up once for a member list (AI-346).

    The head row carries the kind; a version repeats it, but the head is what
    every View member joins on and what the compiler reads. One query, not one
    per member.

    THE WORD IS READ HERE BECAUSE THE REFUSAL NEEDS IT. The first version of this
    lookup returned the kind alone, and the two refusals it feeds printed the
    `smc_<ULID>` the caller had sent -- which
    `tests/core/test_refusals_never_name_an_identifier.py` caught the same day.
    A person does not know that string; they know `sessions`. The head's `name`
    is the identity everything joins on and the word the prepare door already
    refuses with (`_members_for_compilation` calls it `head_name`), so the write
    door now says the same thing. `label` is deliberately not read: it lives on
    the VERSION, and a write-time refusal must not depend on which version the
    caller happened to pin.
    """
    wanted = sorted({str(c) for c in concept_ids if c})
    if not wanted:
        return {}
    return {
        str(row["id"]): (str(row["kind"]), str(row.get("name") or ""))
        for row in _fetch(
            conn,
            "SELECT id, kind, name FROM app.semantic_concepts WHERE id = ANY(%(ids)s)",
            {"ids": wanted},
        )
    }


def _members_for_compilation(
    conn: Any, project_id: str, selection: Sequence[Mapping[str, Any]]
) -> tuple[list[ConceptMember], list[Refusal]]:
    """Resolve an intent's Concept selection to exact, readable versions."""
    refusals: list[Refusal] = []
    members: list[ConceptMember] = []
    #: Read only when a refusal needs a word. The happy path resolves every
    #: member without ever naming one, and this read is project-wide.
    head_words: dict[str, str] | None = None
    #  The caller's own `dataset` still wins when it names one -- a View that
    #  declares where a Concept lives is more precise than a binding, and the
    #  compiler's tests pass it directly.
    bound_datasets = _bound_datastreams(conn, project_id)
    for index, entry in enumerate(selection):
        concept_id = str(entry.get("concept_id") or "")
        version_id = str(entry.get("concept_version_id") or "")
        if not concept_id or not version_id:
            refusals.append(
                Refusal(
                    "missing_exact_version",
                    "Every Concept in a Semantic View is pinned to an exact version. "
                    "`latest` is not a version.",
                    f"$.concepts[{index}]",
                )
            )
            continue
        rows = _fetch(
            conn,
            """
            SELECT v.*, c.project_id AS concept_project_id,
                   c.name AS concept_head_name
            FROM app.semantic_concept_versions v
            JOIN app.semantic_concepts c ON c.id = v.concept_id
            WHERE v.id = %(version_id)s AND v.concept_id = %(concept_id)s
              AND (c.project_id = %(project_id)s OR c.project_id IS NULL)
            """,
            {
                "version_id": version_id,
                "concept_id": concept_id,
                "project_id": project_id,
            },
        )
        if not rows:
            # THE SENTENCE NAMES THE CONCEPT AND THE GESTURE, NEVER THE `smv_`.
            # Exact twin of the two write-door refusals repaired in `_apply_view`
            # (AI-346, 4b034bc0): an identifier is the one thing the reader
            # already holds and cannot look up. The difference here is that the
            # HEAD can still be readable when the VERSION is not -- so the word
            # is served whenever the head answers, and only a head nobody can
            # read falls back to naming the gesture alone.
            if head_words is None:
                head_words = concept_names(conn, project_id)
            word = head_words.get(concept_id) or ""
            refusals.append(
                Refusal(
                    "unknown_reference",
                    (
                        f"The version this Semantic View pins for `{word}` is not "
                        "readable in this Project. Open the Semantic Model and pin "
                        "a published version of that Concept."
                        if word
                        else "This Semantic View pins a Concept version that is not "
                        "readable in this Project. Open the Semantic Model and pick "
                        "the Concept again, or publish the one it means."
                    ),
                    f"$.concepts[{index}]",
                )
            )
            continue
        row = rows[0]
        if str(row.get("status")) != "published":
            refusals.append(
                Refusal(
                    "unpublished_member",
                    f"{row.get('label')} is {row.get('status')}, not published. A "
                    "published Semantic View cannot pin a draft.",
                    f"$.concepts[{index}]",
                )
            )
            continue
        # 27.8 -- THE NAME COMES FROM THE CONCEPT HEAD, NOT FROM THE VERSION.
        # `semantic_concepts.name` carries the machine-name check
        # (`^[a-z][a-z0-9_]{0,126}$`) and the uniqueness index of its scope, and
        # `_apply_concept` writes it once, at creation, and never again.
        # `semantic_concept_versions.name` is free text that a later version can
        # set to anything. The compiled matrix publishes `name` so a validator can
        # ask WHAT a dimension is; reading the mutable half would let an edit move
        # a dimension in or out of the language family without anyone deciding it.
        head_name = str(row.get("concept_head_name") or row["name"])
        if str(row["name"]) != head_name:
            refusals.append(
                Refusal(
                    "concept_identity_drift",
                    f"This version calls the Concept `{row['name']}` while the Concept "
                    f"itself is `{head_name}`. The label is what a client renames; the "
                    "name is the identity everything else joins on. Correct the version "
                    "so both say the same thing.",
                    f"$.concepts[{index}]",
                )
            )
            continue
        # AI-346 -- THE ROLE IS THE CONCEPT'S KIND. A payload may repeat it, and
        # then it must agree; it may omit it, and then the kind is what gets
        # stored. It may never contradict it: measured 2026-09-01 on the
        # reference Project, 13 stored members all said `metric`, dimensions
        # included, because the writer took the caller's word.
        claimed_role = str(entry.get("role") or "")
        if claimed_role and claimed_role != str(row["kind"]):
            refusals.append(
                Refusal(
                    "member_role_mismatch",
                    f"{row.get('label') or head_name} is a {row['kind']}, and this View "
                    f"lists it as a {claimed_role}. A member's role is the Concept's "
                    "kind; leave it out, or say what the Concept says.",
                    f"$.concepts[{index}].role",
                )
            )
            continue
        time_semantics = _json(row.get("time_semantics")) or {}
        members.append(
            ConceptMember(
                concept_id=concept_id,
                version_id=version_id,
                name=head_name,
                label=str(row.get("label") or head_name),
                role=str(row["kind"]),
                dataset=str(entry.get("dataset") or bound_datasets.get(concept_id) or ""),
                value_type=str(row.get("value_type") or "decimal"),
                unit=row.get("unit"),
                currency=(_json(row.get("currency_behavior")) or {}).get("scope"),
                timezone=time_semantics.get("timezone"),
                grain=time_semantics.get("grain"),
                aggregation=_json(row.get("aggregation")),
                additivity_class=row.get("additivity_class"),
                non_additive_dimensions=tuple(row.get("non_additive_dimensions") or ()),
                expression=_json(row.get("expression")),
                semantic_type=row.get("semantic_type"),
            )
        )
    return members, refusals


@dataclass(frozen=True, slots=True)
class _CompilationInputs:
    """What `compile_semantic_view` is handed for one View payload.

    Built by ONE helper for the two callers that compile -- `prepare_change_set`
    at publication and `recompile_stale_artifacts` when the compiler moved
    (AI-346) -- so the two cannot drift into compiling the same version from
    different inputs.
    """

    members: list[ConceptMember]
    refusals: list[Refusal]
    relationships: list[Relationship]
    datasets: list[DatasetRef]


def _view_compilation_inputs(
    conn: Any, project_id: str, payload: Mapping[str, Any]
) -> _CompilationInputs:
    members, member_refusals = _members_for_compilation(
        conn, project_id, payload.get("concepts") or ()
    )
    relationships = [
        Relationship(
            name=str(entry.get("name") or ""),
            from_dataset=str(entry.get("from_dataset") or ""),
            to_dataset=str(entry.get("to_dataset") or ""),
            from_columns=tuple(entry.get("from_columns") or ()),
            to_columns=tuple(entry.get("to_columns") or ()),
            cardinality_type=str(entry.get("cardinality_type") or "many_to_one"),
            fan_out_policy=str(entry.get("fan_out_policy") or "forbid"),
            bridge_dataset=entry.get("bridge_dataset"),
        )
        for entry in (payload.get("relationships") or ())
        if isinstance(entry, Mapping)
    ]
    datasets = [
        DatasetRef(
            name=str(entry.get("name") or ""),
            source=str(entry.get("source") or ""),
            primary_key=tuple(entry.get("primary_key") or ()),
            unique_keys=tuple(tuple(key) for key in (entry.get("unique_keys") or ())),
        )
        for entry in (payload.get("datasets") or ())
        if isinstance(entry, Mapping)
    ]
    return _CompilationInputs(
        members=members,
        refusals=list(member_refusals),
        relationships=relationships,
        datasets=datasets,
    )


def _compile_view_payload(
    payload: Mapping[str, Any], inputs: _CompilationInputs, resolver: ConceptResolver
):
    """The one `compile_semantic_view` call publication and recompilation share."""
    return compile_semantic_view(
        view_name=str(payload.get("name") or ""),
        view_label=str(payload.get("label") or payload.get("name") or ""),
        description=payload.get("description"),
        concepts=inputs.members,
        relationships=inputs.relationships,
        datasets=inputs.datasets,
        resolver=resolver,
        dbt_relation_refs=payload.get("dbt_relation_refs") or (),
        query_policy=payload.get("query_policy") or {},
    )


def _active_mapping_versions(conn: Any, project_id: str) -> dict[str, str]:
    return {
        str(row["id"]): str(row["current_mapping_version_id"])
        for row in _fetch(
            conn,
            """
            SELECT id, current_mapping_version_id FROM app.datastreams
            WHERE project_id = %(project_id)s AND current_mapping_version_id IS NOT NULL
            """,
            {"project_id": project_id},
        )
    }


def _datastream_words(conn: Any, project_id: str) -> dict[str, str]:
    """Datastream id -> the word a person knows it by, for refusal sentences.

    `app.datastreams.name` is `NOT NULL` and unique inside a Project
    (`uq_datastreams_project_name`, migration 023), so it is an identity a
    reader can act on -- unlike the `ds_<ULID>` two binding refusals printed.
    Deliberately NOT folded into `_active_mapping_versions`: that map answers
    « which version does Data publish », several call sites read it as such, and
    a refusal that needs a word must not change what a check means.
    """
    return {
        str(row["id"]): str(row["name"] or "")
        for row in _fetch(
            conn,
            "SELECT id, name FROM app.datastreams WHERE project_id = %(project_id)s",
            {"project_id": project_id},
        )
    }


def _bindings_naming_an_absent_dimension(
    conn: Any,
    project_id: str,
    bindings: Sequence[Any],
    active: Mapping[str, str],
) -> list[Refusal]:
    """AI-342 -- refuse to publish a dimension the bound relation does not carry.

    THE DEFECT, MEASURED. A View pins each Concept to a Datastream, and the
    Datastream publishes ONE relation. Nothing here asked whether that relation
    carries the Concept, so the reference Project published ELEVEN dimensions
    over relations materialising TWO, and « views by country » ran and answered
    zero rows -- an empty answer where the truth was "this source publishes no
    country split". A published View is a PROMISE about what can be asked of it;
    a promise nothing checks is a promise the reader discovers is false.

    THE CLASS, NOT THE INSTANCE. This names no Connector and no dimension. It
    holds for every View, every Datastream and every breakdown landing: the
    Concept's own name is what the mapping's `canonical_target` says and what the
    landing writes into its key column, and that one equality is the whole test.

    THREE THINGS IT DELIBERATELY DOES NOT REFUSE, each because the alternative
    would refuse something true:

    * a Datastream that has never run. It publishes no Output version, so there
      is no relation to ask, and binding before the first run is the ordinary
      order of the product;
    * a Concept the mapping does not name at all. That is a different gap with a
      different repair (map the field), and `resolve_physical_plan` already names
      it at read time; inventing a physical field to test here would refuse
      Views over a fault this function cannot see;
    * a relation whose shape could not be read. « We could not look » is not
      « nothing is there », and a warehouse outage must not retract a governed
      publication.

    A MEASURE IS CHECKED TOO, since 2026-09-02 (AI-352, governance.md). The
    first version of this guard scoped measures out to spare a second DISTINCT
    per publication; the arbitration pays it: publication is rare, reads are
    many, and the sentence above -- a published View is a PROMISE -- does not
    distinguish the kind of the member the promise is about. The same three
    deliberate non-refusals hold for measures, for the same reasons.
    """
    from core import relation_shape  # noqa: PLC0415

    entries = [b for b in bindings if isinstance(b, Mapping) and b.get("concept_id")]
    if not entries:
        return []
    kinds = _concept_kinds(conn, [str(b.get("concept_id")) for b in entries])
    names = concept_names(conn, project_id)
    shapes: dict[str, Any] = {}
    payloads: dict[str, dict[str, str]] = {}
    refusals: list[Refusal] = []
    for index, binding in enumerate(bindings):
        if not isinstance(binding, Mapping):
            continue
        concept_id = str(binding.get("concept_id") or "")
        kind = (kinds.get(concept_id) or ("", ""))[0]
        if kind not in ("dimension", "metric"):
            continue
        name = names.get(concept_id) or ""
        datastream_id = str(binding.get("datastream_id") or "")
        mapping_version_id = str(
            binding.get("mapping_version_id") or active.get(datastream_id) or ""
        )
        if not name or not datastream_id or not mapping_version_id:
            continue
        if mapping_version_id not in payloads:
            payloads[mapping_version_id] = _mapped_physical_fields(
                conn, project_id, mapping_version_id
            )
        physical = payloads[mapping_version_id].get(name)
        if not physical:
            continue
        relation = _published_relation(conn, project_id, datastream_id, mapping_version_id)
        if not relation:
            continue
        if relation not in shapes:
            shapes[relation] = relation_shape.read(
                project_id, relation, with_metric_keys=True
            )
        if kind == "metric":
            verdict = relation_shape.answers_measure(
                shapes[relation], physical_field=physical, concept_name=name
            )
        else:
            verdict = relation_shape.answers(
                shapes[relation], physical_field=physical, concept_name=name
            )
        if verdict is not False:
            continue
        if kind == "metric":
            measured = sorted(shapes[relation].metric_keys or ())
            carries = (
                "It measures " + ", ".join(f"`{key}`" for key in measured) + "."
                if measured
                else "It publishes no measure rows at all."
            )
            code = "measure_absent_from_bound_relation"
        else:
            carried = sorted(shapes[relation].breakdown_keys or ())
            carries = (
                "It publishes " + ", ".join(f"`{key}`" for key in carried) + "."
                if carried
                else "It publishes no breakdown at all."
            )
            code = "dimension_absent_from_bound_relation"
        refusals.append(
            Refusal(
                code,
                f"`{name}` is bound to a Datastream whose published rows do not carry "
                f"it. {carries} Collect `{name}` on that Datastream, or bind this "
                "Concept to a Datastream that reports it. A View that published this "
                "would answer the question with nothing instead of saying so.",
                f"$.view.bindings[{index}]",
            )
        )
    return refusals


def _mapped_physical_fields(
    conn: Any, project_id: str, mapping_version_id: str
) -> dict[str, str]:
    """`canonical_target` -> physical `field_id`, for one mapping version.

    The same reading `query_execution.resolve_physical_plan` makes, so publish
    and read cannot disagree about which column answers a Concept.
    """
    rows = _fetch(
        conn,
        """
        SELECT mapping_payload FROM app.datastream_mapping_versions
        WHERE id = %(id)s AND project_id = %(project_id)s
        """,
        {"id": mapping_version_id, "project_id": project_id},
    )
    payload = _json(rows[0]["mapping_payload"]) if rows else {}
    return {
        str((field.get("binding") or {}).get("canonical_target") or ""): str(
            field.get("field_id") or ""
        )
        for field in (payload or {}).get("fields") or []
    }


def _published_relation(
    conn: Any, project_id: str, datastream_id: str, mapping_version_id: str
) -> str:
    """The relation the Datastream's latest Output version of that mapping names."""
    rows = _fetch(
        conn,
        """
        SELECT relation_ref FROM app.datastream_output_versions
        WHERE mapping_version_id = %(mapping)s AND datastream_id = %(datastream)s
          AND project_id = %(project_id)s
        ORDER BY created_at DESC LIMIT 1
        """,
        {
            "mapping": mapping_version_id,
            "datastream": datastream_id,
            "project_id": project_id,
        },
    )
    if not rows:
        return ""
    from core.raw_landing import promoted_relation  # noqa: PLC0415

    reference = rows[0]["relation_ref"]
    if isinstance(reference, str):
        # A published candidate is a promoted one (2026-09-04): the same rule
        # `query_execution.resolve_physical_plan` applies, so publish and read
        # cannot disagree about which relation answers.
        return promoted_relation(reference) if "__cand_" in reference else reference
    reference = _json(reference) or {}
    return str(reference.get("relation") or reference.get("name") or "")


def _evaluate_test_gate(
    conn: Any, project_id: str, object_type: str, object_id: str | None,
    change_set_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Ask Test whether THIS CANDIDATE has passing coverage.

    Three outcomes, and only one of them is ``pass``. When Test has no verdict
    for this object the answer is ``unverifiable`` — which blocks publication
    just as ``fail`` does. Treating "we never checked" as "it is fine" is how a
    gate becomes decoration.

    PINNED TO THE CANDIDATE (story 53.7, CAV-19). The lookup used to select the
    LATEST verdict for `(project_id, object_id)` with no version pin, so a `pass`
    recorded against one candidate still answered for the next one — the object is
    stable, the candidate is not. `analyze-and-test.md:419` states the rule for
    regression comparisons ("a regression comparison mixes unpinned versions");
    a publication gate that mixes them is the same defect on the transition that
    actually ships.

    IT WAS READING A TABLE NOBODY WRITES, and that is what made the gate a dead
    end (repaired 2026-08-16). The lookup asked `app.audit_log` for an action
    named `semantic_model.test_gate`; nothing in this repository has ever emitted
    one, so every answer was `unverifiable / no_test_coverage` and the written
    override was the ONLY way a Concept ever got published from the console.

    Test does emit a verdict, and `analyze-and-test.md:399-405` says where and who
    reads it: *"Test ... emits an immutable **Gate Decision**: `Pass`, `Block` or
    `Unverifiable` ... The owning Governance or Context Hub workflow checks that
    decision before its own publish/activate transition."* That object is
    `app.evaluation_gate_decisions` (migration 153), written by
    `evaluation_runs.emit_gate_decision` with `candidate_owner_workspace =
    'governance'`. This function now reads THAT — the producer and the consumer
    named by the same document, instead of two halves that never touched.

    PINNED BY THE CHANGE SET, NOT BY THE OBJECT, and the reason is creation. A
    change set that CREATES a Concept carries `object_id = NULL` until confirm, so
    an object-keyed lookup could never match one — and `candidate_object_id` has a
    non-empty CHECK, so Test could not have recorded `''` either. The change set
    id IS the candidate's exact identity at the moment the gate is asked, which is
    what `candidate_version_id` pins. The CAV-19 requirement is unchanged and in
    fact strengthened: a verdict recorded against one change set can never answer
    for the next one.

    `block` reads as `fail`, `unverifiable` as `unverifiable`, and anything else
    the column could ever hold as `unverifiable` too — an unknown verdict is not a
    pass. Treating "we never checked" as "it is fine" is how a gate becomes
    decoration.
    """
    if not change_set_id:
        # No candidate to pin means no verdict can belong to it. Said rather than
        # answered with the newest decision of some other change set.
        return "unverifiable", {
            "reason": "no_test_coverage",
            "message": "No Test gate verdict exists for this candidate. Missing "
            "coverage is Unverifiable, not Pass, and blocks publication unless an "
            "authorized, reasoned override is recorded.",
        }
    try:
        rows = _fetch(
            conn,
            """
            SELECT decision, decided_at, comparison_id, coverage, failing_dimensions,
                   decision_reason, candidate_object_type
            FROM app.evaluation_gate_decisions
            WHERE project_id = %(project_id)s
              AND candidate_owner_workspace = 'governance'
              AND candidate_version_id = %(change_set_id)s
            ORDER BY decided_at DESC LIMIT 1
            """,
            {"project_id": project_id, "change_set_id": change_set_id},
        )
    except Exception as exc:  # noqa: BLE001 -- Test owner unreadable
        return "unverifiable", {
            "reason": "test_owner_unreadable",
            "message": "The Test gate owner could not be read. Coverage is unknown, "
            "which is not the same as covered.",
            "detail": type(exc).__name__,
        }
    if not rows:
        return "unverifiable", {
            "reason": "no_test_coverage",
            "message": "No Test gate verdict exists for this candidate. Missing "
            "coverage is Unverifiable, not Pass, and blocks publication unless an "
            "authorized, reasoned override is recorded.",
        }

    row = rows[0]
    # The decision names a candidate object type; a verdict emitted against a
    # Semantic View must not answer for a Concept even under the same change set.
    recorded_type = str(row.get("candidate_object_type") or "")
    if recorded_type and recorded_type != object_type:
        return "unverifiable", {
            "reason": "test_gate_object_mismatch",
            "message": f"The Test gate verdict on this change set was decided for a "
            f"{recorded_type}, not a {object_type}. A verdict about another object "
            "is not coverage of this one.",
        }

    evidence = {
        "recorded_at": _iso(row.get("decided_at")),
        "comparison_id": row.get("comparison_id"),
        "coverage": _json(row.get("coverage")),
        "failing_dimensions": _json(row.get("failing_dimensions")),
    }
    decision = str(row.get("decision") or "").lower()
    if decision == "pass":
        return "pass", evidence
    if decision == "block":
        return "fail", {
            "reason": "test_gate_failed",
            "message": str(row.get("decision_reason") or "")
            or "The Test gate blocked this candidate.",
            **evidence,
        }
    return "unverifiable", {
        "reason": "test_gate_unverifiable",
        "message": str(row.get("decision_reason") or "")
        or "Test could not verify this candidate. Unverifiable is not Pass.",
        **evidence,
    }


def _validate_business_domain_refs(
    conn: Any,
    project_id: str,
    refs: Any,
    *,
    path: str,
) -> list[Refusal]:
    """Refuse a Business Domain reference that does not resolve, before it is frozen.

    `governance.md:72` makes a Semantic View "linkable to Business Domains", and
    `_apply_view` / `_apply_concept` write `business_domain_refs` straight from
    the intent into `app.semantic_view_versions` and
    `app.semantic_concept_versions`. Nothing between the two checked anything:
    an intent naming a domain that does not exist, or one belonging to another
    organization, was written verbatim.

    That matters more here than in an ordinary form, and the reason is the
    lifecycle rather than the field: a published version is IMMUTABLE. A dangling
    reference cannot be corrected, only superseded by a new version — and every
    Result already pinned to the bad one keeps pointing at it. The cheapest
    moment to refuse is the only moment: before the row exists.

    The scope check is not decoration either. Business Domains are ORGANIZATION
    objects (`README.md:97`) and this validates through `app.projects`, so a
    reference reaching across organizations is refused by construction rather
    than by trusting the caller's `org_id` — the same rule the API applies when
    it ignores a client-supplied organization.

    Silence is a valid answer: an empty list means "no domain linked", which is
    the honest default and not a refusal.
    """
    if refs in (None, [], ()):
        return []
    if not isinstance(refs, (list, tuple)):
        return [
            Refusal(
                "malformed_business_domain_refs",
                "Business Domain references must be a list of Business Domain ids.",
                path,
            )
        ]
    wanted = [str(ref) for ref in refs if str(ref or "").strip()]
    if not wanted:
        return []
    duplicates = sorted({ref for ref in wanted if wanted.count(ref) > 1})
    refusals: list[Refusal] = []
    if duplicates:
        refusals.append(
            Refusal(
                "duplicate_business_domain_ref",
                "The same Business Domain is linked twice: " + ", ".join(duplicates) + ".",
                path,
            )
        )
    # Story 49.2: the reference guard reads the authority first. A published
    # Semantic Model version is immutable, so a reference that does not resolve
    # today can never be corrected in place -- and reading the superseded store
    # alone refused every reference to a Business Domain minted in Master Data,
    # which is the only place a Business Domain is minted since the cutover.
    rows = _fetch(
        conn,
        f"""
        SELECT d.id, d.status, d.archived_at
          FROM {catalogue.DOMAIN_SOURCE} d
          JOIN app.projects p ON p.org_id = d.org_id
         WHERE p.id = %(project_id)s AND d.id = ANY(%(ids)s)
        """,
        {"project_id": project_id, "ids": list(dict.fromkeys(wanted))},
    )
    found = {str(row["id"]): row for row in rows}
    unknown = [ref for ref in dict.fromkeys(wanted) if ref not in found]
    if unknown:
        refusals.append(
            Refusal(
                "unknown_business_domain",
                "No Business Domain in this organization has the id "
                + ", ".join(sorted(unknown))
                + ". A published version is immutable, so a reference that does "
                "not resolve today can never be corrected in place.",
                path,
            )
        )
    archived = sorted(
        ref
        for ref, row in found.items()
        if row.get("archived_at") is not None or str(row.get("status") or "") == "archived"
    )
    if archived:
        refusals.append(
            Refusal(
                "archived_business_domain",
                "These Business Domains are archived and cannot be linked by a new "
                "version: " + ", ".join(archived) + ".",
                path,
            )
        )
    return refusals


def prepare_change_set(
    conn: Any,
    project_id: str,
    change_set_id: str,
    *,
    actor: str,
    allow_test_override: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate everything, measure impact, and mint a single-use confirmation."""
    row = _load_change_set_row(conn, project_id, change_set_id)
    if row["state"] not in {"open", "prepared"}:
        raise SemanticRefused(
            "change_set_not_open",
            f"This change set is {row['state']}; only an open one can be prepared.",
        )
    intent = row["intent"] or {}
    action = str(intent.get("action") or "")
    refusals: list[Refusal] = []
    validation: dict[str, Any] = {"action": action, "checked_at": _iso(_now())}

    resolver = concept_resolver(conn, project_id)
    pinned_concept_versions: list[str] = []
    pinned_mapping_versions: list[str] = []
    pinned_master_data: list[str] = []

    if action in {"create_concept", "edit_concept"}:
        payload = intent.get("concept") or {}
        refusals.extend(
            _validate_business_domain_refs(
                conn,
                project_id,
                payload.get("business_domain_refs"),
                path="$.concept.business_domain_refs",
            )
        )
        kind = str(payload.get("kind") or "")
        if kind not in {"metric", "dimension"}:
            refusals.append(
                Refusal("unknown_kind", "A Concept is a metric or a dimension.", "$.concept.kind")
            )
        value_type = str(payload.get("value_type") or "decimal")
        if kind == "metric":
            analysis = validate_expression(
                payload.get("expression"),
                resolver,
                owning_concept_name=str(payload.get("name") or ""),
                owning_value_type=value_type,
            )
            refusals.extend(analysis.refusals)
            if analysis.unresolved_names:
                refusals.append(
                    Refusal(
                        "unresolved_reference",
                        "This formula still refers to "
                        + ", ".join(sorted(set(analysis.unresolved_names)))
                        + " by name. Pin the exact Concept versions before publishing.",
                        "$.concept.expression",
                    )
                )
            refusals.extend(
                validate_aggregation(
                    payload.get("aggregation"),
                    additivity_class=payload.get("additivity_class"),
                    non_additive_dimensions=list(payload.get("non_additive_dimensions") or ()),
                    value_type=value_type,
                )
            )
            pinned_concept_versions.extend(ref.version_id for ref in analysis.dependencies)
            validation["expression"] = analysis.as_dict()
        elif kind == "dimension" and not payload.get("semantic_type"):
            refusals.append(
                Refusal(
                    "undeclared_semantic_type",
                    "A dimension declares its semantic type; without it nothing can "
                    "decide whether it is a time axis.",
                    "$.concept.semantic_type",
                )
            )
        pinned_master_data.extend(
            str(ref.get("version_id"))
            for ref in (payload.get("master_data_refs") or ())
            if isinstance(ref, Mapping) and ref.get("version_id")
        )

        # 27.8 -- AN EDIT DOES NOT RENAME THE IDENTITY. The 2026-08-18 amendment
        # already says it ("What an edit must preserve is on the wire ... the
        # object's canonical `name`"); nothing enforced it. `_apply_concept` writes
        # `semantic_concepts.name` once and never again, so a renaming edit made
        # the version and the head disagree for good -- and the compiled matrix,
        # which publishes that name for the language-family guard to judge on,
        # would have said a dimension was something it is not.
        if action == "edit_concept" and row.get("object_id"):
            head = _fetch(
                conn,
                "SELECT name FROM app.semantic_concepts "
                "WHERE id = %(concept_id)s AND (project_id = %(project_id)s "
                "OR project_id IS NULL)",
                {"concept_id": str(row["object_id"]), "project_id": project_id},
            )
            proposed = str(payload.get("name") or "")
            if head and proposed and proposed != str(head[0]["name"]):
                refusals.append(
                    Refusal(
                        "name_is_not_editable",
                        f"This Concept is `{head[0]['name']}`, and that is the name "
                        "every mapping, view and compiled artifact joins on. Change the "
                        "label to change what people read; a new name is a new Concept.",
                        "$.concept.name",
                    )
                )

    elif action in {"create_view", "edit_view"}:
        payload = intent.get("view") or {}
        refusals.extend(
            _validate_business_domain_refs(
                conn,
                project_id,
                payload.get("business_domain_refs"),
                path="$.view.business_domain_refs",
            )
        )
        # AI-346: the SAME helper the recompilation sweep uses, so a version
        # re-derived under a moved compiler is compiled from the same inputs a
        # publication is.
        inputs = _view_compilation_inputs(conn, project_id, payload)
        members = inputs.members
        refusals.extend(inputs.refusals)
        for index, entry in enumerate(payload.get("relationships") or ()):
            if not isinstance(entry, Mapping):
                continue
            key_version_id = str(entry.get("mdm_common_key_version_id") or "")
            if not key_version_id:
                continue
            rows = _fetch(
                conn,
                "SELECT id FROM app.mdm_common_key_versions "
                "WHERE id = %(version_id)s AND project_id = %(project_id)s",
                {"version_id": key_version_id, "project_id": project_id},
            )
            if not rows:
                refusals.append(
                    Refusal(
                        "common_key_version_not_found",
                        "The relationship's common key version is not available in this "
                        "Project.",
                        f"$.view.relationships[{index}].mdm_common_key_version_id",
                    )
                )
            left_datastream_id = str(entry.get("left_datastream_id") or "")
            right_datastream_id = str(entry.get("right_datastream_id") or "")
            if not left_datastream_id or not right_datastream_id:
                refusals.append(
                    Refusal(
                        "relationship_datastream_pair_required",
                        "A common-key relationship approves one exact Datastream pair.",
                        f"$.view.relationships[{index}]",
                    )
                )
                continue
            rows = _fetch(
                conn,
                "SELECT id FROM app.datastreams WHERE project_id = %(project_id)s "
                "AND archived_at IS NULL AND id = ANY(%(ids)s)",
                {
                    "project_id": project_id,
                    "ids": [left_datastream_id, right_datastream_id],
                },
            )
            if left_datastream_id == right_datastream_id or len(rows) != 2:
                refusals.append(
                    Refusal(
                        "relationship_datastream_pair_not_found",
                        "Both different Datastreams must be available in this Project.",
                        f"$.view.relationships[{index}]",
                    )
                )
        compilation = _compile_view_payload(payload, inputs, resolver)
        refusals.extend(compilation.refusals)
        validation["compilation"] = {
            "summary": compilation.matrix.get("summary", {}),
            "compiler_version": COMPILER_VERSION,
        }
        validation["queryability_matrix"] = compilation.matrix
        validation["ossie_projection"] = compilation.ossie_projection
        validation["ossie_spec_version"] = OSSIE_SPEC_VERSION
        validation["toorow_extension_version"] = TOOROW_EXTENSION_VERSION
        pinned_concept_versions.extend(member.version_id for member in members)

        # Every binding a published View pins must be an ACTIVE Data mapping
        # version. A binding that names a version Data no longer publishes would
        # freeze a mapping the owner has already moved past.
        active = _active_mapping_versions(conn, project_id)
        # THE TWO REFUSALS BELOW NAME THE DATASTREAM, NEVER ITS `ds_<ULID>`.
        # Same class as the `_apply_view` pair repaired under AI-346: a binding
        # refusal is read by whoever is editing the View, and `ds_01J...` is
        # exactly what they cannot resolve. Read once for the whole list.
        words = _datastream_words(conn, project_id)
        for index, binding in enumerate(payload.get("bindings") or ()):
            if not isinstance(binding, Mapping):
                continue
            datastream_id = str(binding.get("datastream_id") or "")
            mapping_version_id = str(binding.get("mapping_version_id") or "")
            if not mapping_version_id:
                # AN ABSENT VERSION IS THE CONSOLE'S CONTRACT, NOT A FAULT.
                # `_apply_view` pins the Datastream's current published mapping
                # inside the confirm transaction, precisely so a browser cannot
                # pin the version it read a moment earlier. This branch refused
                # that same shape, so prepare rejected exactly what apply was
                # written to accept: measured 2026-08-14 against the reference
                # Project, where a 45-binding edit came back with ten
                # `inactive_mapping_binding` refusals and NO named version. No
                # console gesture could publish a View with bindings at all.
                #
                # What must still be refused is a Datastream that publishes
                # nothing -- there the server has no version to pin either, and
                # confirm would raise instead of answering.
                if not active.get(datastream_id):
                    word = words.get(datastream_id) or ""
                    refusals.append(
                        Refusal(
                            "datastream_has_no_published_mapping",
                            (
                                f"The Datastream `{word}` has no published mapping, "
                                "so a Concept cannot be bound to it yet. Open it in "
                                "Data and publish its mapping first."
                                if word
                                else "This binding names a Datastream that is not "
                                "readable in this Project. Open the View's bindings "
                                "and pick the Datastream again."
                            ),
                            f"$.view.bindings[{index}]",
                        )
                    )
                    continue
                pinned_mapping_versions.append(active[datastream_id])
                continue
            if active.get(datastream_id) != mapping_version_id:
                # WHAT IT PUBLISHES, NOT WHICH VERSION. Both ids named a
                # `dsm_<ULID>` no reader can resolve, and neither is the thing
                # to act on: the gesture is the same whether the Datastream has
                # moved on or publishes nothing at all.
                word = words.get(datastream_id) or ""
                subject = (
                    f"The Datastream `{word}`" if word else "The Datastream bound here"
                )
                state = (
                    "publishes no mapping at all"
                    if not active.get(datastream_id)
                    else "no longer publishes the mapping version this binding names"
                )
                refusals.append(
                    Refusal(
                        "inactive_mapping_binding",
                        f"{subject} {state}. Re-open this binding and pin the mapping "
                        "the Datastream publishes today -- governance pins what Data "
                        "publishes; it does not choose it.",
                        f"$.view.bindings[{index}]",
                    )
                )
                continue
            pinned_mapping_versions.append(mapping_version_id)
        refusals.extend(
            _bindings_naming_an_absent_dimension(
                conn, project_id, payload.get("bindings") or (), active
            )
        )
        pinned_master_data.extend(
            str(ref.get("version_id"))
            for ref in (payload.get("master_data_refs") or ())
            if isinstance(ref, Mapping) and ref.get("version_id")
        )

    elif action == "archive_object":
        # RETIRING IS BLOCKED BY WHAT STILL HOLDS THE OBJECT, AND BY NOTHING
        # ELSE. The count below is measured here, at prepare, so the person reads
        # it before approving -- and recomputed inside `_apply_archive`, because
        # a holder published between prepare and confirm must not be archived
        # around.
        if _is_platform_scoped(conn, row["object_type"], row.get("object_id")):
            refusals.append(
                Refusal(
                    "platform_scope_archive_refused",
                    "This object is platform-scoped: every Project reads it. Retiring it "
                    "from one Project would retire it for all of them, and this surface "
                    "speaks for one Project only.",
                    "$.object_id",
                )
            )
        holders = live_consumers(conn, project_id, row["object_type"], row.get("object_id"))
        validation["live_consumers"] = holders
        if holders:
            refusals.append(_archive_refusal(holders))

    # Used-by impact: who is currently pinned to the version being replaced.
    impact = _used_by_impact(conn, project_id, row["object_type"], row.get("object_id"))
    validation["used_by_impact"] = impact

    test_state, test_detail = _evaluate_test_gate(
        conn, project_id, row["object_type"], row.get("object_id"),
        change_set_id=str(row["id"]),
    )
    validation["test_gate"] = {"state": test_state, **test_detail}
    override_payload: str | None = None
    if test_state != "pass" and allow_test_override:
        reason = str(allow_test_override.get("reason") or "").strip()
        if len(reason) < 20:
            raise SemanticRefused(
                "unreasoned_override",
                "Overriding a Test gate requires a written reason of at least 20 "
                "characters. A blank override is indistinguishable from no gate.",
            )
        test_state = "overridden"
        override_payload = canonical_json(
            {
                "reason": reason,
                "authorized_by": actor,
                "overridden_state": validation["test_gate"]["state"],
                "recorded_at": _iso(_now()),
            }
        )
        validation["test_gate"]["override"] = _json(override_payload)

    fingerprint = dependency_fingerprint(
        concept_versions=pinned_concept_versions,
        mapping_versions=pinned_mapping_versions,
        master_data_versions=pinned_master_data,
        policy_version=SEMANTIC_POLICY_VERSION,
    )
    validation["refusals"] = [refusal.as_dict() for refusal in refusals]
    validation["publishable"] = not refusals and test_state in {"pass", "overridden"}

    diff = _compute_diff(conn, project_id, row, intent)
    confirmation_secret = secrets.token_urlsafe(32)
    confirmation_hash = _sha256(f"{change_set_id}:{confirmation_secret}")
    expires_at = _now() + CONFIRMATION_TTL

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.semantic_change_sets
               SET diff = %s::jsonb,
                   dependency_fingerprint = %s,
                   validation = %s::jsonb,
                   test_gate_state = %s,
                   test_gate_override = %s::jsonb,
                   state = 'prepared',
                   confirmation_token_hash = %s,
                   expires_at = %s
             WHERE id = %s AND project_id = %s
            """,
            (
                canonical_json(diff),
                fingerprint,
                canonical_json(validation),
                test_state,
                override_payload,
                confirmation_hash,
                expires_at,
                change_set_id,
                project_id,
            ),
        )

    prepared = get_change_set(conn, project_id, change_set_id)
    return {
        **prepared.as_dict(),
        # Returned ONCE. The server stores only its hash, so a leaked prepare
        # response cannot be replayed from the database side either.
        "confirmation_token": confirmation_secret,
        "confirmation_expires_at": _iso(expires_at),
        "refusals": [refusal.as_dict() for refusal in refusals],
    }


def _used_by_impact(
    conn: Any, project_id: str, object_type: str, object_id: str | None
) -> dict[str, Any]:
    """Who is pinned to this object today. Unreadable owners say so by name."""
    if not object_id:
        return {"state": "empty", "groups": []}
    groups: list[dict[str, Any]] = []
    if object_type == "semantic-concept":
        rows = _fetch(
            conn,
            """
            SELECT DISTINCT vv.view_id, vv.version_number, s.name
            FROM app.semantic_view_version_concepts vc
            JOIN app.semantic_view_versions vv ON vv.id = vc.view_version_id
            JOIN app.semantic_views s ON s.id = vv.view_id
            WHERE vc.concept_id = %(object_id)s AND s.project_id = %(project_id)s
            """,
            {"object_id": object_id, "project_id": project_id},
        )
        groups.append(
            {
                "owner": "governance/semantic-model",
                "label": "Semantic Views",
                "state": "available" if rows else "empty",
                "count": len(rows),
                "refs": [
                    {"object_type": "semantic-view", "id": str(r["view_id"]), "label": r["name"]}
                    for r in rows[:50]
                ],
            }
        )
    unavailable_owners = [
        ("analyze/explore", "Analyze queries", "50.1"),
        ("context-hub/knowledge-library", "Knowledge and Skills", "49.6"),
        ("test/regression-runs", "Test suites", "49.4"),
    ]
    if object_type == "semantic-view":
        # THIS ONE IS ANSWERED, and answering it is what lets a View be retired.
        # `app.query_specs`, `app.golden_question_versions` and
        # `app.observed_cohorts` are the three stores that carry the reference --
        # the same three the Governance read model counts for this object's
        # `used_by` facet -- so the number a person read on the object is the
        # number that appears here. Declaring it `unavailable` beside a store
        # that answers is the defect `_semantic_view_used_by` was repaired for.
        try:
            holders = live_consumers(conn, project_id, object_type, object_id)
        except Exception as exc:  # noqa: BLE001 -- an unread store is not a zero
            groups.append(
                {
                    "owner": "analyze/explore",
                    "label": "Query Specs, Golden Questions and Cohorts",
                    "state": "unavailable",
                    "count": None,
                    "reason": f"These consumers could not be read ({type(exc).__name__}). "
                    "This is not a count of zero.",
                }
            )
        else:
            groups.append(
                {
                    "owner": "analyze/explore",
                    "label": "Query Specs, Golden Questions and Cohorts",
                    "state": "available" if holders else "empty",
                    "count": len(holders),
                    "refs": holders[:50],
                }
            )
        unavailable_owners = unavailable_owners[1:]

    # Context and Skills adapters are not delivered by this story. They report
    # `unavailable` with their owner named -- never an empty group, which would
    # claim nothing references this object.
    for owner, label, story in unavailable_owners:
        groups.append(
            {
                "owner": owner,
                "label": label,
                "state": "unavailable",
                "count": None,
                "reason": f"The {label} relationship adapter is owned by Story {story} "
                "and is not delivered. This is not an empty list.",
            }
        )
    return {"state": "available", "groups": groups}


#: Who still HOLDS a Concept: a Semantic View version that is not history.
#:
#: `_used_by_impact` above counts every version that ever pinned it, which is the
#: right answer for "what has depended on this". It is the WRONG question for
#: retiring: `governance.md` (amendment of 2026-08-16) settles it -- "only a
#: reference that still holds blocks -- a `draft` does, because somebody can
#: publish it tomorrow; a `superseded` or `archived` version does not, because it
#: never comes back and blocking on it makes the object unretirable for good".
_LIVE_CONCEPT_HOLDERS = """
    SELECT DISTINCT s.id AS holder_id, s.name AS holder_name, vv.status AS holder_state
    FROM app.semantic_view_version_concepts vc
    JOIN app.semantic_view_versions vv ON vv.id = vc.view_version_id
    JOIN app.semantic_views s ON s.id = vv.view_id
    WHERE vc.concept_id = %(object_id)s
      AND s.project_id = %(project_id)s
      AND s.lifecycle_status <> 'archived'
      AND vv.status IN ('draft', 'candidate', 'published')
    ORDER BY s.name
"""

#: Who still holds a Semantic View. The same three stores the Governance read
#: model counts for its `used_by` facet (`governance_read_model.py:1904-1919`),
#: so the number a person read on the object is the number that blocks here.
_LIVE_VIEW_HOLDERS = """
    SELECT holder_kind, holder_id, holder_name FROM (
        SELECT 'query-spec' AS holder_kind, id AS holder_id, id AS holder_name
          FROM app.query_specs
         WHERE project_id = %(project_id)s AND semantic_view_id = %(object_id)s
        UNION ALL
        SELECT 'golden-question', golden_question_id, golden_question_id
          FROM app.golden_question_versions
         WHERE project_id = %(project_id)s AND semantic_view_id = %(object_id)s
        UNION ALL
        SELECT 'observed-cohort', id, id
          FROM app.observed_cohorts
         WHERE project_id = %(project_id)s AND semantic_view_id = %(object_id)s
    ) holders
    GROUP BY holder_kind, holder_id, holder_name
    ORDER BY holder_kind, holder_id
"""


def live_consumers(
    conn: Any, project_id: str, object_type: str, object_id: str | None
) -> list[dict[str, Any]]:
    """What would be left pointing at this object if it were retired NOW.

    Exported because the Governance read model asks the same question to decide
    whether `archive_object` is honourable on an object at all: one authority
    answers "who holds this", and a screen offering a gesture the applier would
    refuse is the defect that split those two.

    A read that FAILS raises. "I could not check" and "nothing holds it" are
    different facts, and only one of them makes retiring safe.
    """
    if not object_id:
        return []
    if object_type == "semantic-concept":
        rows = _fetch(
            conn, _LIVE_CONCEPT_HOLDERS, {"object_id": object_id, "project_id": project_id}
        )
        return [
            {
                "object_type": "semantic-view",
                "id": str(row["holder_id"]),
                "label": str(row["holder_name"]),
                "state": str(row["holder_state"]),
            }
            for row in rows
        ]
    rows = _fetch(conn, _LIVE_VIEW_HOLDERS, {"object_id": object_id, "project_id": project_id})
    return [
        {
            "object_type": str(row["holder_kind"]),
            "id": str(row["holder_id"]),
            "label": str(row["holder_name"]),
            "state": "live",
        }
        for row in rows
    ]


def is_platform_scoped(conn: Any, object_type: str, object_id: str | None) -> bool:
    """A Concept every Project reads (`semantic_concepts.project_id IS NULL`).

    `_assert_base_belongs` deliberately lets a Project EDIT one -- the scope rule
    of migration 142 -- but retiring is not editing: it would remove the object
    from every other Project from a surface that speaks for one.
    """
    if not object_id or object_type != "semantic-concept":
        return False
    rows = _fetch(
        conn,
        "SELECT project_id FROM app.semantic_concepts WHERE id = %(object_id)s",
        {"object_id": object_id},
    )
    return bool(rows) and rows[0].get("project_id") is None


#: Kept private-looking for the two call sites inside this module.
_is_platform_scoped = is_platform_scoped


def _archive_refusal(holders: list[dict[str, Any]]) -> Refusal:
    """One sentence, with the holders NAMED. A bare count sends a person hunting."""
    named = ", ".join(f"{holder['label']} ({holder['state']})" for holder in holders[:20])
    return Refusal(
        "live_consumers",
        f"{len(holders)} object(s) still hold this one: {named}. A draft holds it too -- "
        "somebody can publish it tomorrow -- while a superseded or archived version does "
        "not and is not counted here. Retire or edit them first; nothing was changed.",
        "$.object_id",
    )


def _compute_diff(
    conn: Any, project_id: str, row: Mapping[str, Any], intent: Mapping[str, Any]
) -> dict[str, Any]:
    """Server-owned diff between the exact base version and the intent."""
    base_version_id = row.get("base_version_id")
    if not base_version_id:
        return {"kind": "creation", "changed": sorted(intent.keys())}
    table = (
        "app.semantic_concept_versions"
        if row["object_type"] == "semantic-concept"
        else "app.semantic_view_versions"
    )
    rows = _fetch(
        conn, f"SELECT * FROM {table} WHERE id = %(id)s", {"id": base_version_id}
    )
    if not rows:
        return {"kind": "unavailable", "reason": "base version disappeared"}
    base = rows[0]
    payload = intent.get("concept") or intent.get("view") or {}
    changed: list[dict[str, Any]] = []
    for key, proposed in payload.items():
        if key not in base:
            continue
        current = _json(base[key]) if isinstance(base[key], (str, bytes)) else base[key]
        if canonical_json(current) != canonical_json(proposed):
            changed.append({"field": key, "from": current, "to": proposed})
    if row["object_type"] == "semantic-view" and "concepts" in payload:
        # AI-346 -- THE MEMBERS ARE VISIBLE TO THE DIFF. They live in a child
        # table, not in a column of the version row, so the loop above never
        # saw them: measured 2026-09-01, a republish that changed the members'
        # roles produced a diff of "description only". What a person can change
        # is WHICH Concepts and WHICH exact versions, in which order; the role
        # is derived from the Concept's kind and is deliberately not compared.
        current_members = [
            {"concept_id": str(m["concept_id"]), "concept_version_id": str(m["concept_version_id"])}
            for m in _fetch(
                conn,
                "SELECT concept_id, concept_version_id "
                "FROM app.semantic_view_version_concepts "
                "WHERE view_version_id = %(version_id)s ORDER BY ordinal",
                {"version_id": base_version_id},
            )
        ]
        proposed_members = [
            {
                "concept_id": str(entry.get("concept_id") or ""),
                "concept_version_id": str(entry.get("concept_version_id") or ""),
            }
            for entry in (payload.get("concepts") or ())
            if isinstance(entry, Mapping)
        ]
        if canonical_json(current_members) != canonical_json(proposed_members):
            changed.append(
                {"field": "concepts", "from": current_members, "to": proposed_members}
            )
    return {"kind": "edit", "base_version_id": base_version_id, "changed": changed}


# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------


def confirm_change_set(
    conn: Any,
    project_id: str,
    change_set_id: str,
    *,
    actor: str,
    confirmation_token: str,
    org_id: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Consume the confirmation once and commit, or refuse with the exact reason.

    Everything below is recomputed rather than trusted: the token, the expiry,
    the dependency fingerprint and the Test verdict. A prepare that was correct
    ten minutes ago is not evidence that it is correct now.
    """
    row = _load_change_set_row(conn, project_id, change_set_id)
    if row["state"] == "confirmed":
        # Idempotent replay: the same change set confirmed twice returns the same
        # version rather than minting a second one.
        return {
            "change_set": _as_change_set(row).as_dict(),
            "result_version_id": row.get("result_version_id"),
            "replayed": True,
        }
    if row["state"] != "prepared":
        raise SemanticRefused(
            "change_set_not_prepared",
            f"This change set is {row['state']}. Confirm follows prepare.",
        )
    expected = row.get("confirmation_token_hash")
    if not expected or _sha256(f"{change_set_id}:{confirmation_token}") != expected:
        raise SemanticRefused(
            "invalid_confirmation",
            "This confirmation does not match the prepared change set.",
        )
    expires_at = row.get("expires_at")
    if isinstance(expires_at, datetime):
        deadline = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
        if _now() > deadline:
            _reject(conn, project_id, change_set_id, "expired")
            raise SemanticStale(
                "confirmation_expired",
                "This confirmation expired. Prepare again so the impact you approve "
                "is measured against the world as it is now.",
            )
    validation = row.get("validation") or {}
    if not validation.get("publishable"):
        raise SemanticRefused(
            "not_publishable",
            "This change set did not pass preparation.",
            [Refusal(**r) for r in validation.get("refusals", []) if isinstance(r, dict)],
        )

    # Drift check. Recompute the fingerprint from the CURRENT world.
    intent = row["intent"] or {}
    fresh = _recompute_fingerprint(conn, project_id, intent)
    if fresh != row.get("dependency_fingerprint"):
        _reject(conn, project_id, change_set_id, "rejected")
        raise SemanticStale(
            "dependency_drift",
            "A Concept version, mapping version, Master Data pin or policy this "
            "change set was prepared against has changed. Nothing was published, "
            "and the current and last-known-good versions are untouched.",
        )
    # `change_set_id` is NOT optional here, and leaving it out was a live defect
    # (found by story 60.2's create -> prepare -> confirm walk, the first test in
    # this repository to make it). `prepare_change_set` pins the verdict to THIS
    # candidate (`change_set_id=str(row["id"])`, :1077) exactly as 53.7 requires;
    # this call omitted the pin, so it looked for a verdict recorded against
    # `change_set_id = ''` and found none whatever the Test owner had written.
    # The consequence was not a stricter gate but an IMPOSSIBLE one: a change set
    # that prepared with `test_gate_state = 'pass'` was then refused
    # `test_gate_drift` at confirm, so NO semantic Concept or View could ever be
    # published without a reasoned override. Re-reading with the same pin is what
    # makes "recompute rather than trust" mean recompute the same question.
    test_state, _ = _evaluate_test_gate(
        conn,
        project_id,
        row["object_type"],
        row.get("object_id"),
        change_set_id=change_set_id,
    )
    if row["test_gate_state"] != "overridden" and test_state != "pass":
        _reject(conn, project_id, change_set_id, "rejected")
        raise SemanticStale(
            "test_gate_drift",
            f"The Test gate is now {test_state}, not the pass this change set was "
            "prepared against.",
        )

    from core.operations import OperationSpec, execute_operation  # noqa: PLC0415

    def mutation(connection: Any, operation_id: str):
        from core.operations import MutationResult  # noqa: PLC0415

        version_id = _apply_change_set(connection, project_id, row, actor=actor)
        with connection.cursor() as cur:
            cur.execute(
                """
                UPDATE app.semantic_change_sets
                   SET state = 'confirmed',
                       confirmation_used_at = NOW(),
                       result_version_id = %s
                 WHERE id = %s AND project_id = %s AND state = 'prepared'
                """,
                (version_id, change_set_id, project_id),
            )
            if cur.rowcount != 1:
                # Another request consumed the same single-use confirmation
                # between the check above and here.
                raise SemanticStale(
                    "confirmation_already_used",
                    "This confirmation was already consumed.",
                )
        result = {"result_version_id": version_id, "change_set_id": change_set_id}
        return MutationResult(
            # `succeeded`, not `applied`. `execute_operation` accepts exactly
            # {succeeded, failed, outcome_unknown}; `applied` is the fourth
            # invented vocabulary on this one call path, after
            # `confirmation_mode`, `host_context` and `versions`. Each of them
            # raised before the transaction could commit.
            outcome="succeeded",
            before_hash=row.get("dependency_fingerprint"),
            after_hash=content_hash(result),
            result=result,
            outbox_payload={
                "event_type": "semantic_model.version_published",
                "project_id": project_id,
                "object_type": row["object_type"],
                "object_id": row.get("object_id"),
                "version_id": version_id,
            },
        )

    outcome = execute_operation(
        conn,
        OperationSpec(
            command_type=ACTION_SEMANTIC_MODEL_CONFIRM_CHANGE_SET,
            actor=actor,
            effective_org_id=org_id,
            resource_path=("project", project_id, "semantic-model", change_set_id),
            idempotency_key=f"semantic-change-set:{change_set_id}",
            # Both dicts are key-allowlisted by `operations._validate_json`, and
            # both were built from invented vocabularies: `host_context` carried
            # `surface`, and `versions` carried `policy_version`/`compiler_version`/
            # `dependency_fingerprint`. None of those keys is accepted, so this
            # call raised before the mutation -- three separate refusals on one
            # spec, which is what "never executed once" looks like. The
            # dependency fingerprint is not lost: `confirm_change_set` recomputes
            # it and the change-set row stores it.
            host_context={},
            versions={
                "policy": SEMANTIC_POLICY_VERSION,
                "catalog": SEMANTIC_POLICY_VERSION,
                "tool": COMPILER_VERSION,
            },
            request_payload={"change_set_id": change_set_id},
            provider_references={},
            # `server`, not `server_verified`. `operations.prepare_operation`
            # accepts exactly {none, server, host, human} and every other caller
            # in the repository passes one of them; this was the only
            # `server_verified`, so `execute_operation` raised
            # `invalid confirmation_mode` before reaching the mutation and NO
            # change set could ever be confirmed. It went unnoticed because the
            # lifecycle had never been run end to end: `app.semantic_views` and
            # `app.semantic_change_sets` were both empty.
            confirmation_mode="server",
            confirmation_reference=confirmation_token,
            trace_id=trace_id,
        ),
        mutation=mutation,
    )
    return {
        "change_set": get_change_set(conn, project_id, change_set_id).as_dict(),
        "result_version_id": outcome.result.get("result_version_id"),
        "operation_id": outcome.operation_id,
        "audit_event_id": outcome.audit_event_id,
        "replayed": outcome.replayed,
    }


def _reject(conn: Any, project_id: str, change_set_id: str, state: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.semantic_change_sets SET state = %s
            WHERE id = %s AND project_id = %s AND state = 'prepared'
            """,
            (state, change_set_id, project_id),
        )


def _recompute_fingerprint(conn: Any, project_id: str, intent: Mapping[str, Any]) -> str:
    """The same computation prepare made, over the world as it is NOW."""
    payload = intent.get("view") or intent.get("concept") or {}
    concept_versions = [
        str(entry.get("concept_version_id"))
        for entry in (payload.get("concepts") or ())
        if isinstance(entry, Mapping) and entry.get("concept_version_id")
    ]
    if intent.get("concept"):
        resolver = concept_resolver(conn, project_id)
        analysis = validate_expression(
            payload.get("expression"),
            resolver,
            owning_concept_name=str(payload.get("name") or ""),
            owning_value_type=str(payload.get("value_type") or "decimal"),
        )
        concept_versions.extend(ref.version_id for ref in analysis.dependencies)
    active = _active_mapping_versions(conn, project_id)
    mapping_versions = [
        active.get(str(binding.get("datastream_id")), "__missing__")
        for binding in (payload.get("bindings") or ())
        if isinstance(binding, Mapping)
    ]
    master_data = [
        str(ref.get("version_id"))
        for ref in (payload.get("master_data_refs") or ())
        if isinstance(ref, Mapping) and ref.get("version_id")
    ]
    return dependency_fingerprint(
        concept_versions=concept_versions,
        mapping_versions=mapping_versions,
        master_data_versions=master_data,
        policy_version=SEMANTIC_POLICY_VERSION,
    )


def _next_version_number(conn: Any, table: str, column: str, object_id: str) -> int:
    rows = _fetch(
        conn,
        f"SELECT COALESCE(MAX(version_number), 0) + 1 AS next FROM {table} "
        f"WHERE {column} = %(object_id)s",
        {"object_id": object_id},
    )
    return int(rows[0]["next"])


def _apply_change_set(
    conn: Any, project_id: str, row: Mapping[str, Any], *, actor: str
) -> str:
    """Write the immutable version and advance the pointers. One transaction."""
    intent = row["intent"] or {}
    action = str(intent.get("action") or "")
    if action in {"create_concept", "edit_concept"}:
        return _apply_concept(conn, project_id, row, actor=actor)
    if action in {"create_view", "edit_view"}:
        return _apply_view(conn, project_id, row, actor=actor)
    if action == "archive_object":
        return _apply_archive(conn, project_id, row, actor=actor)
    raise SemanticRefused("unsupported_intent", f"{action!r} cannot be applied.")


#: The head row of each governed type. Both carry `lifecycle_status` with
#: `'archived'` in their CHECK since migration 142, and both name-uniqueness
#: indexes are partial `WHERE lifecycle_status <> 'archived'` -- the schema was
#: built for this transition ("archived rows excluded so a name can be retired
#: and re-minted", `142_semantic_model.sql:107`). No migration adds it.
_ARCHIVE_HEAD_TABLE = {
    "semantic-concept": "app.semantic_concepts",
    "semantic-view": "app.semantic_views",
}


def _apply_archive(
    conn: Any, project_id: str, row: Mapping[str, Any], *, actor: str
) -> str:
    """Retire the OBJECT, and touch not one of its versions.

    Three decisions, and each of them is a refusal of something else:

    * **The head row moves; the versions do not.** A version is an immutable
      snapshot and `status` is its own lifecycle -- what already pins one keeps
      working, which is what the confirmation promises the person. Rewriting the
      published version to `archived` would break that promise for every artifact
      that pinned it, to record a fact about the object rather than the version.
    * **`current_version_id` and `last_known_good_version_id` stay.** A retired
      object still says what it was; clearing the pointers would make an archive
      indistinguishable from a deletion, and this transition deletes nothing.
    * **The holders are recomputed here.** `prepare` measured them and the person
      approved that measurement, but confirm is the moment that writes: a View
      published in between would otherwise be left pointing at a retired Concept.
      This is the same "recompute rather than trust" `confirm_change_set` applies
      to the token, the expiry, the fingerprint and the Test verdict.

    Returns the base version id: the exact version this retirement was approved
    against. `result_version_id` is NOT NULL on a confirmed change set
    (`ck_semantic_change_sets_confirmation`), and inventing a new version id for
    a transition that writes no version would be a version nobody can read.
    """
    object_type = str(row["object_type"])
    object_id = str(row.get("object_id") or "")
    base_version_id = str(row.get("base_version_id") or "")
    if not object_id or not base_version_id:
        raise SemanticRefused(
            "missing_exact_base",
            "Retiring names an exact object and the exact version it was approved "
            "against.",
        )
    if is_platform_scoped(conn, object_type, object_id):
        raise SemanticRefused(
            "platform_scope_archive_refused",
            "This object is platform-scoped: retiring it from one Project would "
            "retire it for every Project. Nothing was changed.",
        )
    holders = live_consumers(conn, project_id, object_type, object_id)
    if holders:
        refusal = _archive_refusal(holders)
        raise SemanticRefused("live_consumers", refusal.message, [refusal])

    table = _ARCHIVE_HEAD_TABLE[object_type]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {table}
               SET lifecycle_status = 'archived',
                   pending_version_id = NULL,
                   updated_at = NOW()
             WHERE id = %s
               AND (project_id = %s OR project_id IS NULL)
               AND lifecycle_status <> 'archived'
            """,
            (object_id, project_id),
        )
        if cur.rowcount != 1:
            # Already archived, or not this Project's. Both are "nothing to do
            # here", and neither may report a retirement this call performed.
            raise SemanticStale(
                "already_archived",
                "This object is no longer retirable from this Project: it is already "
                "archived, or it moved. Nothing was changed.",
            )
    # A retired object depends on nothing. Its Business Domain references are
    # RELEASED, not deleted -- the row stays and says when it stopped holding --
    # and this is what makes "withdraw the view, then archive the domain" a real
    # repair path instead of a dead end: the authority's guard reads live rows.
    from core.semantic_model_used_by import release_object  # noqa: PLC0415

    release_object(conn, project_id=project_id, object_type=object_type, object_id=object_id)
    return base_version_id


def _apply_concept(
    conn: Any, project_id: str, row: Mapping[str, Any], *, actor: str
) -> str:
    payload = (row["intent"] or {}).get("concept") or {}
    concept_id = row.get("object_id")
    validation = row.get("validation") or {}
    with conn.cursor() as cur:
        if not concept_id:
            concept_id = _mint("sc")
            cur.execute(
                """
                INSERT INTO app.semantic_concepts
                    (id, project_id, kind, name, lifecycle_status, created_by)
                VALUES (%s, %s, %s, %s, 'draft', %s)
                """,
                (
                    concept_id,
                    project_id,
                    str(payload.get("kind")),
                    str(payload.get("name")),
                    actor,
                ),
            )
        version_number = _next_version_number(
            conn, "app.semantic_concept_versions", "concept_id", concept_id
        )
        version_id = _mint("scv")
        body = {
            "kind": payload.get("kind"),
            "name": payload.get("name"),
            "label": payload.get("label") or payload.get("name"),
            "value_type": payload.get("value_type"),
            "expression": payload.get("expression"),
            "aggregation": payload.get("aggregation"),
            "version_number": version_number,
        }
        cur.execute(
            """
            INSERT INTO app.semantic_concept_versions
                (id, concept_id, project_id, version_number, status, kind, name, label,
                 definition, value_type, unit, format, owner, business_domain_refs,
                 master_data_refs, expression, aggregation, additivity_class,
                 non_additive_dimensions, currency_behavior, time_behavior, display,
                 semantic_type, allowed_grains, hierarchies, value_domain, conformance,
                 time_semantics, provenance, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 'published', %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb,
                    %s::jsonb, %s, %s)
            """,
            (
                version_id,
                concept_id,
                project_id,
                version_number,
                str(payload.get("kind")),
                str(payload.get("name")),
                str(payload.get("label") or payload.get("name")),
                payload.get("definition"),
                str(payload.get("value_type") or "decimal"),
                payload.get("unit"),
                payload.get("format"),
                payload.get("owner"),
                canonical_json(payload.get("business_domain_refs") or []),
                canonical_json(payload.get("master_data_refs") or []),
                canonical_json(payload.get("expression")) if payload.get("expression") else None,
                canonical_json(payload.get("aggregation")) if payload.get("aggregation") else None,
                payload.get("additivity_class"),
                list(payload.get("non_additive_dimensions") or ()),
                canonical_json(payload.get("currency_behavior") or {}),
                canonical_json(payload.get("time_behavior") or {}),
                canonical_json(payload.get("display") or {}),
                payload.get("semantic_type"),
                list(payload.get("allowed_grains") or ()),
                canonical_json(payload.get("hierarchies") or []),
                canonical_json(payload.get("value_domain") or {}),
                payload.get("conformance"),
                canonical_json(payload.get("time_semantics") or {}),
                canonical_json(
                    {
                        "change_set_id": row["id"],
                        "base_version_id": row.get("base_version_id"),
                        "authored_by": actor,
                    }
                ),
                content_hash(body),
                actor,
            ),
        )
        for ordinal, dependency in enumerate(
            (validation.get("expression") or {}).get("dependencies") or []
        ):
            cur.execute(
                """
                INSERT INTO app.semantic_concept_dependencies
                    (version_id, ordinal, depends_on_concept_id, depends_on_version_id, role)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    ordinal,
                    dependency["concept_id"],
                    dependency["version_id"],
                    dependency["role"],
                ),
            )
        # The version that WAS current becomes last-known-good. Losing it would
        # leave nothing to fall back to the next time a publish fails.
        cur.execute(
            """
            UPDATE app.semantic_concepts
               SET last_known_good_version_id = COALESCE(current_version_id,
                                                         last_known_good_version_id),
                   current_version_id = %s,
                   pending_version_id = NULL,
                   lifecycle_status = 'published',
                   updated_at = NOW()
             WHERE id = %s
            """,
            (version_id, concept_id),
        )
        cur.execute(
            """
            UPDATE app.semantic_concept_versions SET status = 'superseded'
             WHERE concept_id = %s AND id <> %s AND status = 'published'
            RETURNING id
            """,
            (concept_id, version_id),
        )
        superseded = [str(row[0]) for row in cur.fetchall()]
    _record_business_domain_dependencies(
        conn,
        project_id=project_id,
        object_type="semantic-concept",
        object_id=str(concept_id),
        version_id=version_id,
        business_domain_refs=payload.get("business_domain_refs"),
        superseded_version_ids=superseded,
        actor=actor,
    )
    return version_id


def _apply_view(conn: Any, project_id: str, row: Mapping[str, Any], *, actor: str) -> str:
    payload = (row["intent"] or {}).get("view") or {}
    validation = row.get("validation") or {}
    view_id = row.get("object_id")
    with conn.cursor() as cur:
        if not view_id:
            view_id = _mint("sv")
            cur.execute(
                """
                INSERT INTO app.semantic_views
                    (id, project_id, name, lifecycle_status, created_by)
                VALUES (%s, %s, %s, 'draft', %s)
                """,
                (view_id, project_id, str(payload.get("name")), actor),
            )
        version_number = _next_version_number(
            conn, "app.semantic_view_versions", "view_id", view_id
        )
        version_id = _mint("svv")
        cur.execute(
            """
            INSERT INTO app.semantic_view_versions
                (id, view_id, project_id, version_number, status, name, label,
                 description, business_scope, business_domain_refs, master_data_refs,
                 query_policy, evidence_refs, dependency_fingerprint, content_hash,
                 created_by)
            VALUES (%s, %s, %s, %s, 'published', %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s, %s, %s)
            """,
            (
                version_id,
                view_id,
                project_id,
                version_number,
                str(payload.get("name")),
                str(payload.get("label") or payload.get("name")),
                payload.get("description"),
                payload.get("business_scope"),
                canonical_json(payload.get("business_domain_refs") or []),
                canonical_json(payload.get("master_data_refs") or []),
                canonical_json(payload.get("query_policy") or {}),
                canonical_json(payload.get("evidence_refs") or []),
                row.get("dependency_fingerprint"),
                content_hash({"view": payload, "version_number": version_number}),
                actor,
            ),
        )
        # AI-346 -- THE ROLE IS THE CONCEPT'S KIND, LOOKED UP ONCE. This used to
        # store `entry.get("role") or "metric"`: the caller's word, defaulting
        # to `metric`, while the compiler read `kind`. Measured 2026-09-01 on
        # the reference Project: 13 of 13 members stored as `metric`, dimensions
        # included. `prepare` refuses a contradicting payload through
        # `_members_for_compilation`; this is the same refusal at the write,
        # for a caller that reaches `_apply_view` without it.
        member_entries = [
            entry for entry in (payload.get("concepts") or ()) if isinstance(entry, Mapping)
        ]
        kinds = _concept_kinds(
            conn, [str(entry.get("concept_id") or "") for entry in member_entries]
        )
        for ordinal, entry in enumerate(member_entries):
            concept_id = str(entry.get("concept_id") or "")
            # THE SENTENCES NAME THE CONCEPT AND THE GESTURE, NEVER THE `smc_`.
            # Both printed the identifier the caller sent until
            # `test_refusals_never_name_an_identifier.py` caught them: an id is
            # what a person cannot look up, and it is the one thing the reader
            # already has. When the head is unreadable there is no word to serve
            # -- that is what the refusal says -- so it names the gesture instead.
            resolved = kinds.get(concept_id)
            if resolved is None:
                raise SemanticRefused(
                    "unknown_reference",
                    "This View lists a Concept that is not readable in this Project. "
                    "Open the Semantic Model and pick the Concept again, or publish "
                    "the one it means.",
                )
            kind, concept_name = resolved
            claimed = str(entry.get("role") or "")
            if claimed and claimed != kind:
                raise SemanticRefused(
                    "member_role_mismatch",
                    f"`{concept_name}` is a {kind}, and this View lists it as a "
                    f"{claimed}. A member's role is the Concept's kind; leave it "
                    "out, or say what the Concept says.",
                )
            cur.execute(
                """
                INSERT INTO app.semantic_view_version_concepts
                    (view_version_id, ordinal, concept_id, concept_version_id, role)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    ordinal,
                    concept_id,
                    entry["concept_version_id"],
                    kind,
                ),
            )
        for ordinal, entry in enumerate(payload.get("relationships") or ()):
            cur.execute(
                """
                INSERT INTO app.semantic_view_version_relationships
                    (view_version_id, project_id, ordinal, name, from_dataset, to_dataset,
                     from_columns, to_columns, cardinality_type, bridge_dataset,
                     fan_out_policy, mdm_common_key_version_id,
                     left_datastream_id, right_datastream_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    project_id,
                    ordinal,
                    entry["name"],
                    entry["from_dataset"],
                    entry["to_dataset"],
                    list(entry.get("from_columns") or ()),
                    list(entry.get("to_columns") or ()),
                    entry.get("cardinality_type") or "many_to_one",
                    entry.get("bridge_dataset"),
                    entry.get("fan_out_policy") or "forbid",
                    entry.get("mdm_common_key_version_id"),
                    entry.get("left_datastream_id"),
                    entry.get("right_datastream_id"),
                ),
            )
        #  AI-358 (governance.md, 2026-09-02): a new version refuses to bind an
        #  ARCHIVED Datastream -- the `archived_business_domain` posture, applied
        #  to the flux. Historical bindings of published versions stay as
        #  history; this door simply stops minting new ones.
        binding_flux = sorted(
            {
                str(entry.get("datastream_id"))
                for entry in (payload.get("bindings") or ())
                if entry.get("datastream_id")
            }
        )
        if binding_flux:
            cur.execute(
                """SELECT id FROM app.datastreams
                    WHERE id = ANY(%s) AND project_id = %s
                      AND archived_at IS NOT NULL""",
                (binding_flux, project_id),
            )
            archived_flux = sorted(str(row[0]) for row in cur.fetchall())
            if archived_flux:
                raise SemanticRefused(
                    "archived_datastream",
                    "These Datastreams are archived and cannot be bound by a new "
                    "version: " + ", ".join(archived_flux) + ". Restore them in "
                    "Data, or bind the Datastream that measures this today.",
                )
        for ordinal, entry in enumerate(payload.get("bindings") or ()):
            # THE SCREEN NAMES THE FLUX; THE SERVER PINS THE VERSION. A console
            # that pinned the mapping version itself would pin the one it read a
            # moment earlier, and a mapping published in between would be bound
            # to a version nobody chose. The Datastream's current mapping is the
            # server's own answer, read inside this transaction.
            if not entry.get("mapping_version_id"):
                cur.execute(
                    """SELECT current_mapping_version_id FROM app.datastreams
                        WHERE id = %s AND project_id = %s""",
                    (entry.get("datastream_id"), project_id),
                )
                pinned = cur.fetchone()
                if pinned is None or not pinned[0]:
                    raise SemanticRefused(
                        "datastream_has_no_published_mapping",
                        "This Datastream has no published mapping, so a Concept "
                        "cannot be bound to it yet. Publish the Datastream first.",
                    )
                entry = {**entry, "mapping_version_id": pinned[0]}
            cur.execute(
                """
                INSERT INTO app.semantic_view_version_bindings
                    (view_version_id, ordinal, concept_id, datastream_id, project_id,
                     mapping_version_id, output_ref, binding_state, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (
                    version_id,
                    ordinal,
                    entry["concept_id"],
                    entry["datastream_id"],
                    project_id,
                    entry["mapping_version_id"],
                    canonical_json(entry.get("output_ref") or {}),
                    entry.get("binding_state") or "active",
                    entry.get("confidence"),
                ),
            )
        matrix = validation.get("queryability_matrix") or {}
        projection = validation.get("ossie_projection") or {}
        cur.execute(
            """
            INSERT INTO app.semantic_compiled_artifacts
                (id, project_id, view_version_id, compiler_version, content_hash,
                 queryability_matrix, ossie_projection, ossie_spec_version,
                 toorow_extension_version, dbt_relation_refs)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb)
            """,
            (
                _mint("sca"),
                project_id,
                version_id,
                COMPILER_VERSION,
                content_hash({"matrix": matrix, "projection": projection}),
                canonical_json(matrix),
                canonical_json(projection),
                OSSIE_SPEC_VERSION,
                TOOROW_EXTENSION_VERSION,
                canonical_json(payload.get("dbt_relation_refs") or []),
            ),
        )
        cur.execute(
            """
            UPDATE app.semantic_views
               SET last_known_good_version_id = COALESCE(current_version_id,
                                                         last_known_good_version_id),
                   current_version_id = %s,
                   pending_version_id = NULL,
                   lifecycle_status = 'published',
                   updated_at = NOW()
             WHERE id = %s AND project_id = %s
            """,
            (version_id, view_id, project_id),
        )
        cur.execute(
            """
            UPDATE app.semantic_view_versions SET status = 'superseded'
             WHERE view_id = %s AND id <> %s AND status = 'published'
            RETURNING id
            """,
            (view_id, version_id),
        )
        superseded = [str(row[0]) for row in cur.fetchall()]
    _record_business_domain_dependencies(
        conn,
        project_id=project_id,
        object_type="semantic-view",
        object_id=str(view_id),
        version_id=version_id,
        business_domain_refs=payload.get("business_domain_refs"),
        superseded_version_ids=superseded,
        actor=actor,
    )
    return version_id


def _record_business_domain_dependencies(
    conn: Any,
    *,
    project_id: str,
    object_type: str,
    object_id: str,
    version_id: str,
    business_domain_refs: Any,
    superseded_version_ids: Sequence[str],
    actor: str,
) -> None:
    """The published version declares, in the authority's store, what it depends on.

    ONE writer, at the one moment the reference becomes true -- inside this
    transaction, so a dependency cannot be lost while the version that declares
    it is live. Everything it decides lives in `core.semantic_model_used_by`;
    what belongs here is only the hook, and the reason it is a hook rather than a
    read performed later by whoever needs it.

    Archiving a Business Domain used to be refused while a live Semantic Model
    version still referenced it, and the cutover of 2026-08-25 lost that guard:
    Master Data's own archive is impact-guarded, but it reads
    `app.master_data_used_by` and nothing wrote a Semantic Model reference there.
    `governance.md` carries the hole in its *Incomplete if* rather than in a
    footnote. This call closes it from the side that KNOWS -- the publisher.

    The import is local, and deliberately: `core.semantic_model_used_by` reads
    `business_taxonomy` and `master_data_convergence`, and a module-level import
    here would put the whole Master Data subsystem behind every import of the
    Semantic Model.
    """

    from core.semantic_model_used_by import (  # noqa: PLC0415
        register_published_version,
    )

    register_published_version(
        conn,
        project_id=project_id,
        object_type=object_type,
        object_id=object_id,
        version_id=version_id,
        business_domain_refs=business_domain_refs,
        superseded_version_ids=superseded_version_ids,
        actor=actor,
    )


# ---------------------------------------------------------------------------
# Recompilation under a moved compiler (AI-346, governance.md 2026-09-01)
#
# A compiled artifact is DERIVED from an immutable version. `COMPILER_VERSION`
# moved on 2026-08-22 and nothing re-derived anything: the reference Project's
# published View kept a `semantic-compiler.v1` artifact, `_load_pinned_view`
# refused every new question as `stale_compiled_artifact`, and the only gesture
# on offer was a new version of a composition that had not changed. This is the
# sweep that repeats the derivation -- a NEW artifact row per stale version, no
# version touched, no gate -- and records what it could not repeat, so the
# refusal a person reads names the attempt.
# ---------------------------------------------------------------------------

#: The actor the sweep signs with. A source suffix (`:startup`, `:nightly`,
#: `:script`) is appended by the caller, so the audit row says WHICH run.
RECOMPILE_ACTOR = "system:semantic-recompile"

#: The versions that can still EXECUTE: the two pointers of every View and every
#: pin a Query Spec or Golden Question version holds, in `published` status only
#: (a `superseded` pin is refused `version_not_executable` before any artifact is
#: read). Staleness is a fact of the NEWEST artifact, which is what the readers
#: take; a version with no artifact at all is `not_compiled`, a different upstream
#: inconsistency this sweep does not paper over.
_STALE_EXECUTABLE_VERSIONS = """
    WITH executable AS (
        SELECT current_version_id AS version_id FROM app.semantic_views
         WHERE current_version_id IS NOT NULL
        UNION
        SELECT last_known_good_version_id FROM app.semantic_views
         WHERE last_known_good_version_id IS NOT NULL
        UNION
        SELECT semantic_view_version_id FROM app.query_spec_versions
        UNION
        SELECT semantic_view_version_id FROM app.golden_question_versions
    )
    SELECT v.id, v.view_id, v.project_id, v.version_number, v.name, v.label,
           v.description, v.query_policy,
           a.id AS artifact_id, a.compiler_version, a.ossie_projection,
           a.dbt_relation_refs
      FROM app.semantic_view_versions v
      JOIN executable e ON e.version_id = v.id
      JOIN LATERAL (
            SELECT id, compiler_version, ossie_projection, dbt_relation_refs
              FROM app.semantic_compiled_artifacts
             WHERE view_version_id = v.id
             ORDER BY created_at DESC
             LIMIT 1
      ) a ON TRUE
     WHERE v.status = 'published'
       AND (%(project_id)s::text IS NULL OR v.project_id = %(project_id)s)
       AND a.compiler_version IS DISTINCT FROM %(compiler_version)s
     ORDER BY v.project_id, v.view_id, v.version_number
"""


def list_stale_view_versions(conn: Any, *, project_id: str | None = None) -> list[dict[str, Any]]:
    """Every executable, published version whose newest artifact is not the current compiler's.

    A reading. `--dry-run` and the report both print exactly this.
    """
    rows = _fetch(
        conn,
        _STALE_EXECUTABLE_VERSIONS,
        {"project_id": project_id, "compiler_version": COMPILER_VERSION},
    )
    for row in rows:
        row["query_policy"] = _json(row.get("query_policy")) or {}
        row["ossie_projection"] = _json(row.get("ossie_projection")) or {}
        row["dbt_relation_refs"] = _json(row.get("dbt_relation_refs")) or []
    return rows


def _member_datasets_from_projection(projection: Mapping[str, Any]) -> dict[str, str]:
    """concept_id -> dataset name, read from the previous artifact's Ossie projection.

    The per-member dataset was never given a column (`datastream_matches.py:11`).
    The projection carries it twice: a dimension is a `field` of its dataset, a
    metric names `dataset` inside its toorow extension. This reads the artifact for
    an INPUT the compiler was handed, never for a verdict.
    """

    def _toorow(extensions: Any) -> dict[str, Any]:
        for extension in extensions or ():
            if not isinstance(extension, Mapping):
                continue
            data = extension.get("data")
            if isinstance(data, str):
                data = _json(data)
            if isinstance(data, Mapping) and isinstance(data.get("toorow"), Mapping):
                return dict(data["toorow"])
        return {}

    datasets: dict[str, str] = {}
    for dataset in projection.get("datasets") or ():
        if not isinstance(dataset, Mapping):
            continue
        for field_object in dataset.get("fields") or ():
            if not isinstance(field_object, Mapping):
                continue
            concept_id = _toorow(field_object.get("custom_extensions")).get("concept_id")
            if concept_id:
                datasets[str(concept_id)] = str(dataset.get("name") or "")
    for metric in projection.get("metrics") or ():
        if not isinstance(metric, Mapping):
            continue
        payload = _toorow(metric.get("custom_extensions"))
        if payload.get("concept_id") and payload.get("dataset"):
            datasets[str(payload["concept_id"])] = str(payload["dataset"])
    return datasets


def _composition_for_version(
    conn: Any, version: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    """The composition a version was compiled from, and where it was read.

    First the confirmed change set whose `result_version_id` is this version: it
    is the exact payload `prepare` compiled. Its members must agree with the
    stored rows -- when they do not, it is not this version's composition. Then
    the stored rows plus the declared inputs of the artifact being replaced.
    Returns `(None, "stored_rows")` when neither yields a compilable payload.
    """
    version_id = str(version["id"])
    project_id = str(version["project_id"])
    stored_members = load_view_members(conn, version_id)
    stored_pairs = [
        (str(m["concept_id"]), str(m["concept_version_id"])) for m in stored_members
    ]

    rows = _fetch(
        conn,
        """
        SELECT intent FROM app.semantic_change_sets
         WHERE result_version_id = %(version_id)s AND project_id = %(project_id)s
           AND state = 'confirmed'
         ORDER BY confirmation_used_at DESC NULLS LAST, created_at DESC
         LIMIT 1
        """,
        {"version_id": version_id, "project_id": project_id},
    )
    if rows:
        intent = rows[0].get("intent")
        if isinstance(intent, (str, bytes)):
            intent = _json(intent)
        view_payload = (intent or {}).get("view") if isinstance(intent, Mapping) else None
        if isinstance(view_payload, Mapping):
            entries = [e for e in (view_payload.get("concepts") or ()) if isinstance(e, Mapping)]
            intent_pairs = [
                (str(e.get("concept_id") or ""), str(e.get("concept_version_id") or ""))
                for e in entries
            ]
            if intent_pairs == stored_pairs:
                payload = dict(view_payload)
                # The role is derived; a payload that carried the wrong one is
                # the very defect being recompiled around.
                payload["concepts"] = [
                    {k: v for k, v in entry.items() if k != "role"} for entry in entries
                ]
                return payload, "change_set"

    if not stored_members:
        return None, "stored_rows"
    projection = version.get("ossie_projection") or {}
    member_datasets = _member_datasets_from_projection(projection)
    payload = {
        "name": version.get("name"),
        "label": version.get("label"),
        "description": version.get("description"),
        "query_policy": version.get("query_policy") or {},
        "dbt_relation_refs": list(version.get("dbt_relation_refs") or []),
        "concepts": [
            {
                "concept_id": str(m["concept_id"]),
                "concept_version_id": str(m["concept_version_id"]),
                **(
                    {"dataset": member_datasets[str(m["concept_id"])]}
                    if str(m["concept_id"]) in member_datasets
                    else {}
                ),
            }
            for m in stored_members
        ],
        "relationships": [
            {
                "name": r.get("name"),
                "from_dataset": r.get("from_dataset"),
                "to_dataset": r.get("to_dataset"),
                "from_columns": list(r.get("from_columns") or ()),
                "to_columns": list(r.get("to_columns") or ()),
                "cardinality_type": r.get("cardinality_type"),
                "fan_out_policy": r.get("fan_out_policy"),
                "bridge_dataset": r.get("bridge_dataset"),
            }
            for r in load_view_relationships(conn, version_id)
        ],
        "datasets": [
            {
                "name": d.get("name"),
                "source": d.get("source"),
                "primary_key": list(d.get("primary_key") or ()),
                "unique_keys": [list(k) for k in (d.get("unique_keys") or ())],
            }
            for d in (projection.get("datasets") or ())
            if isinstance(d, Mapping)
        ],
    }
    return payload, "stored_rows"


def _record_recompile_attempt(
    conn: Any,
    *,
    version: Mapping[str, Any],
    outcome: str,
    refusals: Sequence[Refusal],
    source: str,
    artifact_id: str | None,
    actor: str,
) -> None:
    """One row per (version, compiler), overwritten: the record of the NEWEST attempt."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.semantic_recompile_attempts
                (view_version_id, compiler_version, project_id, outcome, refusals,
                 composition_source, artifact_id, attempted_by, attempted_at)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, NOW())
            ON CONFLICT (view_version_id, compiler_version) DO UPDATE
               SET outcome = EXCLUDED.outcome,
                   refusals = EXCLUDED.refusals,
                   composition_source = EXCLUDED.composition_source,
                   artifact_id = EXCLUDED.artifact_id,
                   attempted_by = EXCLUDED.attempted_by,
                   attempted_at = NOW()
            """,
            (
                str(version["id"]),
                COMPILER_VERSION,
                str(version["project_id"]),
                outcome,
                canonical_json([r.as_dict() for r in refusals]),
                source,
                artifact_id,
                actor,
            ),
        )


def recompile_stale_artifacts(
    conn: Any,
    *,
    project_id: str | None = None,
    actor: str = RECOMPILE_ACTOR,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-derive the artifact of every executable published version the compiler has not compiled.

    For each stale version: read its composition, build the compiler's inputs
    with the helper `prepare_change_set` uses, compile, and -- only when nothing
    refuses -- INSERT a new `semantic_compiled_artifacts` row under
    `COMPILER_VERSION`. The version row is never touched; the old artifact is never
    updated. A compilation with refusals is recorded and reported by version with
    its codes; the read-side refusal then names the attempt.

    Commits nothing: the caller owns the transaction. Idempotent: the second run
    lists no stale version, because the first replaced the newest artifact.
    """
    stale = list_stale_view_versions(conn, project_id=project_id)
    report: dict[str, Any] = {
        "compiler_version": COMPILER_VERSION,
        "dry_run": dry_run,
        "stale": [
            {
                "version_id": str(v["id"]),
                "view_id": str(v["view_id"]),
                "project_id": str(v["project_id"]),
                "version_number": v["version_number"],
                "compiled_by": v.get("compiler_version"),
            }
            for v in stale
        ],
        "recompiled": [],
        "refused": [],
    }
    if dry_run:
        return report

    from core.audit import insert_audit_row  # noqa: PLC0415 -- only on the write path

    resolvers: dict[str, ConceptResolver] = {}
    for version in stale:
        version_id = str(version["id"])
        version_project = str(version["project_id"])
        payload, source = _composition_for_version(conn, version)
        if payload is None:
            refusals = [
                Refusal(
                    "no_composition",
                    "This version has no member rows and no confirmed change set, so "
                    "there is nothing to recompile from.",
                    "$.concepts",
                )
            ]
        else:
            resolver = resolvers.get(version_project)
            if resolver is None:
                resolver = resolvers[version_project] = concept_resolver(conn, version_project)
            inputs = _view_compilation_inputs(conn, version_project, payload)
            compilation = _compile_view_payload(payload, inputs, resolver)
            refusals = list(inputs.refusals) + list(compilation.refusals)

        if refusals:
            _record_recompile_attempt(
                conn,
                version=version,
                outcome="refused",
                refusals=refusals,
                source=source,
                artifact_id=None,
                actor=actor,
            )
            report["refused"].append(
                {
                    "version_id": version_id,
                    "view_id": str(version["view_id"]),
                    "project_id": version_project,
                    "version_number": version["version_number"],
                    "compiled_by": version.get("compiler_version"),
                    "composition_source": source,
                    "codes": sorted({r.code for r in refusals}),
                    "refusals": [r.as_dict() for r in refusals],
                    "gesture": "correct the version and publish it again through a change set",
                }
            )
            continue

        matrix = compilation.matrix
        projection = compilation.ossie_projection
        artifact_id = _mint("sca")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.semantic_compiled_artifacts
                    (id, project_id, view_version_id, compiler_version, content_hash,
                     queryability_matrix, ossie_projection, ossie_spec_version,
                     toorow_extension_version, dbt_relation_refs)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s::jsonb)
                ON CONFLICT ON CONSTRAINT uq_semantic_compiled_artifact_version DO NOTHING
                RETURNING id
                """,
                (
                    artifact_id,
                    version_project,
                    version_id,
                    COMPILER_VERSION,
                    content_hash({"matrix": matrix, "projection": projection}),
                    canonical_json(matrix),
                    canonical_json(projection),
                    OSSIE_SPEC_VERSION,
                    TOOROW_EXTENSION_VERSION,
                    canonical_json(compilation.dbt_relation_refs or []),
                ),
            )
            inserted = cur.fetchone()
            if inserted is None:
                # Another run inserted this (version, compiler) between the
                # listing and here. Its row is the artifact; report it as such.
                cur.execute(
                    "SELECT id FROM app.semantic_compiled_artifacts "
                    "WHERE view_version_id = %s AND compiler_version = %s",
                    (version_id, COMPILER_VERSION),
                )
                existing = cur.fetchone()
                artifact_id = str(existing[0]) if existing else artifact_id
        _record_recompile_attempt(
            conn,
            version=version,
            outcome="recompiled",
            refusals=(),
            source=source,
            artifact_id=artifact_id,
            actor=actor,
        )
        insert_audit_row(
            conn,
            identity=actor,
            action=ACTION_SEMANTIC_MODEL_RECOMPILE_ARTIFACT,
            provider_account="",
            connection_ref="",
            metadata={
                "project_id": version_project,
                "view_id": str(version["view_id"]),
                "view_version_id": version_id,
                "artifact_id": artifact_id,
                "previous_artifact_id": str(version.get("artifact_id") or ""),
                "previous_compiler_version": version.get("compiler_version"),
                "compiler_version": COMPILER_VERSION,
                "composition_source": source,
                "summary": (matrix or {}).get("summary", {}),
            },
        )
        report["recompiled"].append(
            {
                "version_id": version_id,
                "view_id": str(version["view_id"]),
                "project_id": version_project,
                "version_number": version["version_number"],
                "compiled_by": version.get("compiler_version"),
                "artifact_id": artifact_id,
                "composition_source": source,
                "summary": (matrix or {}).get("summary", {}),
            }
        )
    return report
