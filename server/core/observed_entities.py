"""Observed delivery entities mounted on the generic Master Data owner (Story 70.2).

This module is the *domain* of an observed entity: what a delivery hierarchy is
(campaign -> placement -> creative, called L1/L2/L3 here because no platform
agrees on the nouns), how a raw row observed in a Datastream binds to a governed
identity, and how a dimension declared at one level is read at another. It owns
no lifecycle. Registries, nodes, immutable versions, dated membership edges and
the fail-closed used-by all live in :mod:`core.master_data`, exactly as
:mod:`core.country_registry` mounts Country on the same five tables.

THE IDENTITY IS THE PATH, NEVER THE ENTITY ID
---------------------------------------------
A node's identity here is the FULL hierarchical path
``(platform, level, l1_id, l2_id, l3_id)``, encoded once by
:func:`canonical_entity_key`. The reference case measured why: 158 of 574
creatives were attached to several parents, 14 of them inheriting divergent
taxonomies. Two platforms also re-emit the same entity id, so a binding made on
the id alone merges two campaigns under one name and the sum stops being
defensible. Under path identity the same creative seen under two parents is two
nodes, each carrying its own inheritance -- which is the answer, not a
duplicate.

WHERE THE ATTACHMENT LIVES, AND WHY NO MIGRATION WAS NEEDED
-----------------------------------------------------------
``app.master_data_aliases`` already is the MDM's value->node binding: a
namespace, a raw value, a SKOS-typed relation, provenance, evidence, and a
UNIQUE index over ``(org_id, namespace, normalized_value, locale)`` for live
``exact`` claims. That index is what makes "one path names exactly one node" a
database fact rather than a query-time coincidence -- the same guarantee
``uq_dimension_value_mappings_scope_key`` gives ``dimension_value_mappings``,
whose model this attachment follows. Nothing new had to be stored, so nothing
was migrated.

Two deliberate departures from :func:`core.master_data.record_alias`, which
could not be reused as-is:

* it writes ``project_id = NULL`` (organization scope). Observed entities are
  Project-scoped instead -- a delivery entity belongs to the Datastreams of one
  Project. When this story landed the scope was also FORCED: ``app.master_data_
  used_by`` carried a ``(project_id, node_id)`` composite foreign key, so an
  organization-scoped node could not be used-by-guarded at all. Migration 311 has
  since replaced that composite key with a single-column FK plus an
  organization-boundary trigger (it names ``observed_entities`` a compatible
  writer), so an org-scoped node is now guardable and the Project scope is the
  design choice, no longer a constraint the schema imposes;
* it normalizes with :func:`core.master_data.normalize_alias_value`, which
  case-folds. Two platform ids differing only in case are two entities; folding
  them is precisely the merge the path identity exists to prevent. The stored
  ``normalized_value`` here is the canonical key verbatim -- already escaped,
  already deterministic, and case-preserving.

INHERITANCE IS RESOLVED AT READ, WITH PROVENANCE PER DIMENSION
--------------------------------------------------------------
``self -> inherited_parent -> inherited_l1 -> unresolved``, declared and
ordered, and every dimension says which one answered it. ``unresolved`` is an
emitted, counted state: never an empty string, never a label invented to fill
the hole. Nothing in this module parses a human label at any level -- L2 and L3
inherit, and a test holds that they do.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

from ulid import ULID

from core.audit import declare_action, insert_audit_row
from core.master_data import (
    DraftContent,
    MasterDataConflict,
    MasterDataError,
    MasterDataNotFound,
    Membership,
    UsedByReference,
    create_draft_version,
    create_node,
    create_registry,
    fetch_memberships,
    fetch_registry,
    fetch_used_by,
    list_nodes,
    publish_version,
    register_used_by,
    release_used_by,
    require_registry,
    require_version,
    resolve_ancestry,
)

# ---------------------------------------------------------------------------
# THE ACTIONS THIS MODULE WRITES.
#
# AD-42: a composed action is declared TERM BY TERM. Both families below are
# built by comprehension over a closed tuple of change kinds, so a fourth kind
# added one day fails HERE rather than landing mute in the journal.
# ---------------------------------------------------------------------------

_ATTACHMENT_CHANGE_KINDS = ("attached", "reattached", "released")
OBSERVED_ENTITY_ATTACHMENT_ACTIONS = {
    kind: declare_action(f"observed_entity_attachment.{kind}")
    for kind in _ATTACHMENT_CHANGE_KINDS
}

_HIERARCHY_CHANGE_KINDS = ("published",)
OBSERVED_ENTITY_HIERARCHY_ACTIONS = {
    kind: declare_action(f"observed_entity_hierarchy.{kind}")
    for kind in _HIERARCHY_CHANGE_KINDS
}

AUDIT_PROVIDER_ACCOUNT = "master-data"


# ---------------------------------------------------------------------------
# Vocabulary.
# ---------------------------------------------------------------------------

OBSERVED_ENTITY_OBJECT_KIND = "observed_entity"
OBSERVED_ENTITY_REGISTRY_LABEL = "Observed Entity Registry"

LEVEL_L1 = "l1"
LEVEL_L2 = "l2"
LEVEL_L3 = "l3"
#: Ordered, outermost first. The order is load-bearing: it decides what a
#: parent is and where the L1 fallback stops.
LEVELS: tuple[str, ...] = (LEVEL_L1, LEVEL_L2, LEVEL_L3)

#: Node kinds. `master_data` validates the shape and never compares to a
#: literal, so these are this module's vocabulary and nobody else's.
NODE_KIND_OF_LEVEL: dict[str, str] = {
    LEVEL_L1: "entity_l1",
    LEVEL_L2: "entity_l2",
    LEVEL_L3: "entity_l3",
}
LEVEL_OF_NODE_KIND: dict[str, str] = {kind: level for level, kind in NODE_KIND_OF_LEVEL.items()}

#: The namespace an attachment lives in, per Project. The alias UNIQUE index
#: carries `org_id` but not `project_id`, so two Projects of one organization
#: observing the same platform entity would collide on an org-wide namespace --
#: and their nodes are Project-scoped, so they must not.
ATTACHMENT_NAMESPACE_PREFIX = "observed_entity"
#: `exact` in the SKOS sense: this path IS this identity. `close` would say the
#: two merely resemble each other, which is not a binding anything may sum on.
ATTACHMENT_RELATION = "exact"
ATTACHMENT_PROVENANCE = "connector"
#: What the used-by store records, so a regroup can name who breaks.
ATTACHMENT_CONSUMER_KIND = "observed_entity_attachment"

#: The encoding version. It prefixes every key so a future encoding is a
#: different key rather than a silent re-reading of the stored ones.
KEY_SCHEME = "oe1"
_KEY_SEPARATOR = "|"
#: `master_data_aliases.raw_value` and `normalized_value` both CHECK 1..400.
MAX_KEY_LENGTH = 400
#: `master_data_aliases.namespace` CHECKs 1..80.
MAX_NAMESPACE_LENGTH = 80

_PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_DIMENSION_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

#: The four states of the read cascade, in the order they are tried. Every one
#: of them is emitted and counted, `unresolved` included.
PROVENANCE_SELF = "self"
PROVENANCE_INHERITED_PARENT = "inherited_parent"
PROVENANCE_INHERITED_L1 = "inherited_l1"
PROVENANCE_UNRESOLVED = "unresolved"
PROVENANCES: tuple[str, ...] = (
    PROVENANCE_SELF,
    PROVENANCE_INHERITED_PARENT,
    PROVENANCE_INHERITED_L1,
    PROVENANCE_UNRESOLVED,
)


class ObservedEntityError(MasterDataError):
    """An observed-entity operation was rejected."""

    code = "invalid_observed_entity_operation"


# ---------------------------------------------------------------------------
# The identity. Pure, deterministic, and testable without a database.
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    """Trim to a stable string. ``None`` and blanks are one empty field."""

    return "" if value is None else str(value).strip()


def canonical_entity_key(
    *,
    platform: str,
    level: str,
    l1_id: str | None = None,
    l2_id: str | None = None,
    l3_id: str | None = None,
) -> str:
    """Encode one full hierarchical path as a stable, unambiguous key.

    Every component is percent-escaped with an empty safe set, so the separator
    cannot occur inside a component and two different tuples can never encode to
    one string. An absent level is the empty component, which keeps a level-1
    path distinguishable from a level-2 path whose parent happens to be blank.

    Deliberately NOT case-folded: platform ids that differ only in case are
    different entities, and folding them would merge exactly what path identity
    exists to keep apart.
    """

    parts = (
        _text(platform),
        _text(level),
        _text(l1_id),
        _text(l2_id),
        _text(l3_id),
    )
    key = _KEY_SEPARATOR.join([KEY_SCHEME, *(quote(part, safe="") for part in parts)])
    if len(key) > MAX_KEY_LENGTH:
        raise ObservedEntityError(
            f"the encoded entity path is {len(key)} characters; "
            f"{MAX_KEY_LENGTH} is the maximum an attachment can carry"
        )
    return key


@dataclass(frozen=True, slots=True)
class ObservedEntityPath:
    """One observed entity, identified by where it sits and nowhere else.

    The constructor is the refusal the story asks for: a level that does not
    name every id above it is not an identity, it is an id, and an attachment
    made on an id alone mixes two campaigns under one name.
    """

    platform: str
    level: str
    l1_id: str = ""
    l2_id: str = ""
    l3_id: str = ""

    def __post_init__(self) -> None:
        platform = _text(self.platform)
        if not _PLATFORM_RE.fullmatch(platform):
            raise ObservedEntityError(
                "platform is required and must be lowercase snake_case, 2 to 40 characters"
            )
        object.__setattr__(self, "platform", platform)

        level = _text(self.level)
        if level not in LEVELS:
            raise ObservedEntityError(f"level must be one of {list(LEVELS)}, not {level!r}")
        object.__setattr__(self, "level", level)

        depth = LEVELS.index(level)
        for index, name in enumerate(("l1_id", "l2_id", "l3_id")):
            value = _text(getattr(self, name))
            object.__setattr__(self, name, value)
            if index <= depth and not value:
                raise ObservedEntityError(
                    f"a level-{depth + 1} entity identifies itself by its whole path; "
                    f"{name} is missing. An entity id alone is not an identity."
                )
            if index > depth and value:
                raise ObservedEntityError(
                    f"a level-{depth + 1} entity cannot carry {name}: "
                    "the path stops at its own level"
                )

    @property
    def depth(self) -> int:
        """1, 2 or 3. The number of ids the path actually names."""

        return LEVELS.index(self.level) + 1

    @property
    def node_kind(self) -> str:
        return NODE_KIND_OF_LEVEL[self.level]

    @property
    def canonical_key(self) -> str:
        return canonical_entity_key(
            platform=self.platform,
            level=self.level,
            l1_id=self.l1_id,
            l2_id=self.l2_id,
            l3_id=self.l3_id,
        )

    @property
    def entity_id(self) -> str:
        """The id at this path's own level. A label, never an identity."""

        return (self.l1_id, self.l2_id, self.l3_id)[self.depth - 1]

    def parent(self) -> "ObservedEntityPath | None":
        """The path one level up, or None at L1."""

        if self.level == LEVEL_L1:
            return None
        if self.level == LEVEL_L2:
            return ObservedEntityPath(platform=self.platform, level=LEVEL_L1, l1_id=self.l1_id)
        return ObservedEntityPath(
            platform=self.platform, level=LEVEL_L2, l1_id=self.l1_id, l2_id=self.l2_id
        )

    def l1_path(self) -> "ObservedEntityPath":
        return ObservedEntityPath(platform=self.platform, level=LEVEL_L1, l1_id=self.l1_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "level": self.level,
            "l1_id": self.l1_id,
            "l2_id": self.l2_id,
            "l3_id": self.l3_id,
            "canonical_key": self.canonical_key,
        }


