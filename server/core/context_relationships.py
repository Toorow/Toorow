"""Story 49-6 AC5 -- the one authority for a cross-owner context relation.

WHAT THIS OWNS, AND WHAT IT REPLACES. Until migration 317 the product's only
cross-owner relation was a row in `app.context_graph`: no version, no org, no
foreign key, no RLS, and a hard `DELETE` for a retirement
(`context_store.py:1861`). Every other governed object of this repository has
carried an append-only version since migration 200, so the relation -- the fact
that says *this Skill applies to that governed field* -- was the one governed
fact nobody governed. Measured 2026-08-24 in `story-log.md`: *"AC5 -- l'autorite
des relations -- a ZERO 25 jours apres"*.

ONE WRITER, TWO DOORS. `context-hub.md`'s amendment of 2026-08-25 says it in as
many words -- *what is forbidden is a second WRITER, never a second door*. So
`context_store.create_graph_edge` and `delete_graph_edge` keep their signatures
and their callers (`context_seed`, the REST route, the tests) and became thin
delegations onto this module. Nothing writes `app.context_graph` but
`_write_projection` below.

`app.context_graph` IS NOW A READ PROJECTION, written here in the same
transaction as the authority. That is character for character the shape
`governance.md` Decision 2 ratified for the superseded taxonomy store, and it is
taken for the identical reason: four non-test readers resolve a relation through
that table (`context_search.py:1098`, `context_seed.py:437`,
`datamodel.py:1551`, `mirror_sync.py:471`) and a cutover that moved them all at
once would be a big-bang nobody could review. They are re-pointed reader by
reader, later, and this module is what makes that possible without them noticing
today.

THE PROJECTION IS NARROWER THAN THE AUTHORITY, ON PURPOSE. `app.context_graph`'s
CHECK constraints know five endpoint types (migrations 031, 117, 272). The
authority knows eight: it adds `semantic_view`, `metric` and `datastream`, which
is the whole point of AC5 -- *"Views, metrics, Datastreams, Knowledge and Skills
link in both directions"*. A relation naming one of those three is held ONLY in
the authority and projects nothing, because a projection row the legacy store
cannot express would have to be invented, and an invented row is worse than an
absent one.

A MASTER DATA <-> MASTER DATA RELATION IS REFUSED BY NAME. It belongs to story
49.2's service and to `app.mdm_business_links`. The refusal is written twice --
here, so a caller reads a sentence, and in the CHECK constraint of migration
317, so no door of this store can hold one whatever the code does.

WHAT IS STORED IS IDS AND TYPED FACTS. Never a copied label, never a URL. A
relation names its endpoints by type and id; the owner keeps its own payload,
and the owner link this module renders is the owner's OWN address, resolved from
the route table each owner already publishes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

import psycopg
from ulid import ULID

from core.audit import declare_action, insert_audit_row

# ---------------------------------------------------------------------------
# AD-42: an action is declared by the module that WRITES it. These three are
# new values, not a rename of `context_graph.edge.created` / `.deleted`: the
# gesture changed. A retirement no longer destroys a row -- it appends a
# supersession fact -- and a restore did not exist at all.
# ---------------------------------------------------------------------------
ACTION_CONTEXT_RELATIONSHIP_CREATED = declare_action("context_relationship.created")
ACTION_CONTEXT_RELATIONSHIP_SUPERSEDED = declare_action("context_relationship.superseded")
ACTION_CONTEXT_RELATIONSHIP_RESTORED = declare_action("context_relationship.restored")


#: Every endpoint the authority can name. The first five are
#: `context_store.GRAPH_NODE_TYPES` -- the incumbent vocabulary, carried whole so
#: the backfill is a carry and not a translation. The last three are AC5's
#: reason for existing.
ENDPOINT_TYPES: frozenset[str] = frozenset(
    {
        "topic",
        "procedure",
        "schema_doc",
        "target_field",
        "master_data_node",
        "semantic_view",
        "metric",
        "datastream",
    }
)

#: The subset `app.context_graph` can hold. A relation outside it lives in the
#: authority alone -- see the module docstring.
PROJECTED_ENDPOINT_TYPES: frozenset[str] = frozenset(
    {"topic", "procedure", "schema_doc", "target_field", "master_data_node"}
)

#: Endpoints whose owner is Project-scoped and therefore cannot be named from
#: the PLATFORM scope. A platform relation lives above every project; pointing
#: it at one project's Datastream would make it unreadable from every other.
PROJECT_ONLY_ENDPOINT_TYPES: frozenset[str] = frozenset(
    {"semantic_view", "metric", "datastream"}
)

#: How many relations one node's facet returns. A "Used by / Related" panel is
#: read by a person; past this it is a dump, and the caller is told the answer
#: was cut rather than left to believe it was complete.
DEFAULT_FACET_LIMIT = 100
MAX_FACET_LIMIT = 500

#: Bumped when the hashed document changes shape, so a stored hash is only ever
#: compared to a hash produced by the same contract. Migration 317's backfill
#: builds the SAME document in SQL, and `test_context_relationships_pg` pins the
#: two against each other.
RELATIONSHIP_CONTRACT_VERSION = "context-relationship.v1"


class ContextRelationshipRefused(ValueError):
    """A refusal with a code a caller can act on and a sentence a human reads.

    The sentence NAMES THE GESTURE THAT REPAIRS and never an identifier -- the
    rule `test_refusals_never_name_an_identifier` sweeps the whole of `core/`
    for, and the reason every message below speaks of *the topic*, *the
    Datastream* rather than printing a `crel_...`.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ContextRelationshipNotFound(LookupError):
    """Foreign, denied and nonexistent are ONE answer.

    Telling them apart tells an unauthorized caller that the object exists, so
    every read that cannot serve raises this and the route answers 404.
    """


class ContextRelationshipsUnavailable(RuntimeError):
    """A read that GATES something could not be served -- fail closed.

    The reverse-link FACET does not raise: a panel that cannot read renders
    ``state: unavailable`` and says so, because "I could not look" must never
    render as "nothing is related" (the story's *Incomplete if*). A read that
    guards a mutation raises, because a guard that did not run is not a guard.
    """


@dataclass(frozen=True)
class Endpoint:
    """One end of a relation: a type and an id, and nothing copied."""

    type: str
    id: str

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def relationship_hash(
    *,
    project_id: str | None,
    source: Endpoint,
    target: Endpoint,
    relationship_kind: str,
) -> str:
    """The identity of a relation version.

    A NEWLINE-JOINED DOCUMENT rather than JSON, and that is not a style choice:
    migration 317's backfill computes the same hash in SQL, and `concat_ws` over
    seven fields is a thing two implementations can agree on byte for byte,
    where a canonical JSON serialiser is a thing they eventually disagree on.
    `test_the_backfill_hash_is_the_one_python_writes` pins the agreement.

    The endpoint LABELS are deliberately absent. Renaming a topic does not
    change which topic relates to which field, and a hash that moved on a rename
    would break every consumer that pinned the version.
    """
    document = "\n".join(
        [
            RELATIONSHIP_CONTRACT_VERSION,
            project_id or "",
            source.type,
            source.id,
            target.type,
            target.id,
            relationship_kind,
        ]
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


def _mint(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


def _record(
    conn: Any, action: str, *, actor: str, project_id: str | None, **metadata: Any
) -> None:
    """One journal row, ON THE SAME TRANSACTION as what it records.

    `insert_audit_row` rather than `write_audit_row`: the second opens its own
    connection and never raises, which suits a caller that has already
    committed. Here the caller has not -- the route calls, then `conn.commit()`
    -- so a journal outside the transaction would assert a gesture a rollback
    erased.
    """
    insert_audit_row(
        conn,
        identity=actor or "anonymous",
        action=action,
        provider_account="platform",
        connection_ref="",
        metadata={"project_id": project_id, **metadata},
    )


# ---------------------------------------------------------------------------
# Validation -- through the OWNER, never by a query of our own
# ---------------------------------------------------------------------------


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _endpoint(value_type: Any, value_id: Any, *, side: str) -> Endpoint:
    node_type = _clean(value_type)
    node_id = _clean(value_id)
    if node_type not in ENDPOINT_TYPES:
        raise ContextRelationshipRefused(
            "unknown_endpoint_type",
            f"Choose a {side} of a kind this product relates: "
            f"{', '.join(sorted(ENDPOINT_TYPES))}.",
        )
    if not node_id:
        raise ContextRelationshipRefused(
            "endpoint_not_named",
            f"Name the {side} this relation points at.",
        )
    return Endpoint(node_type, node_id)


def _relationship_kinds() -> frozenset[str]:
    """The vocabulary, read from the store that already publishes it.

    A second list here would be a second vocabulary, and
    `context_store.py:1569` records what the first pile of vocabularies cost.
    Imported inside the function because `context_store` delegates BACK to this
    module, and a module-level import in both directions is a cycle.
    """
    from core.context_store import GRAPH_EDGE_TYPES  # noqa: PLC0415

    return GRAPH_EDGE_TYPES


def _endpoint_exists(
    conn: Any, endpoint: Endpoint, *, project_id: str | None
) -> bool:
    """Ask the OWNER, never a query of our own.

    Two owners would eventually disagree about what "visible in this project"
    means, and the disagreement would show up as a relation pointing at an
    object the owning screen refuses to open. Each branch below calls the public
    read its owner already publishes.
    """
    if endpoint.type in PROJECT_ONLY_ENDPOINT_TYPES and not project_id:
        return False

    if endpoint.type in PROJECTED_ENDPOINT_TYPES:
        from core.context_store import node_exists_in_scope  # noqa: PLC0415

        return node_exists_in_scope(
            conn, node_id=endpoint.id, node_type=endpoint.type, project_id=project_id
        )

    if endpoint.type == "semantic_view":
        from core import semantic_model  # noqa: PLC0415

        try:
            semantic_model.load_view(conn, str(project_id), endpoint.id)
        except LookupError:
            return False
        return True

    if endpoint.type == "metric":
        from core.canonical_field_registry import (  # noqa: PLC0415
            list_visible_canonical_fields,
        )

        return any(
            field["id"] == endpoint.id and field["concept_kind"] == "metric"
            for field in list_visible_canonical_fields(conn, project_id=str(project_id))
        )

    if endpoint.type == "datastream":
        from core.datastreams import get_datastream  # noqa: PLC0415

        return get_datastream(endpoint.id, str(project_id), conn) is not None

    return False


def _assert_endpoint_reachable(
    conn: Any, endpoint: Endpoint, *, project_id: str | None, side: str
) -> None:
    if endpoint.type in PROJECT_ONLY_ENDPOINT_TYPES and not project_id:
        raise ContextRelationshipRefused(
            "endpoint_needs_a_project",
            f"Open a project and declare this relation there: a {side} of this "
            "kind belongs to one project and cannot be related from the "
            "platform scope.",
        )
    if not _endpoint_exists(conn, endpoint, project_id=project_id):
        raise ContextRelationshipRefused(
            "endpoint_not_in_scope",
            f"Pick a {side} that exists in this project and is not archived, "
            "or restore it first.",
        )


def _project_org_id(conn: Any, project_id: str) -> str:
    """The organization, DERIVED from the project graph and never sent in.

    A caller-supplied org is how a row ends up carrying another tenant's id and
    crossing the RLS by its own column.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.projects WHERE id = %s AND status = 'active'",
            (project_id,),
        )
        row = cur.fetchone()
    if row is None or not row[0]:
        raise ContextRelationshipsUnavailable(
            "the project's organization could not be read"
        )
    return str(row[0])


# ---------------------------------------------------------------------------
# Owner links -- the owner's OWN address, never one invented here
# ---------------------------------------------------------------------------

#: Each address below is a route this repository actually mounts. A link this
#: module invented would 404 on the facet that renders it, which is a worse
#: answer than no link at all.
def owner_link(endpoint: Endpoint, *, project_id: str | None) -> dict[str, Any]:
    node_type, node_id = endpoint.type, endpoint.id
    scope = project_id or ""
    if node_type == "topic":
        return {"surface": "knowledge", "href": f"/api/context/topics/{node_id}"}
    if node_type == "procedure":
        return {"surface": "skills", "href": f"/api/context/procedures/{node_id}"}
    if node_type == "schema_doc":
        return {"surface": "knowledge-graph", "href": f"/api/context/graph?project_id={scope}"}
    if node_type == "target_field":
        return {
            "surface": "data-dictionary",
            "href": f"/api/context/graph?project_id={scope}&include_fields=all",
        }
    if node_type == "master_data_node":
        return {
            "surface": "master-data",
            "href": f"/api/projects/{scope}/governance/master-data/nodes",
        }
    if node_type == "semantic_view":
        return {
            "surface": "semantic-model",
            "href": f"/api/projects/{scope}/governance/semantic-model",
        }
    if node_type == "metric":
        return {
            "surface": "canonical-fields",
            "href": f"/api/projects/{scope}/mdm/canonical-fields",
        }
    return {"surface": "datastreams", "href": f"/api/projects/{scope}/datastreams/{node_id}"}


# ---------------------------------------------------------------------------
# The projection -- the ONLY place `app.context_graph` is written
# ---------------------------------------------------------------------------


def _write_projection(
    conn: Any,
    *,
    project_id: str | None,
    source: Endpoint,
    target: Endpoint,
    relationship_kind: str,
    created_by: str,
) -> dict[str, Any] | None:
    """One legacy row, in the SAME transaction, or None when it cannot exist.

    RETURNS THE ROW, not just its id, so no caller has to read back what this
    function just wrote. A relation, its version, its projection and its audit
    row are one transaction; adding a SELECT between them buys nothing and costs
    a round trip on the platform seed's every edge.

    `governance.md` Decision 2's shape. The row is not a second authority: it is
    written by this function and by nothing else, and it is deleted the moment
    the relation it projects is superseded.
    """
    if source.type not in PROJECTED_ENDPOINT_TYPES:
        return None
    if target.type not in PROJECTED_ENDPOINT_TYPES:
        return None

    # `uq_context_graph_edge` (031:56) is unique on the endpoint tuple WITHOUT
    # the project, so two projects cannot both project the same pair. Asked
    # before the INSERT rather than caught after it: a 23505 aborts the caller's
    # whole transaction unless a SAVEPOINT wraps it, and the caller has a
    # relation, a version and an audit row riding on that transaction.
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM app.context_graph
            WHERE from_id = %s AND from_type = %s
              AND to_id = %s AND to_type = %s AND edge_type = %s
            """,
            (source.id, source.type, target.id, target.type, relationship_kind),
        )
        if cur.fetchone() is not None:
            raise ContextRelationshipRefused(
                "relation_already_exists",
                "Open the existing relation instead: these two are already "
                "related this way.",
            )

    edge_id = _mint("edge")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.context_graph
                (id, from_id, from_type, to_id, to_type, edge_type,
                 project_id, created_by, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
            RETURNING
                id, from_id, from_type, to_id, to_type, edge_type,
                project_id, created_by, created_at
            """,
            (
                edge_id,
                source.id,
                source.type,
                target.id,
                target.type,
                relationship_kind,
                project_id,
                created_by,
            ),
        )
        columns = [description[0] for description in cur.description]
        row = cur.fetchone()
    edge = dict(zip(columns, row))
    edge["created_at"] = _iso(edge["created_at"])
    return edge


def _drop_projection(conn: Any, edge_id: str | None) -> None:
    if not edge_id:
        return
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.context_graph WHERE id = %s", (edge_id,))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

_HEAD_COLUMNS = (
    "id",
    "org_id",
    "project_id",
    "source_type",
    "source_id",
    "target_type",
    "target_id",
    "relationship_kind",
    "status",
    "provenance",
    "projection_edge_id",
    "current_version_id",
    "created_by",
    "created_at",
    "updated_at",
)

_VERSION_COLUMNS = (
    "id",
    "relationship_id",
    "version_number",
    "lifecycle",
    "fact",
    "supersedes_version_id",
    "source_version_id",
    "target_version_id",
    "content_hash",
    "created_by",
    "created_at",
)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _head_row(row: Any) -> dict[str, Any]:
    record = dict(zip(_HEAD_COLUMNS, row))
    record["created_at"] = _iso(record["created_at"])
    record["updated_at"] = _iso(record["updated_at"])
    return record


def _fetch_head(
    conn: Any, *, relationship_id: str, project_id: str | None
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_HEAD_COLUMNS)}
            FROM app.context_relationships
            WHERE id = %s
              AND (project_id IS NULL OR project_id = %s)
            """,
            (relationship_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ContextRelationshipNotFound("no relation is readable under this scope")
    return _head_row(row)


def _versions(conn: Any, *, relationship_id: str) -> list[dict[str, Any]]:
    """Every act, oldest first, with `superseded_by` DERIVED.

    The column does not exist and cannot: filling it on version N the day N+1 is
    written is the UPDATE migration 317's trigger refuses. So the successor
    names its predecessor (`supersedes_version_id`) and the predecessor's
    `superseded_by` is read back here. The chain is complete in both directions
    and only one end of it is writable -- which is the property that makes it
    evidence.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_VERSION_COLUMNS)}
            FROM app.context_relationship_versions
            WHERE relationship_id = %s
            ORDER BY version_number
            """,
            (relationship_id,),
        )
        rows = [dict(zip(_VERSION_COLUMNS, row)) for row in cur.fetchall()]
    superseded_by = {
        row["supersedes_version_id"]: row["id"]
        for row in rows
        if row["supersedes_version_id"]
    }
    for row in rows:
        row["created_at"] = _iso(row["created_at"])
        row["superseded_by"] = superseded_by.get(row["id"])
    return rows


def read_relationship(
    conn: Any, *, project_id: str | None, relationship_id: str
) -> dict[str, Any]:
    """One relation, its versions and the two owner links it names."""
    head = _fetch_head(conn, relationship_id=relationship_id, project_id=project_id)
    source = Endpoint(head["source_type"], head["source_id"])
    target = Endpoint(head["target_type"], head["target_id"])
    head["versions"] = _versions(conn, relationship_id=relationship_id)
    head["source_owner"] = owner_link(source, project_id=head["project_id"])
    head["target_owner"] = owner_link(target, project_id=head["project_id"])
    return head


def list_for_node(
    conn: Any,
    *,
    project_id: str | None,
    node_type: str,
    node_id: str,
    limit: int = DEFAULT_FACET_LIMIT,
    include_superseded: bool = False,
) -> dict[str, Any]:
    """The "Used by / Related" facet, BOTH directions, bounded and typed.

    A FAILED READ IS NEVER AN EMPTY LIST. The story's *Incomplete if* names it:
    *"a failed reverse-link read renders as 'no relations'"*. So a read that
    could not run comes back ``state: 'unavailable'`` with no `outgoing` /
    `incoming` keys to mistake for an answer, and the owner renders the sentence
    rather than the absence.
    """
    endpoint = _endpoint(node_type, node_id, side="node")
    bounded = max(1, min(int(limit or DEFAULT_FACET_LIMIT), MAX_FACET_LIMIT))
    statuses = ("active", "superseded") if include_superseded else ("active",)

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(_HEAD_COLUMNS)}
                FROM app.context_relationships
                WHERE (project_id IS NULL OR project_id = %s)
                  AND status = ANY(%s)
                  AND (
                        (source_type = %s AND source_id = %s)
                     OR (target_type = %s AND target_id = %s)
                  )
                ORDER BY created_at DESC, id
                LIMIT %s
                """,
                (
                    project_id,
                    list(statuses),
                    endpoint.type,
                    endpoint.id,
                    endpoint.type,
                    endpoint.id,
                    bounded + 1,
                ),
            )
            rows = [_head_row(row) for row in cur.fetchall()]
    except ContextRelationshipRefused:
        raise
    except Exception as exc:  # noqa: BLE001 -- any read failure is one answer
        return {
            "state": "unavailable",
            "node": endpoint.as_dict(),
            "reason": "related_items_unreadable",
            "message": (
                "Open this panel again in a moment: the related items could not "
                "be read."
            ),
            "detail": type(exc).__name__,
        }

    truncated = len(rows) > bounded
    rows = rows[:bounded]

    outgoing: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = []
    for row in rows:
        source = Endpoint(row["source_type"], row["source_id"])
        target = Endpoint(row["target_type"], row["target_id"])
        item = {
            "relationship_id": row["id"],
            "relationship_kind": row["relationship_kind"],
            "status": row["status"],
            "provenance": row["provenance"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
        if source.type == endpoint.type and source.id == endpoint.id:
            outgoing.append(
                {
                    **item,
                    "direction": "outgoing",
                    "other": target.as_dict(),
                    "owner": owner_link(target, project_id=row["project_id"]),
                }
            )
        else:
            incoming.append(
                {
                    **item,
                    "direction": "incoming",
                    "other": source.as_dict(),
                    "owner": owner_link(source, project_id=row["project_id"]),
                }
            )

    return {
        "state": "ready",
        "node": endpoint.as_dict(),
        "outgoing": outgoing,
        "incoming": incoming,
        "limit": bounded,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Writes -- the only three gestures, and the only writer of either store
# ---------------------------------------------------------------------------


def _next_version_number(conn: Any, *, relationship_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 "
            "FROM app.context_relationship_versions WHERE relationship_id = %s",
            (relationship_id,),
        )
        return int(cur.fetchone()[0])


def _current_version_id(conn: Any, *, relationship_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_version_id FROM app.context_relationships WHERE id = %s",
            (relationship_id,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def _append_version(
    conn: Any,
    *,
    relationship_id: str,
    org_id: str | None,
    project_id: str | None,
    lifecycle: str,
    fact: Mapping[str, Any],
    content_hash: str,
    supersedes_version_id: str | None,
    source_version_id: str | None,
    target_version_id: str | None,
    actor: str,
    version_number: int | None = None,
) -> dict[str, Any]:
    version_id = _mint("crelv")
    # A CREATE ALWAYS WRITES VERSION 1 and says so, rather than asking the
    # database what it already knows. The read is for the acts that follow.
    if version_number is None:
        version_number = _next_version_number(conn, relationship_id=relationship_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.context_relationship_versions
                (id, relationship_id, org_id, project_id, version_number,
                 lifecycle, fact, supersedes_version_id, source_version_id,
                 target_version_id, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            """,
            (
                version_id,
                relationship_id,
                org_id,
                project_id,
                version_number,
                lifecycle,
                json.dumps(dict(fact), sort_keys=True),
                supersedes_version_id,
                source_version_id,
                target_version_id,
                content_hash,
                actor,
            ),
        )
        cur.execute(
            "UPDATE app.context_relationships "
            "SET current_version_id = %s, updated_at = now() WHERE id = %s",
            (version_id, relationship_id),
        )
    return {
        "id": version_id,
        "relationship_id": relationship_id,
        "version_number": version_number,
        "lifecycle": lifecycle,
        "fact": dict(fact),
        "supersedes_version_id": supersedes_version_id,
        "source_version_id": source_version_id,
        "target_version_id": target_version_id,
        "content_hash": content_hash,
        "created_by": actor,
        "superseded_by": None,
    }