def path_from_mapping(value: Mapping[str, Any]) -> ObservedEntityPath:
    return ObservedEntityPath(
        platform=_text(value.get("platform")),
        level=_text(value.get("level")),
        l1_id=_text(value.get("l1_id")),
        l2_id=_text(value.get("l2_id")),
        l3_id=_text(value.get("l3_id")),
    )


def attachment_namespace(project_id: str) -> str:
    project = _text(project_id)
    if not project:
        raise ObservedEntityError("project_id is required")
    namespace = f"{ATTACHMENT_NAMESPACE_PREFIX}:{project}"
    if len(namespace) > MAX_NAMESPACE_LENGTH:
        raise ObservedEntityError(
            f"the attachment namespace for {project} is {len(namespace)} characters; "
            f"{MAX_NAMESPACE_LENGTH} is the maximum"
        )
    return namespace


def default_node_label(path: ObservedEntityPath) -> str:
    """What the node is CALLED when the caller offers nothing better.

    The label is a projection and may be renamed freely; the identity is the
    alias key. Falling back to the entity id keeps a screen readable without
    ever letting the id become the identity.
    """

    return path.entity_id[:120]


# ---------------------------------------------------------------------------
# The registry itself.
# ---------------------------------------------------------------------------


def ensure_observed_entity_registry(
    conn, *, org_id: str, project_id: str, actor: str
) -> dict[str, Any]:
    """The Project's single owner of observed delivery entities."""

    return create_registry(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind=OBSERVED_ENTITY_OBJECT_KIND,
        label=OBSERVED_ENTITY_REGISTRY_LABEL,
        actor=actor,
    )