def create_relationship(
    conn: Any,
    *,
    project_id: str | None,
    source_type: str,
    source_id: str,
    target_type: str,
    target_id: str,
    relationship_kind: str,
    actor: str,
    provenance: str = "console",
    source_version_id: str | None = None,
    target_version_id: str | None = None,
) -> dict[str, Any]:
    """Declare one relation: the authority, its first version, its projection.

    Everything happens on the CALLER's transaction. The route commits; a
    rollback erases the relation, its version, its projection and its audit row
    together, which is the only way three stores can be said to agree.
    """
    source = _endpoint(source_type, source_id, side="source")
    target = _endpoint(target_type, target_id, side="target")
    kind = _clean(relationship_kind)

    if kind not in _relationship_kinds():
        raise ContextRelationshipRefused(
            "unknown_relationship_kind",
            "Choose one of the relations this product names: "
            f"{', '.join(sorted(_relationship_kinds()))}.",
        )

    if source.type == "master_data_node" and target.type == "master_data_node":
        raise ContextRelationshipRefused(
            "master_data_relation_belongs_to_the_mdm",
            "Declare this relation in Master Data: a link between two governed "
            "identities is held by the Master Data authority, not by the "
            "Context Hub.",
        )

    # EXISTENCE IS ASKED FIRST, and the order is the message. A person who names
    # a retired field twice needs to hear that the field is gone, not that a
    # relation joins two -- the second sentence is true and sends them looking
    # in the wrong place.
    _assert_endpoint_reachable(conn, source, project_id=project_id, side="source")
    _assert_endpoint_reachable(conn, target, project_id=project_id, side="target")

    if source.type == target.type and source.id == target.id:
        raise ContextRelationshipRefused(
            "relation_points_at_itself",
            "Pick a second object: a relation joins two, and this names one "
            "twice.",
        )

    org_id = _project_org_id(conn, project_id) if project_id else None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status FROM app.context_relationships
            WHERE COALESCE(project_id, '') = COALESCE(%s, '')
              AND source_type = %s AND source_id = %s
              AND target_type = %s AND target_id = %s
              AND relationship_kind = %s
              AND status = 'active'
            """,
            (project_id, source.type, source.id, target.type, target.id, kind),
        )
        if cur.fetchone() is not None:
            raise ContextRelationshipRefused(
                "relation_already_exists",
                "Open the existing relation instead: these two are already "
                "related this way.",
            )

    relationship_id = _mint("crel")
    # The SELECT above is a courtesy, not the guard: two concurrent creates both
    # pass it, and the second lands on `uq_context_relationships_active` (or on
    # `uq_context_graph_edge` in the projection). Review of 2026-08-28 measured
    # that landing as a 500 `db_error` where the serial path says 422
    # `relation_already_exists`; the unique index IS the rule, so its violation
    # is the same refusal, said the same way.
    try:
        projection = _write_projection(
            conn,
            project_id=project_id,
            source=source,
            target=target,
            relationship_kind=kind,
            created_by=actor,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise ContextRelationshipRefused(
            "relation_already_exists",
            "Open the existing relation instead: these two are already "
            "related this way.",
        ) from exc
    edge_id = projection["id"] if projection else None

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.context_relationships
                    (id, org_id, project_id, source_type, source_id, target_type,
                     target_id, relationship_kind, status, provenance,
                     projection_edge_id, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, %s)
                """,
                (
                    relationship_id,
                    org_id,
                    project_id,
                    source.type,
                    source.id,
                    target.type,
                    target.id,
                    kind,
                    _clean(provenance) or "console",
                    edge_id,
                    actor,
                ),
            )

    except psycopg.errors.UniqueViolation as exc:
        raise ContextRelationshipRefused(
            "relation_already_exists",
            "Open the existing relation instead: these two are already "
            "related this way.",
        ) from exc

    fact = {
        "source": source.as_dict(),
        "target": target.as_dict(),
        "kind": kind,
        "provenance": _clean(provenance) or "console",
    }
    version = _append_version(
        conn,
        relationship_id=relationship_id,
        org_id=org_id,
        project_id=project_id,
        lifecycle="created",
        fact=fact,
        content_hash=relationship_hash(
            project_id=project_id, source=source, target=target, relationship_kind=kind
        ),
        supersedes_version_id=None,
        source_version_id=source_version_id,
        target_version_id=target_version_id,
        actor=actor,
        version_number=1,
    )

    _record(
        conn,
        ACTION_CONTEXT_RELATIONSHIP_CREATED,
        actor=actor,
        project_id=project_id,
        relationship=relationship_id,
        source_type=source.type,
        target_type=target.type,
        relationship_kind=kind,
    )

    # BUILT IN MEMORY, never read back. Everything below was written by this
    # function on this transaction; a SELECT to learn it again would be a read
    # that can only agree with itself, and it would put four more statements on
    # the platform seed's every edge.
    return {
        "id": relationship_id,
        "org_id": org_id,
        "project_id": project_id,
        "source_type": source.type,
        "source_id": source.id,
        "target_type": target.type,
        "target_id": target.id,
        "relationship_kind": kind,
        "status": "active",
        "provenance": _clean(provenance) or "console",
        "projection_edge_id": edge_id,
        "projection": projection,
        "current_version_id": version["id"],
        "created_by": actor,
        "versions": [version],
        "source_owner": owner_link(source, project_id=project_id),
        "target_owner": owner_link(target, project_id=project_id),
    }


def supersede_relationship(
    conn: Any,
    *,
    project_id: str | None,
    relationship_id: str,
    actor: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Retire a relation: a version row, and the projection loses its row.

    NOTHING IS DESTROYED. `context-hub.md` ratified it on 2026-08-17 for a
    manual event -- *"Retirement is a supersede, never a delete"* -- and
    `governance.md` restates it for a governed link. The head keeps its identity
    and its history; only the READ projection stops carrying it, which is what
    makes the four legacy readers agree with the authority.
    """
    head = _fetch_head(conn, relationship_id=relationship_id, project_id=project_id)
    if head["status"] != "active":
        raise ContextRelationshipRefused(
            "relation_already_superseded",
            "Restore this relation first: it is already retired.",
        )

    _drop_projection(conn, head["projection_edge_id"])

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.context_relationships "
            "SET status = 'superseded', projection_edge_id = NULL, updated_at = now() "
            "WHERE id = %s",
            (relationship_id,),
        )

    source = Endpoint(head["source_type"], head["source_id"])
    target = Endpoint(head["target_type"], head["target_id"])
    version = _append_version(
        conn,
        relationship_id=relationship_id,
        org_id=head["org_id"],
        project_id=head["project_id"],
        lifecycle="superseded",
        fact={
            "source": source.as_dict(),
            "target": target.as_dict(),
            "kind": head["relationship_kind"],
            "provenance": head["provenance"],
            "reason": _clean(reason) or None,
        },
        content_hash=relationship_hash(
            project_id=head["project_id"],
            source=source,
            target=target,
            relationship_kind=head["relationship_kind"],
        ),
        supersedes_version_id=head["current_version_id"],
        source_version_id=None,
        target_version_id=None,
        actor=actor,
    )

    _record(
        conn,
        ACTION_CONTEXT_RELATIONSHIP_SUPERSEDED,
        actor=actor,
        project_id=head["project_id"],
        relationship=relationship_id,
        relationship_kind=head["relationship_kind"],
        reason=_clean(reason) or None,
    )

    # The head as it now stands plus the act just written -- built in memory for
    # `create_relationship`'s reason. `version`, singular: the FULL history is
    # `read_relationship`, and returning one row under the plural key would
    # invite a caller to read it as the whole chain.
    head["status"] = "superseded"
    head["projection_edge_id"] = None
    head["current_version_id"] = version["id"]
    head["version"] = version
    head["source_owner"] = owner_link(source, project_id=head["project_id"])
    head["target_owner"] = owner_link(target, project_id=head["project_id"])
    return head


def restore(
    conn: Any, *, project_id: str | None, relationship_id: str, actor: str
) -> dict[str, Any]:
    """Put a retired relation back -- an archive is a version, so it has a way back.

    `context-hub.md`'s amendment of 2026-08-18. The endpoints are re-validated:
    a relation retired six months ago may point at a topic that has since been
    archived, and restoring it would resurrect a link to an object no screen can
    open.
    """
    head = _fetch_head(conn, relationship_id=relationship_id, project_id=project_id)
    if head["status"] == "active":
        raise ContextRelationshipRefused(
            "relation_already_active",
            "Nothing to restore: this relation is live.",
        )

    source = Endpoint(head["source_type"], head["source_id"])
    target = Endpoint(head["target_type"], head["target_id"])
    _assert_endpoint_reachable(
        conn, source, project_id=head["project_id"], side="source"
    )
    _assert_endpoint_reachable(
        conn, target, project_id=head["project_id"], side="target"
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM app.context_relationships
            WHERE COALESCE(project_id, '') = COALESCE(%s, '')
              AND source_type = %s AND source_id = %s
              AND target_type = %s AND target_id = %s
              AND relationship_kind = %s
              AND status = 'active'
            """,
            (
                head["project_id"],
                source.type,
                source.id,
                target.type,
                target.id,
                head["relationship_kind"],
            ),
        )
        if cur.fetchone() is not None:
            raise ContextRelationshipRefused(
                "relation_already_exists",
                "Open the live relation instead: these two were related this "
                "way again while this one was retired.",
            )

    projection = _write_projection(
        conn,
        project_id=head["project_id"],
        source=source,
        target=target,
        relationship_kind=head["relationship_kind"],
        created_by=actor,
    )
    edge_id = projection["id"] if projection else None

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.context_relationships "
            "SET status = 'active', projection_edge_id = %s, updated_at = now() "
            "WHERE id = %s",
            (edge_id, relationship_id),
        )

    version = _append_version(
        conn,
        relationship_id=relationship_id,
        org_id=head["org_id"],
        project_id=head["project_id"],
        lifecycle="restored",
        fact={
            "source": source.as_dict(),
            "target": target.as_dict(),
            "kind": head["relationship_kind"],
            "provenance": head["provenance"],
        },
        content_hash=relationship_hash(
            project_id=head["project_id"],
            source=source,
            target=target,
            relationship_kind=head["relationship_kind"],
        ),
        supersedes_version_id=head["current_version_id"],
        source_version_id=None,
        target_version_id=None,
        actor=actor,
    )

    _record(
        conn,
        ACTION_CONTEXT_RELATIONSHIP_RESTORED,
        actor=actor,
        project_id=head["project_id"],
        relationship=relationship_id,
        relationship_kind=head["relationship_kind"],
    )

    head["status"] = "active"
    head["projection_edge_id"] = edge_id
    head["projection"] = projection
    head["current_version_id"] = version["id"]
    head["version"] = version
    head["source_owner"] = owner_link(source, project_id=head["project_id"])
    head["target_owner"] = owner_link(target, project_id=head["project_id"])
    return head