def fetch_observed_entity_registry(conn, *, project_id: str) -> dict[str, Any] | None:
    return fetch_registry(
        conn, project_id=project_id, object_kind=OBSERVED_ENTITY_OBJECT_KIND
    )


# ---------------------------------------------------------------------------
# Attachment: raw observed entity -> governed node.
# ---------------------------------------------------------------------------

#: Read and written by hand rather than through `record_alias`, for the two
#: reasons stated in the module docstring. The column list mirrors
#: `core.master_data._ALIAS_COLUMNS`; a conformance test would catch a drift
#: faster than a shared private import would prevent one.
ATTACHMENT_COLUMNS: tuple[str, ...] = (
    "id",
    "org_id",
    "project_id",
    "node_id",
    "namespace",
    "raw_value",
    "normalized_value",
    "relation",
    "provenance",
    "provenance_reference",
    "evidence",
    "conflict_state",
    "created_by",
    "created_at",
    "retired_at",
)
_ATTACHMENT_SELECT = ", ".join(ATTACHMENT_COLUMNS)


def _attachment_row(row: Any) -> dict[str, Any] | None:
    return None if row is None else dict(zip(ATTACHMENT_COLUMNS, row, strict=False))


def datastream_consumer_label(conn, *, project_id: str, datastream_id: str) -> str | None:
    """The WORD an attachment consumer is known by, or None when there is none.

    An attachment has no workbench of its own -- `master_data_consumers` files it
    under the Data workspace with no href -- so the only thing naming it on a
    Master Data refusal is this label. It was written as `_text(datastream_id)`,
    which put a `ds_<ULID>` in a name's position on every screen that lists
    consumers, and no browser fallback could repair it: the server had already
    answered, and its answer was the identifier.

    `app.datastreams.name` is `NOT NULL` and unique inside a Project
    (`uq_datastreams_project_name`, migration 023), so the word exists whenever
    the Datastream does. None comes back when it does not, and the reader is then
    told the consumer is unnamed rather than handed a token -- the rule of
    `visualization-and-rendering.md`, *A member's label is a name, never its
    identifier*, applied where the name is minted rather than where it is shown.
    """

    stream = _text(datastream_id)
    if not stream:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name FROM app.datastreams WHERE id = %s AND project_id = %s",
            (stream, _text(project_id)),
        )
        row = cur.fetchone()
    return None if row is None else (_text(row[0]) or None)


def fetch_attachment(
    conn, *, project_id: str, path: ObservedEntityPath | Mapping[str, Any]
) -> dict[str, Any] | None:
    """The live binding of one path, or None. Reads only; writes nothing."""

    resolved = path if isinstance(path, ObservedEntityPath) else path_from_mapping(path)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_ATTACHMENT_SELECT}
            FROM app.master_data_aliases
            WHERE project_id = %s AND namespace = %s AND normalized_value = %s
              AND relation = %s AND retired_at IS NULL AND effective_to IS NULL
            """,
            (
                _text(project_id),
                attachment_namespace(project_id),
                resolved.canonical_key,
                ATTACHMENT_RELATION,
            ),
        )
        return _attachment_row(cur.fetchone())


def list_attachments(
    conn, *, project_id: str, node_ids: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """Every live binding of this Project, optionally narrowed to some nodes."""

    clauses = [
        "project_id = %s",
        "namespace = %s",
        "relation = %s",
        "retired_at IS NULL",
        "effective_to IS NULL",
    ]
    params: list[Any] = [
        _text(project_id),
        attachment_namespace(project_id),
        ATTACHMENT_RELATION,
    ]
    if node_ids is not None:
        if not node_ids:
            return []
        clauses.append("node_id = ANY(%s)")
        params.append(list(node_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {_ATTACHMENT_SELECT}
            FROM app.master_data_aliases
            WHERE {" AND ".join(clauses)}
            ORDER BY node_id, normalized_value
            """,
            tuple(params),
        )
        return [dict(zip(ATTACHMENT_COLUMNS, row, strict=False)) for row in cur.fetchall()]