def find_by_projection_edge(
    conn: Any, *, edge_id: str
) -> dict[str, Any] | None:
    """The relation an incumbent `edge_...` id names, or None.

    The bridge the DELETE door needs: a caller still holds an edge id, and the
    authority answers for it rather than making the caller learn a new
    identifier the day the cutover lands.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(_HEAD_COLUMNS)}
            FROM app.context_relationships
            WHERE projection_edge_id = %s
            """,
            (edge_id,),
        )
        row = cur.fetchone()
    return _head_row(row) if row else None


class ContextRelationshipService:
    """The named seam story 49-6 AC5 asks for, bound to one connection.

    The functions above are the implementation and stay callable -- every other
    store of this repository is written that way, and a class that hid them
    would make the module the odd one out. This is the name the story, the
    Ownership section and `governance.md` all use, and it is what a caller
    holding a connection reaches for.
    """

    def __init__(self, conn: Any, *, project_id: str | None) -> None:
        self._conn = conn
        self._project_id = project_id

    def create_relationship(self, **kwargs: Any) -> dict[str, Any]:
        return create_relationship(self._conn, project_id=self._project_id, **kwargs)

    def supersede_relationship(self, **kwargs: Any) -> dict[str, Any]:
        return supersede_relationship(
            self._conn, project_id=self._project_id, **kwargs
        )

    def restore(self, **kwargs: Any) -> dict[str, Any]:
        return restore(self._conn, project_id=self._project_id, **kwargs)

    def read_relationship(self, relationship_id: str) -> dict[str, Any]:
        return read_relationship(
            self._conn, project_id=self._project_id, relationship_id=relationship_id
        )

    def list_for_node(self, **kwargs: Any) -> dict[str, Any]:
        return list_for_node(self._conn, project_id=self._project_id, **kwargs)