def _require_node(conn, *, project_id: str, registry_id: str, node_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, node_kind, label, archived_at
            FROM app.master_data_nodes
            WHERE project_id = %s AND registry_id = %s AND id = %s
            """,
            (_text(project_id), _text(registry_id), _text(node_id)),
        )
        row = cur.fetchone()
    if row is None:
        raise MasterDataNotFound("node not found in this observed-entity registry")
    node = {"id": row[0], "node_kind": row[1], "label": row[2], "archived_at": row[3]}
    if node["archived_at"] is not None:
        raise MasterDataConflict(
            f"node {node['id']} is archived; restore it before attaching to it"
        )
    return node


def _audit(
    conn,
    *,
    actor: str,
    action: str,
    org_id: str,
    project_id: str,
    resource_id: str,
    reason: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
    trace_id: str | None = None,
) -> None:
    """One audit row on the CALLER's transaction, beside the mutation it records."""

    insert_audit_row(
        conn,
        identity=_text(actor) or "system",
        action=action,
        provider_account=AUDIT_PROVIDER_ACCOUNT,
        connection_ref="",
        metadata={
            "effective_org_id": org_id,
            "project_id": project_id,
            "resource_id": resource_id,
            "object_kind": OBSERVED_ENTITY_OBJECT_KIND,
            "reason": reason,
            "trace_id": trace_id,
            "before": dict(before) if before is not None else None,
            "after": dict(after) if after is not None else None,
        },
    )


def _insert_attachment(
    conn,
    *,
    org_id: str,
    project_id: str,
    node_id: str,
    path: ObservedEntityPath,
    datastream_id: str,
    actor: str,
) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO app.master_data_aliases
                (id, org_id, project_id, node_id, namespace, raw_value, normalized_value,
                 relation, provenance, provenance_reference, evidence, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING {_ATTACHMENT_SELECT}
            """,
            (
                f"mdali_{ULID()}",
                _text(org_id),
                _text(project_id),
                _text(node_id),
                attachment_namespace(project_id),
                path.canonical_key,
                path.canonical_key,
                ATTACHMENT_RELATION,
                ATTACHMENT_PROVENANCE,
                _text(datastream_id),
                json.dumps({"datastream_id": _text(datastream_id), **path.as_dict()}),
                _text(actor) or "system",
            ),
        )
        row = _attachment_row(cur.fetchone())
    if row is None:  # pragma: no cover - only under a concurrent delete
        raise MasterDataConflict("the attachment could not be written or read back")
    return row


def attach_observed_entity(
    conn,
    *,
    org_id: str,
    project_id: str,
    registry_id: str,
    path: ObservedEntityPath | Mapping[str, Any],
    datastream_id: str,
    actor: str,
    node_id: str | None = None,
    label: str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Bind one observed entity of a Datastream to a governed node.

    Idempotent by path: a path already bound to the same node returns the
    existing row and writes NOTHING -- no second alias, no second audit line,
    no second used-by row. A path already bound to a DIFFERENT node is refused
    here and sent to :func:`reattach_observed_entity`, because moving a binding
    is a regroup and a regroup has to be guarded.

    Passing ``node_id`` attaches to an existing identity (the case that merges
    two observations of the same entity); omitting it mints one whose label is
    a projection and whose identity is the path.
    """

    resolved = path if isinstance(path, ObservedEntityPath) else path_from_mapping(path)
    registry = require_registry(conn, project_id=project_id, registry_id=registry_id)

    existing = fetch_attachment(conn, project_id=project_id, path=resolved)
    if existing is not None:
        if node_id is None or _text(node_id) == existing["node_id"]:
            return {"outcome": "unchanged", "attachment": existing, "node_id": existing["node_id"]}
        raise MasterDataConflict(
            f"{resolved.canonical_key} is already attached to node {existing['node_id']}. "
            "Moving it is a regroup: use reattach_observed_entity, which shows what "
            "depends on the node it would leave."
        )

    if node_id is None:
        node = create_node(
            conn,
            org_id=org_id,
            project_id=project_id,
            registry_id=registry["id"],
            node_kind=resolved.node_kind,
            label=_text(label) or default_node_label(resolved),
            actor=actor,
        )
        minted = True
    else:
        node = _require_node(
            conn, project_id=project_id, registry_id=registry["id"], node_id=node_id
        )
        minted = False
        if node["node_kind"] != resolved.node_kind:
            raise MasterDataConflict(
                f"node {node['id']} is a {node['node_kind']}; "
                f"a level-{resolved.depth} path attaches to a {resolved.node_kind}"
            )

    attachment = _insert_attachment(
        conn,
        org_id=org_id,
        project_id=project_id,
        node_id=node["id"],
        path=resolved,
        datastream_id=datastream_id,
        actor=actor,
    )
    register_used_by(
        conn,
        project_id=project_id,
        registry_id=registry["id"],
        reference=UsedByReference(
            node_id=node["id"],
            consumer_kind=ATTACHMENT_CONSUMER_KIND,
            consumer_id=attachment["id"],
            consumer_label=datastream_consumer_label(
                conn, project_id=project_id, datastream_id=datastream_id
            ),
        ),
        actor=_text(actor) or "system",
    )
    _audit(
        conn,
        actor=actor,
        action=OBSERVED_ENTITY_ATTACHMENT_ACTIONS["attached"],
        org_id=org_id,
        project_id=project_id,
        resource_id=attachment["id"],
        reason=f"observed entity of datastream {_text(datastream_id)} bound to a governed node",
        before=None,
        after={
            "node_id": node["id"],
            "node_minted": minted,
            "datastream_id": _text(datastream_id),
            **resolved.as_dict(),
        },
        trace_id=trace_id,
    )
    return {
        "outcome": "attached",
        "attachment": attachment,
        "node_id": node["id"],
        "node_minted": minted,
    }


def blocking_consumers(
    conn,
    *,
    project_id: str,
    registry_id: str,
    node_id: str,
    excluding_attachment_id: str | None = None,
) -> tuple[UsedByReference, ...]:
    """Who still depends on a node, other than the attachment being moved.

    :func:`core.master_data.fetch_used_by` RAISES ``MasterDataUnavailable`` when
    the store cannot be read, and that exception is deliberately not caught
    anywhere in this module: "I could not check" and "nothing depends on this"
    are different facts, and only one of them is safe to act on.
    """

    references = fetch_used_by(
        conn, project_id=project_id, registry_id=registry_id, node_ids=[node_id]
    )
    if excluding_attachment_id is None:
        return references
    return tuple(
        reference
        for reference in references
        if not (
            reference.consumer_kind == ATTACHMENT_CONSUMER_KIND
            and reference.consumer_id == excluding_attachment_id
        )
    )


def reattach_observed_entity(
    conn,
    *,
    org_id: str,
    project_id: str,
    registry_id: str,
    path: ObservedEntityPath | Mapping[str, Any],
    node_id: str,
    actor: str,
    acknowledge_impact: bool = False,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Move one path's binding to another node -- guarded, or refused.

    This is the destructive regroup the used-by store exists for. Whatever else
    still points at the node the path is leaving loses those rows, so the
    consumers are read and NAMED before anything is written, and the caller
    either releases them or states that a human saw them and chose to proceed.

    Raises:
        MasterDataConflict: live consumers remain and the impact was not
            acknowledged.
        MasterDataUnavailable: the used-by store could not be read (fail closed).
    """

    resolved = path if isinstance(path, ObservedEntityPath) else path_from_mapping(path)
    registry = require_registry(conn, project_id=project_id, registry_id=registry_id)

    existing = fetch_attachment(conn, project_id=project_id, path=resolved)
    if existing is None:
        raise MasterDataNotFound(
            f"{resolved.canonical_key} is not attached in this Project; attach it first"
        )
    target_id = _text(node_id)
    if target_id == existing["node_id"]:
        return {"outcome": "unchanged", "attachment": existing, "node_id": existing["node_id"]}

    target = _require_node(
        conn, project_id=project_id, registry_id=registry["id"], node_id=target_id
    )
    if target["node_kind"] != resolved.node_kind:
        raise MasterDataConflict(
            f"node {target['id']} is a {target['node_kind']}; "
            f"a level-{resolved.depth} path attaches to a {resolved.node_kind}"
        )

    consumers = blocking_consumers(
        conn,
        project_id=project_id,
        registry_id=registry["id"],
        node_id=existing["node_id"],
        excluding_attachment_id=existing["id"],
    )
    if consumers and not acknowledge_impact:
        summary = ", ".join(sorted({reference.consumer_kind for reference in consumers}))
        raise MasterDataConflict(
            f"{len(consumers)} live reference(s) still depend on node "
            f"{existing['node_id']} ({summary}). Regrouping this entity away from it "
            "would change what they mean. Release them, or reattach with the impact "
            "acknowledged."
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.master_data_aliases
               SET retired_at = NOW(), retired_by = %s
             WHERE id = %s AND retired_at IS NULL
            """,
            (_text(actor) or "system", existing["id"]),
        )
    release_used_by(
        conn,
        project_id=project_id,
        node_id=existing["node_id"],
        consumer_kind=ATTACHMENT_CONSUMER_KIND,
        consumer_id=existing["id"],
    )
    attachment = _insert_attachment(
        conn,
        org_id=org_id,
        project_id=project_id,
        node_id=target["id"],
        path=resolved,
        datastream_id=_text(existing.get("provenance_reference")),
        actor=actor,
    )
    register_used_by(
        conn,
        project_id=project_id,
        registry_id=registry["id"],
        reference=UsedByReference(
            node_id=target["id"],
            consumer_kind=ATTACHMENT_CONSUMER_KIND,
            consumer_id=attachment["id"],
            consumer_label=datastream_consumer_label(
                conn,
                project_id=project_id,
                datastream_id=_text(existing.get("provenance_reference")),
            ),
        ),
        actor=_text(actor) or "system",
    )
    _audit(
        conn,
        actor=actor,
        action=OBSERVED_ENTITY_ATTACHMENT_ACTIONS["reattached"],
        org_id=org_id,
        project_id=project_id,
        resource_id=attachment["id"],
        reason="observed entity regrouped onto another governed node",
        before={"node_id": existing["node_id"], "attachment_id": existing["id"]},
        after={
            "node_id": target["id"],
            "attachment_id": attachment["id"],
            "impact_acknowledged": bool(acknowledge_impact and consumers),
            "consumers_at_command_time": [reference.as_dict() for reference in consumers],
            **resolved.as_dict(),
        },
        trace_id=trace_id,
    )
    return {
        "outcome": "reattached",
        "attachment": attachment,
        "node_id": target["id"],
        "released_node_id": existing["node_id"],
        "consumers_at_command_time": [reference.as_dict() for reference in consumers],
    }


def release_observed_entity(
    conn,
    *,
    org_id: str,
    project_id: str,
    path: ObservedEntityPath | Mapping[str, Any],
    actor: str,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Withdraw one binding. The node and every version naming it survive."""

    resolved = path if isinstance(path, ObservedEntityPath) else path_from_mapping(path)
    existing = fetch_attachment(conn, project_id=project_id, path=resolved)
    if existing is None:
        raise MasterDataNotFound(f"{resolved.canonical_key} is not attached in this Project")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.master_data_aliases
               SET retired_at = NOW(), retired_by = %s
             WHERE id = %s AND retired_at IS NULL
            """,
            (_text(actor) or "system", existing["id"]),
        )
    release_used_by(
        conn,
        project_id=project_id,
        node_id=existing["node_id"],
        consumer_kind=ATTACHMENT_CONSUMER_KIND,
        consumer_id=existing["id"],
    )
    _audit(
        conn,
        actor=actor,
        action=OBSERVED_ENTITY_ATTACHMENT_ACTIONS["released"],
        org_id=org_id,
        project_id=project_id,
        resource_id=existing["id"],
        reason="observed entity binding withdrawn",
        before={"node_id": existing["node_id"], **resolved.as_dict()},
        after=None,
        trace_id=trace_id,
    )
    return {"outcome": "released", "attachment": existing, "node_id": existing["node_id"]}


# ---------------------------------------------------------------------------
# The hierarchy version: dated edges plus the dimensions declared per node.
# ---------------------------------------------------------------------------


def normalize_declared_dimensions(
    declared: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, str]]:
    """Validate what a version declares, so `unresolved` stays honest.

    An empty declared value is REFUSED rather than stored: a stored blank reads
    as a resolved answer at every level below it, and the whole point of the
    fourth state is that a hole says so. Omit the dimension instead.
    """

    normalized: dict[str, dict[str, str]] = {}
    for node_id, values in declared.items():
        node = _text(node_id)
        if not node:
            raise ObservedEntityError("a dimension declaration names the node it belongs to")
        bucket: dict[str, str] = {}
        for dimension, value in (values or {}).items():
            name = _text(dimension)
            if not _DIMENSION_RE.fullmatch(name):
                raise ObservedEntityError(
                    f"dimension {dimension!r} must be lowercase snake_case, 2 to 64 characters"
                )
            text = _text(value)
            if not text:
                raise ObservedEntityError(
                    f"dimension {name!r} on node {node} declares an empty value. "
                    "Omit it: an empty string reads as an answer, and it is not one."
                )
            bucket[name] = text
        normalized[node] = bucket
    return normalized


def build_hierarchy_memberships(
    node_id_of_key: Mapping[str, str],
    paths: Iterable[ObservedEntityPath],
    *,
    effective_from: date | None = None,
    effective_to: date | None = None,
) -> tuple[Membership, ...]:
    """Dated parent edges, derived from the paths themselves.

    A path whose parent is not attached yields NO edge: an orphan L3 is a real
    reading (its parent was never observed) and inventing a parent for it would
    be the fabricated answer the whole story refuses. It resolves `unresolved`,
    which is exactly what it is.
    """

    edges: list[Membership] = []
    seen: set[tuple[str, str]] = set()
    for order, path in enumerate(sorted(paths, key=lambda item: item.canonical_key)):
        parent = path.parent()
        if parent is None:
            continue
        child_id = node_id_of_key.get(path.canonical_key)
        parent_id = node_id_of_key.get(parent.canonical_key)
        if child_id is None or parent_id is None:
            continue
        if (parent_id, child_id) in seen:
            continue
        seen.add((parent_id, child_id))
        kwargs: dict[str, Any] = {"display_order": order}
        if effective_from is not None:
            kwargs["effective_from"] = effective_from
        if effective_to is not None:
            kwargs["effective_to"] = effective_to
        edges.append(
            Membership(parent_node_id=parent_id, child_node_id=child_id, **kwargs)
        )
    return tuple(edges)


def hierarchy_payload(
    *,
    node_paths: Mapping[str, ObservedEntityPath],
    declared_dimensions: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """What one immutable version carries besides its edges."""

    return {
        "node_paths": {
            node_id: path.as_dict() for node_id, path in sorted(node_paths.items())
        },
        "node_dimensions": normalize_declared_dimensions(declared_dimensions or {}),
    }


def publish_entity_hierarchy(
    conn,
    *,
    org_id: str,
    project_id: str,
    registry_id: str,
    actor: str,
    node_paths: Mapping[str, ObservedEntityPath],
    declared_dimensions: Mapping[str, Mapping[str, Any]] | None = None,
    effective_from: date | None = None,
    effective_date: date | str | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Freeze one reading of the hierarchy and its declared dimensions.

    A regroup is a NEW version, never a mutation nobody can date -- that rule is
    the generic owner's, and this door does not soften it.
    """

    registry = require_registry(conn, project_id=project_id, registry_id=registry_id)
    payload = hierarchy_payload(
        node_paths=node_paths, declared_dimensions=declared_dimensions
    )
    key_of_node = {node_id: path.canonical_key for node_id, path in node_paths.items()}
    node_id_of_key = {key: node_id for node_id, key in key_of_node.items()}
    memberships = build_hierarchy_memberships(
        node_id_of_key, node_paths.values(), effective_from=effective_from
    )
    draft = create_draft_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        registry_id=registry["id"],
        actor=actor,
        content=DraftContent(memberships=memberships, payload=payload),
        effective_date=effective_date,
    )
    published = publish_version(
        conn, project_id=project_id, version_id=draft["id"], actor=actor
    )
    _audit(
        conn,
        actor=actor,
        action=OBSERVED_ENTITY_HIERARCHY_ACTIONS["published"],
        org_id=org_id,
        project_id=project_id,
        resource_id=published["id"],
        reason="observed entity hierarchy published",
        before=None,
        after={
            "registry_id": registry["id"],
            "version_id": published["id"],
            "version_number": published["version_number"],
            "content_hash": published["content_hash"],
            "node_count": len(node_paths),
            "edge_count": len(memberships),
        },
        trace_id=trace_id,
    )
    return published


# ---------------------------------------------------------------------------
# The read cascade. Pure, so the four provenances are proven directly.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolution:
    """One dimension of one node, and where its value came from.

    ``value`` is None exactly when ``provenance`` is ``unresolved``. There is no
    third shape: an empty string would read as an answer.
    """

    dimension: str
    value: str | None
    provenance: str
    source_node_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "value": self.value,
            "provenance": self.provenance,
            "source_node_id": self.source_node_id,
        }


@dataclass(frozen=True, slots=True)
class EntityProjection:
    """One Project's observed hierarchy at one exact version and date."""

    hierarchy_version_id: str
    registry_id: str
    as_of: date
    memberships: tuple[Membership, ...]
    kinds: Mapping[str, str] = field(default_factory=dict)
    paths: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    dimensions: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    def ancestry(self, node_id: str) -> tuple[str, ...]:
        """The chain from *node_id* to its root on this date, innermost first."""

        return resolve_ancestry(self.memberships, node_id, self.as_of)

    def l1_of(self, node_id: str) -> str | None:
        """The level-1 ancestor, or None when the chain never reaches one."""

        for candidate in self.ancestry(node_id):
            if self.kinds.get(candidate) == NODE_KIND_OF_LEVEL[LEVEL_L1]:
                return candidate
        return None

    def _declared(self, node_id: str | None, dimension: str) -> str | None:
        if node_id is None:
            return None
        value = (self.dimensions.get(node_id) or {}).get(dimension)
        text = _text(value)
        return text or None

    def resolve(self, node_id: str, dimensions: Sequence[str]) -> dict[str, Resolution]:
        """Resolve each dimension in the declared order, and say who answered.

        ``self -> inherited_parent -> inherited_l1 -> unresolved``. The L1 step
        is skipped when the L1 IS the node or its parent -- it has already been
        consulted, and reporting the same value under a second provenance would
        make a two-level tree look like a three-level one.

        Nothing here reads a label. L2 and L3 inherit; a name is not evidence.
        """

        chain = self.ancestry(node_id)
        parent_id = chain[1] if len(chain) > 1 else None
        l1_id = self.l1_of(node_id)
        if l1_id in (node_id, parent_id):
            l1_id = None

        resolved: dict[str, Resolution] = {}
        for dimension in dimensions:
            name = _text(dimension)
            candidates = (
                (PROVENANCE_SELF, node_id),
                (PROVENANCE_INHERITED_PARENT, parent_id),
                (PROVENANCE_INHERITED_L1, l1_id),
            )
            answer: Resolution | None = None
            for provenance, source in candidates:
                value = self._declared(source, name)
                if value is not None:
                    answer = Resolution(
                        dimension=name,
                        value=value,
                        provenance=provenance,
                        source_node_id=source,
                    )
                    break
            resolved[name] = answer or Resolution(
                dimension=name, value=None, provenance=PROVENANCE_UNRESOLVED
            )
        return resolved

    def resolve_many(
        self, node_ids: Sequence[str], dimensions: Sequence[str]
    ) -> dict[str, dict[str, Resolution]]:
        return {node_id: self.resolve(node_id, dimensions) for node_id in node_ids}


def resolution_counts(resolutions: Mapping[str, Resolution]) -> dict[str, int]:
    """How many dimensions each provenance answered. All four keys, always.

    ``unresolved`` is present at zero rather than absent: a state that vanishes
    when it is empty cannot be watched, and watching it is how coverage moves.
    """

    counts = {provenance: 0 for provenance in PROVENANCES}
    for resolution in resolutions.values():
        counts[resolution.provenance] = counts.get(resolution.provenance, 0) + 1
    return counts


def aggregate_resolution_counts(
    resolved: Mapping[str, Mapping[str, Resolution]]
) -> dict[str, int]:
    totals = {provenance: 0 for provenance in PROVENANCES}
    for resolutions in resolved.values():
        for provenance, count in resolution_counts(resolutions).items():
            totals[provenance] = totals.get(provenance, 0) + count
    return totals


def build_entity_projection(
    *,
    hierarchy_version_id: str,
    registry_id: str,
    memberships: Sequence[Membership],
    nodes: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any] | None = None,
    as_of: date | None = None,
) -> EntityProjection:
    """Assemble the projection from already-read evidence. Pure and testable."""

    body = dict(payload or {})
    return EntityProjection(
        hierarchy_version_id=hierarchy_version_id,
        registry_id=registry_id,
        as_of=as_of or date.today(),
        memberships=tuple(memberships),
        kinds={str(node["id"]): str(node["node_kind"]) for node in nodes},
        paths={str(k): dict(v) for k, v in (body.get("node_paths") or {}).items()},
        dimensions={
            str(node_id): {str(k): _text(v) for k, v in (values or {}).items()}
            for node_id, values in (body.get("node_dimensions") or {}).items()
        },
    )


def load_entity_projection(
    conn, *, project_id: str, version_id: str | None = None, as_of: date | None = None
) -> EntityProjection | None:
    """Read the Project's observed hierarchy at an exact version.

    ``None`` means this Project has published no observed-entity meaning yet.
    Every caller must treat that as "cannot decide" -- never as "nothing to
    inherit", which would silently turn a missing version into full coverage.
    """

    registry = fetch_observed_entity_registry(conn, project_id=project_id)
    if registry is None:
        return None
    pinned = version_id or registry["current_version_id"]
    if not pinned:
        return None
    version = require_version(conn, project_id=project_id, version_id=str(pinned))
    return build_entity_projection(
        hierarchy_version_id=version["id"],
        registry_id=registry["id"],
        memberships=fetch_memberships(conn, project_id=project_id, version_id=str(pinned)),
        nodes=list_nodes(
            conn, project_id=project_id, registry_id=registry["id"], include_archived=True
        ),
        payload=version.get("payload") or {},
        as_of=as_of,
    )


__all__ = [
    "ATTACHMENT_CONSUMER_KIND",
    "ATTACHMENT_RELATION",
    "LEVELS",
    "LEVEL_L1",
    "LEVEL_L2",
    "LEVEL_L3",
    "NODE_KIND_OF_LEVEL",
    "OBSERVED_ENTITY_ATTACHMENT_ACTIONS",
    "OBSERVED_ENTITY_HIERARCHY_ACTIONS",
    "OBSERVED_ENTITY_OBJECT_KIND",
    "PROVENANCES",
    "PROVENANCE_INHERITED_L1",
    "PROVENANCE_INHERITED_PARENT",
    "PROVENANCE_SELF",
    "PROVENANCE_UNRESOLVED",
    "EntityProjection",
    "ObservedEntityError",
    "ObservedEntityPath",
    "Resolution",
    "aggregate_resolution_counts",
    "attach_observed_entity",
    "attachment_namespace",
    "blocking_consumers",
    "build_entity_projection",
    "build_hierarchy_memberships",
    "canonical_entity_key",
    "default_node_label",
    "ensure_observed_entity_registry",
    "fetch_attachment",
    "fetch_observed_entity_registry",
    "hierarchy_payload",
    "list_attachments",
    "load_entity_projection",
    "normalize_declared_dimensions",
    "path_from_mapping",
    "publish_entity_hierarchy",
    "reattach_observed_entity",
    "release_observed_entity",
    "resolution_counts",
]
